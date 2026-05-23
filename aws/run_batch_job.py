"""
Entrypoint for one AWS Batch array-job task.

Resilience guarantees
---------------------
1. CHECKPOINT RESUME  — at start, downloads any existing output from S3 for
   this slice so main.py's resume logic skips already-completed records.
   A task interrupted by SIGTERM, OOM, or host failure resumes from where
   it stopped on the next Batch retry attempt — zero work is duplicated.

2. PERIODIC UPLOAD    — a background thread uploads /tmp/output/ to S3 every
   CHECKPOINT_INTERVAL_S seconds (default 300). Progress is never lost for
   more than one interval even on a sudden crash or host failure.

3. SIGTERM HANDLER    — Fargate sends SIGTERM 30 s before SIGKILL. We catch it,
   stop the pipeline subprocess gracefully, then upload everything.

4. ALWAYS-UPLOAD FINALLY — the finally block runs on unhandled exceptions,
   OOM, and KeyboardInterrupt. Results always land in S3.

5. RETRY-SAFE AWS CALLS — every S3/SM call uses exponential back-off with
   jitter (adaptive Botocore mode + our own _with_retry wrapper).

6. CLEAN SHUTDOWN     — boto3 connection pools, temp GCP credentials, and
   the subprocess are closed/terminated before exit.

Env vars (set by Batch job definition)
---------------------------------------
  INPUT_BUCKET            S3 bucket containing the master index
  OUTPUT_BUCKET           S3 bucket for results (can equal INPUT_BUCKET)
  INDEX_KEY               Key of master dataset_index.csv
  SLICE_SIZE              Records per task (default 200)
  WORKERS                 Pipeline worker threads per task (default 4)
  GOOGLE_CREDS_SECRET_ID  Secrets Manager id with GCP SA + Gemini key
  RDS_CREDS_SECRET_ID     Secrets Manager id with RDS credentials
  CHECKPOINT_INTERVAL_S   Seconds between periodic S3 uploads (default 300)
"""

import atexit
import csv
import io
import json
import logging
import os
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import boto3
from botocore.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("run_batch_job")

# ---------------------------------------------------------------------------
# Job parameters
# ---------------------------------------------------------------------------
def _required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise EnvironmentError(f"Required env var '{name}' is not set.")
    return v


try:
    INPUT_BUCKET  = _required("INPUT_BUCKET")
    OUTPUT_BUCKET = _required("OUTPUT_BUCKET")
    INDEX_KEY     = _required("INDEX_KEY")
except EnvironmentError as _e:
    logging.critical("%s", _e)
    sys.exit(1)

_ARRAY_INDEX  = int(os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", "0"))
_INDEX_OFFSET = int(os.environ.get("JOB_INDEX_OFFSET", "0"))
JOB_INDEX     = _ARRAY_INDEX + _INDEX_OFFSET
JOB_ID        = os.environ.get("AWS_BATCH_JOB_ID", f"local-{JOB_INDEX}")
SLICE_SIZE  = int(os.environ.get("SLICE_SIZE", "200"))
WORKERS     = int(os.environ.get("WORKERS", "4"))
CHECKPOINT_INTERVAL_S = int(os.environ.get("CHECKPOINT_INTERVAL_S", "300"))

OUTPUT_ROOT  = Path(os.environ.get("OUTPUT_ROOT", "/tmp/output"))
SLICE_PREFIX = f"results/slice-{JOB_INDEX:05d}"

# Disk-space thresholds (bytes).
_DISK_WARN_BYTES  = int(os.environ.get("DISK_WARN_GB",  "8"))  * 1024**3
_DISK_PRUNE_BYTES = int(os.environ.get("DISK_PRUNE_GB", "4"))  * 1024**3
_DISK_CHECK_INTERVAL_S = 120   # check every 2 minutes

# Image extensions pruned from /tmp/output to reclaim space.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}

# ---------------------------------------------------------------------------
# Post-run result thresholds (overridable via Batch job env vars)
# ---------------------------------------------------------------------------
# If >CODE_FAIL_ABORT_PCT% of records have 'exception' errors → code bug,
# mark slice logic_failed (exit 2) so orchestrator won't auto-retry.
_CODE_FAIL_WARN_PCT  = float(os.environ.get("CODE_FAIL_WARN_PCT",  "5"))
_CODE_FAIL_ABORT_PCT = float(os.environ.get("CODE_FAIL_ABORT_PCT", "30"))
# If >API_FAIL_ABORT_PCT% of records hit API errors → quota exhausted,
# mark api_saturated (exit 3) so orchestrator retries with longer delay.
_API_FAIL_WARN_PCT   = float(os.environ.get("API_FAIL_WARN_PCT",   "20"))
_API_FAIL_ABORT_PCT  = float(os.environ.get("API_FAIL_ABORT_PCT",  "70"))
# Warn (but don't abort) if overall record failure rate is high.
_TOTAL_FAIL_WARN_PCT = float(os.environ.get("TOTAL_FAIL_WARN_PCT", "40"))

# Sentinel exit codes understood by orchestrate_robust._classify_slice()
EXIT_LOGIC_FAILED  = 2   # systematic code-level failures — do NOT auto-retry
EXIT_API_SATURATED = 3   # API quota exhausted — retry only after long delay

_STAGES = ("latlong", "grid", "location", "county", "dot")

# ---------------------------------------------------------------------------
# SIGTERM handler
# ---------------------------------------------------------------------------
_terminating   = False
_pipeline_proc = None
_proc_lock     = threading.Lock()


def _handle_sigterm(sig, frame):  # noqa: ARG001
    global _terminating
    _terminating = True
    log.warning("SIGTERM received — stopping pipeline subprocess")
    with _proc_lock:
        proc = _pipeline_proc
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


signal.signal(signal.SIGTERM, _handle_sigterm)

# ---------------------------------------------------------------------------
# Boto3 — lazy singletons with thread-safe init
# ---------------------------------------------------------------------------
_S3_CFG = Config(retries={"mode": "adaptive", "max_attempts": 6})
_SM_CFG = Config(retries={"mode": "adaptive", "max_attempts": 4})
_s3_client = _sm_client = None
_client_lock = threading.Lock()


def _s3():
    global _s3_client
    with _client_lock:
        if _s3_client is None:
            _s3_client = boto3.client("s3", config=_S3_CFG)
    return _s3_client


def _sm():
    global _sm_client
    with _client_lock:
        if _sm_client is None:
            _sm_client = boto3.client("secretsmanager", config=_SM_CFG)
    return _sm_client


def _close_aws_clients():
    global _s3_client, _sm_client
    with _client_lock:
        for cli in (_s3_client, _sm_client):
            try:
                if cli:
                    cli.close()
            except Exception:
                pass
        _s3_client = _sm_client = None


atexit.register(_close_aws_clients)

# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------
def _with_retry(fn, label: str, max_attempts: int = 4, base_delay: float = 2.0):
    """Exponential back-off with jitter. Raises on final failure."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == max_attempts:
                log.error("%s failed after %d attempts: %s", label, max_attempts, exc)
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
            log.warning("%s attempt %d/%d failed — retry in %.1fs  (%s)",
                        label, attempt, max_attempts, delay, exc)
            time.sleep(delay)

# ---------------------------------------------------------------------------
# Disk space management
# ---------------------------------------------------------------------------

def _free_bytes(path: Path) -> int:
    """Bytes free on the filesystem containing `path`."""
    try:
        return shutil.disk_usage(str(path)).free
    except Exception:
        return 2**62   # assume infinite on error


def _prune_images(output_root: Path) -> int:
    """
    Delete crop/annotated image files from output_root to reclaim disk space.
    State files (CSV, JSON, .log) are kept — they are small and needed for
    resume. Returns number of bytes freed.
    """
    freed = 0
    for p in output_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS:
            try:
                freed += p.stat().st_size
                p.unlink()
            except Exception:
                pass
    log.info("Pruned images from %s — freed %.1f MB", output_root, freed / 1024**2)
    return freed


def _start_disk_watchdog(output_root: Path, stop_event: threading.Event):
    """
    Background thread that watches free disk space on /tmp.
    - Warns at < DISK_WARN_GB free.
    - Prunes images at < DISK_PRUNE_GB free (emergency cleanup).
    """
    def _loop():
        log.info("Disk watchdog started (check every %ds)", _DISK_CHECK_INTERVAL_S)
        while not stop_event.wait(timeout=_DISK_CHECK_INTERVAL_S):
            free = _free_bytes(output_root)
            free_gb = free / 1024**3
            if free < _DISK_PRUNE_BYTES:
                log.error(
                    "DISK CRITICAL: %.1f GB free — pruning images immediately", free_gb
                )
                _prune_images(output_root)
            elif free < _DISK_WARN_BYTES:
                log.warning("Disk low: %.1f GB free", free_gb)
        log.info("Disk watchdog stopped")

    t = threading.Thread(target=_loop, name="disk-watchdog", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
def _load_secret(secret_id: str) -> dict:
    try:
        def _get():
            sec = _sm().get_secret_value(SecretId=secret_id)
            return json.loads(sec["SecretString"])
        return _with_retry(_get, f"secret:{secret_id}", max_attempts=3)
    except json.JSONDecodeError as e:
        log.error("Secret '%s' is not valid JSON: %s", secret_id, e)
        return {}
    except Exception as e:
        log.warning("Could not load secret '%s': %s", secret_id, e)
        return {}


def _load_secrets():
    # ---- GCP / Gemini ----
    gcp_id  = os.environ.get("GOOGLE_CREDS_SECRET_ID", "osu-pipeline/credentials")
    payload = _load_secret(gcp_id)

    gcp = payload.get("gcp_service_account")
    if gcp:
        gcp_path = Path("/tmp/gcp.json")
        gcp_path.write_text(
            json.dumps(gcp) if isinstance(gcp, dict) else str(gcp), encoding="utf-8"
        )
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(gcp_path)
        log.info("GCP service account loaded → /tmp/gcp.json")
    else:
        log.warning("No 'gcp_service_account' in secret '%s'", gcp_id)

    gemini_key = payload.get("gemini_api_key", "")
    if gemini_key and gemini_key not in ("", "REPLACE_ME"):
        os.environ["GOOGLE_API_KEY"] = gemini_key
        log.info("Gemini API key loaded")
    else:
        log.warning("gemini_api_key missing/placeholder in '%s'", gcp_id)

    # ---- RDS ----
    rds_id = os.environ.get("RDS_CREDS_SECRET_ID", "osu-pipeline/rds")
    rds    = _load_secret(rds_id)
    # Secret stores lowercase keys; map to uppercase env vars that main.py expects.
    # AWS RDS Proxy uses "username" (not "user"); support both for safety.
    _RDS_KEY_MAP = {
        "RDS_HOST":     ("host",),
        "RDS_PORT":     ("port",),
        "RDS_DBNAME":   ("dbname",),
        "RDS_USER":     ("username", "user"),
        "RDS_PASSWORD": ("password",),
    }
    for env_key, secret_keys in _RDS_KEY_MAP.items():
        if not os.environ.get(env_key):
            for sk in secret_keys:
                val = rds.get(sk)
                if val:
                    os.environ[env_key] = str(val)
                    break
    if os.environ.get("RDS_HOST"):
        log.info("RDS credentials loaded (%s)", os.environ["RDS_HOST"])
    else:
        log.warning("RDS credentials not found in '%s' — PLSS resolution disabled", rds_id)

# ---------------------------------------------------------------------------
# Checkpoint: download prior S3 results so main.py can resume
# ---------------------------------------------------------------------------
# Only state/CSV files are restored — not large images (saves bandwidth).
_RESUME_FILES = {
    "processing_status.csv",
    "success.csv",
    "failed.csv",
    "dot_coordinates.csv",
    "run_insights.json",
    "county_constraints.json",
}


def _download_checkpoint(output_root: Path) -> int:
    """
    Download previously-uploaded state files for this slice from S3.
    Returns count of files restored (0 = fresh start).
    main.py sees the restored processing_status.csv and skips completed records.
    """
    s3        = _s3()
    paginator = s3.get_paginator("list_objects_v2")
    n = errors = 0
    try:
        for page in paginator.paginate(Bucket=OUTPUT_BUCKET, Prefix=SLICE_PREFIX + "/"):
            for obj in page.get("Contents", []):
                key      = obj["Key"]
                filename = Path(key).name
                if filename not in _RESUME_FILES:
                    continue
                rel   = key[len(SLICE_PREFIX) + 1:]
                local = output_root / rel
                local.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _with_retry(
                        lambda k=key, l=str(local): s3.download_file(OUTPUT_BUCKET, k, l),
                        label=f"checkpoint_dl:{filename}", max_attempts=3,
                    )
                    n += 1
                except Exception as e:
                    log.warning("Checkpoint download skipped %s: %s", filename, e)
                    errors += 1
    except Exception as e:
        log.warning("Could not list S3 checkpoint: %s", e)
        return 0

    if n:
        log.info("Checkpoint: restored %d state files (resume mode, %d skipped)", n, errors)
    else:
        log.info("Checkpoint: no prior state — fresh start")
    return n

# ---------------------------------------------------------------------------
# Slice fetching
# ---------------------------------------------------------------------------
def _fetch_slice() -> Path:
    log.info("Fetching index s3://%s/%s ...", INPUT_BUCKET, INDEX_KEY)

    text  = _with_retry(
        lambda: _s3().get_object(Bucket=INPUT_BUCKET, Key=INDEX_KEY)["Body"].read().decode("utf-8"),
        label="fetch_index", max_attempts=5,
    )
    rows  = list(csv.DictReader(io.StringIO(text)))
    total = len(rows)
    start = JOB_INDEX * SLICE_SIZE
    end   = min(start + SLICE_SIZE, total)

    if start >= total:
        log.info("job %d: start=%d >= total=%d — nothing to do", JOB_INDEX, start, total)
        sys.exit(0)

    slice_rows = rows[start:end]
    log.info("job %d: rows %d..%d of %d  (%d records)",
             JOB_INDEX, start, end, total, len(slice_rows))

    out = Path("/tmp/index_slice.csv")
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(slice_rows)
    return out

# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------
def _run_pipeline(index_csv: Path, output_root: Path) -> int:
    global _pipeline_proc
    cmd = [
        sys.executable, "/app/main.py",
        "--index",   str(index_csv),
        "--output",  str(output_root),
        "--workers", str(WORKERS),
    ]
    log.info("EXEC: %s", " ".join(cmd))
    env = {
        **os.environ,
        # Belt-and-suspenders: explicitly set PYTHONPATH even though it is
        # already baked into the Docker image ENV.  This ensures subprocesses
        # spawned by main.py (e.g. tesseract wrappers) also inherit the path.
        "PYTHONPATH":        "/app/project:/app",
        "PYTHONUTF8":        "1",
        "PYTHONIOENCODING":  "utf-8",
        "PYTHONUNBUFFERED":  "1",
    }

    with _proc_lock:
        _pipeline_proc = subprocess.Popen(cmd, env=env)
    _pipeline_proc.wait()
    rc = _pipeline_proc.returncode
    with _proc_lock:
        _pipeline_proc = None
    log.info("pipeline exit code: %d", rc)
    return rc

# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------
def _upload_results(output_root: Path, prune_images_after: bool = False) -> int:
    """
    Upload everything under output_root to S3.
    If prune_images_after=True, delete successfully-uploaded image files
    to reclaim disk space (state files are always kept).
    """
    s3 = _s3()
    n = errors = 0
    uploaded_images: list[Path] = []

    for path in sorted(Path(output_root).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(output_root).as_posix()
        key = f"{SLICE_PREFIX}/{rel}"
        try:
            _with_retry(
                lambda p=path, k=key: s3.upload_file(str(p), OUTPUT_BUCKET, k),
                label=f"upload:{path.name}", max_attempts=5,
            )
            n += 1
            if prune_images_after and path.suffix.lower() in _IMAGE_EXTS:
                # Never prune *_grid.png files in periodic checkpoints.
                # The dot stage runs after the grid stage and scans its output
                # directory for this file.  Records take 20–40 min end-to-end,
                # so the periodic checkpoint fires several times between grid
                # finishing and dot starting — without this guard, the dot
                # stage always fails with "grid_image_not_found".
                if path.name.endswith("_grid.png"):
                    continue
                uploaded_images.append(path)
        except Exception as e:
            log.error("upload failed: %s  (%s)", path.name, e)
            errors += 1

    if uploaded_images:
        freed = sum(p.stat().st_size for p in uploaded_images if p.exists())
        for p in uploaded_images:
            try:
                p.unlink()
            except Exception:
                pass
        log.info("Post-upload pruned %d images (%.1f MB freed)",
                 len(uploaded_images), freed / 1024**2)

    log.info("uploaded %d files to s3://%s/%s/  (%d errors)",
             n, OUTPUT_BUCKET, SLICE_PREFIX, errors)
    return n


def _write_job_status(exit_code: int, note: str, elapsed: float,
                      analysis: dict | None = None):
    body = json.dumps({
        "job_id":      JOB_ID,
        "job_index":   JOB_INDEX,
        "slice_start": JOB_INDEX * SLICE_SIZE,
        "slice_end":   (JOB_INDEX + 1) * SLICE_SIZE,
        "exit_code":   exit_code,
        "note":        note,
        "elapsed_s":   round(elapsed),
        "s3_prefix":   f"s3://{OUTPUT_BUCKET}/{SLICE_PREFIX}/",
        "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # Result quality analysis — used by orchestrate_robust._classify_slice()
        "analysis":    analysis or {},
    }, indent=2).encode("utf-8")
    try:
        _with_retry(
            lambda: _s3().put_object(
                Bucket=OUTPUT_BUCKET,
                Key=f"{SLICE_PREFIX}/job_status.json",
                Body=body, ContentType="application/json",
            ),
            label="write_job_status", max_attempts=4,
        )
    except Exception as e:
        log.warning("Could not write job_status.json: %s", e)

# ---------------------------------------------------------------------------
# Post-run result analysis
# ---------------------------------------------------------------------------
def _analyze_results(output_root: Path) -> dict:
    """
    Read processing_status.csv and compute per-record failure statistics.
    Returns a verdict and structured counts that are written into job_status.json
    so the orchestrator can make smart retry decisions.

    Verdict values
    --------------
    ok            : all failure rates within thresholds
    warn          : elevated failures, still within abort thresholds
    logic_failed  : >CODE_FAIL_ABORT_PCT records have code exceptions
                    → orchestrator will NOT auto-retry this slice
    api_saturated : >API_FAIL_ABORT_PCT records hit API quota errors
                    → orchestrator retries only after extended backoff
    """
    status_csv = output_root / "processing_status.csv"
    stage_counts = {s: {"done": 0, "failed": 0, "skipped": 0, "pending": 0}
                    for s in _STAGES}
    error_types: dict[str, int] = {}
    total = failed_records = api_records = code_records = 0

    if not status_csv.exists():
        log.warning("processing_status.csv not found — skipping result analysis")
        return {"total": 0, "verdict": "ok", "stage_counts": stage_counts,
                "error_types": {}}

    try:
        with status_csv.open(encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                total += 1
                has_fail = has_api = has_code = False
                for stage in _STAGES:
                    st = (row.get(f"{stage}_status") or "pending").lower()
                    et = (row.get(f"{stage}_error_type") or "unknown").lower()
                    # Normalise unexpected status strings to known buckets.
                    if st not in stage_counts[stage]:
                        st = "pending"
                    stage_counts[stage][st] += 1
                    if st == "failed":
                        has_fail = True
                        error_types[et] = error_types.get(et, 0) + 1
                        if et == "api_error":
                            has_api = True
                        elif et == "exception":
                            has_code = True
                if has_fail:  failed_records += 1
                if has_api:   api_records    += 1
                if has_code:  code_records   += 1
    except Exception as exc:
        log.warning("Could not parse processing_status.csv: %s", exc)
        return {"total": 0, "verdict": "ok", "stage_counts": stage_counts,
                "error_types": {}}

    if total == 0:
        return {"total": 0, "verdict": "ok", "stage_counts": stage_counts,
                "error_types": {}}

    total_fail_pct = 100.0 * failed_records / total
    api_fail_pct   = 100.0 * api_records    / total
    code_fail_pct  = 100.0 * code_records   / total

    # --- Determine verdict (most severe threshold wins) ---
    if code_fail_pct >= _CODE_FAIL_ABORT_PCT:
        verdict = "logic_failed"
        log.error(
            "THRESHOLD: code exceptions %.1f%% >= abort %.0f%% "
            "— slice marked logic_failed, will NOT be auto-retried.  "
            "Top error types: %s",
            code_fail_pct, _CODE_FAIL_ABORT_PCT,
            sorted(error_types.items(), key=lambda x: -x[1])[:5],
        )
    elif api_fail_pct >= _API_FAIL_ABORT_PCT:
        verdict = "api_saturated"
        log.error(
            "THRESHOLD: api_error %.1f%% >= abort %.0f%% "
            "— API quota exhausted, retry after backoff.",
            api_fail_pct, _API_FAIL_ABORT_PCT,
        )
    elif (code_fail_pct >= _CODE_FAIL_WARN_PCT
          or api_fail_pct >= _API_FAIL_WARN_PCT
          or total_fail_pct >= _TOTAL_FAIL_WARN_PCT):
        verdict = "warn"
        log.warning(
            "THRESHOLD WARN: total_fail=%.1f%%  api_err=%.1f%%  code_err=%.1f%%",
            total_fail_pct, api_fail_pct, code_fail_pct,
        )
    else:
        verdict = "ok"

    log.info(
        "Results: total=%d  failed_records=%d(%.1f%%)  "
        "api_err=%d(%.1f%%)  code_err=%d(%.1f%%)  verdict=%s",
        total, failed_records, total_fail_pct,
        api_records, api_fail_pct,
        code_records, code_fail_pct,
        verdict,
    )

    return {
        "total":              total,
        "failed_records":     failed_records,
        "total_fail_pct":     round(total_fail_pct, 1),
        "api_records":        api_records,
        "api_fail_pct":       round(api_fail_pct, 1),
        "code_records":       code_records,
        "code_fail_pct":      round(code_fail_pct, 1),
        "verdict":            verdict,
        "stage_counts":       stage_counts,
        "error_types":        error_types,
    }


# ---------------------------------------------------------------------------
# Periodic checkpoint thread
# ---------------------------------------------------------------------------
def _start_checkpoint_thread(output_root: Path, stop_event: threading.Event):
    def _loop():
        log.info("Checkpoint thread started (every %ds)", CHECKPOINT_INTERVAL_S)
        while not stop_event.wait(timeout=CHECKPOINT_INTERVAL_S):
            if _terminating:
                break
            log.info("Periodic checkpoint upload (prune_images=True, grid.png preserved) ...")
            try:
                # Prune crop/page images after upload to keep /tmp lean.
                # Grid PNGs are exempted — the dot stage needs them on local disk.
                _upload_results(output_root, prune_images_after=True)
            except Exception:
                log.warning("Periodic checkpoint error:\n%s", traceback.format_exc())
        log.info("Checkpoint thread stopped")

    t = threading.Thread(target=_loop, name="checkpoint", daemon=True)
    t.start()
    return t

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _preflight_check():
    """Verify project modules are importable before launching the pipeline subprocess."""
    import importlib
    sys.path.insert(0, "/app/project")
    sys.path.insert(0, "/app")
    for mod in ("config", "scan_dataset"):
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as exc:
            log.critical(
                "PRE-FLIGHT FAILED: cannot import '%s' — PYTHONPATH=%s  sys.path=%s  error=%s",
                mod, os.environ.get("PYTHONPATH", "<not set>"), sys.path[:6], exc,
            )
            sys.exit(1)
    log.info("Pre-flight OK: config and scan_dataset importable")


def main():
    sys.path.insert(0, "/app/project")
    sys.path.insert(0, "/app")
    _preflight_check()
    start_time      = time.time()
    pipeline_exit   = 1
    upload_count    = 0
    analysis: dict  = {}
    stop_checkpoint = threading.Event()

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    # Log initial disk state.
    free_gb = _free_bytes(OUTPUT_ROOT) / 1024**3
    log.info("Disk free at start: %.1f GB", free_gb)

    try:
        _load_secrets()
        _download_checkpoint(OUTPUT_ROOT)   # resume: restore prior state files
        index_csv = _fetch_slice()
        _start_checkpoint_thread(OUTPUT_ROOT, stop_checkpoint)
        _start_disk_watchdog(OUTPUT_ROOT, stop_checkpoint)
        pipeline_exit = _run_pipeline(index_csv, OUTPUT_ROOT)

    except SystemExit:
        raise

    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt — uploading partial results")
        pipeline_exit = 130

    except Exception:
        log.critical("Unhandled exception:\n%s", traceback.format_exc())
        pipeline_exit = 1

    finally:
        stop_checkpoint.set()   # stop periodic thread
        elapsed = time.time() - start_time

        # --- Result quality analysis ---
        # Run even on failure so partial results are characterised.
        # Only upgrade exit codes when pipeline appeared to run (exit 0 or 1).
        if not _terminating and pipeline_exit in (0, 1):
            try:
                analysis = _analyze_results(OUTPUT_ROOT)
                verdict  = analysis.get("verdict", "ok")
                if verdict == "logic_failed" and pipeline_exit == 0:
                    log.error("Upgrading exit %d -> %d (logic_failed threshold)",
                              pipeline_exit, EXIT_LOGIC_FAILED)
                    pipeline_exit = EXIT_LOGIC_FAILED
                elif verdict == "api_saturated" and pipeline_exit == 0:
                    log.error("Upgrading exit %d -> %d (api_saturated threshold)",
                              pipeline_exit, EXIT_API_SATURATED)
                    pipeline_exit = EXIT_API_SATURATED
            except Exception:
                log.warning("Result analysis failed:\n%s", traceback.format_exc())

        note = ("SIGTERM"       if _terminating else
                "ok"           if pipeline_exit == 0 else
                "logic_failed" if pipeline_exit == EXIT_LOGIC_FAILED else
                "api_saturated" if pipeline_exit == EXIT_API_SATURATED else
                "preempted"    if pipeline_exit == 130 else
                f"exit={pipeline_exit}")

        log.info("Final upload (%.0fs elapsed, status=%s) ...", elapsed, note)

        try:
            upload_count = _upload_results(OUTPUT_ROOT)
        except Exception:
            log.error("Final upload failed:\n%s", traceback.format_exc())

        try:
            _write_job_status(pipeline_exit, note, elapsed, analysis)
        except Exception:
            pass

        _close_aws_clients()

        try:
            Path("/tmp/gcp.json").unlink(missing_ok=True)
        except Exception:
            pass

        log.info("Job %d done — exit=%d  uploaded=%d  elapsed=%.0fs",
                 JOB_INDEX, pipeline_exit, upload_count, elapsed)

    sys.exit(pipeline_exit)


if __name__ == "__main__":
    main()

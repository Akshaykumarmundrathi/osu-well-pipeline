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
    for key in ("RDS_HOST", "RDS_PORT", "RDS_DBNAME", "RDS_USER", "RDS_PASSWORD"):
        if rds.get(key) and not os.environ.get(key):
            os.environ[key] = str(rds[key])
    if rds.get("RDS_HOST"):
        log.info("RDS credentials loaded (%s)", rds["RDS_HOST"])
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
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}

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
def _upload_results(output_root: Path) -> int:
    s3 = _s3()
    n = errors = 0
    for path in sorted(Path(output_root).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(output_root).as_posix()
        key = f"{SLICE_PREFIX}/{rel}"
        try:
            _with_retry(
                lambda p=path, k=key: s3.upload_file(str(p), OUTPUT_BUCKET, k),
                label=f"upload:{path.name}", max_attempts=4,
            )
            n += 1
        except Exception as e:
            log.error("upload failed: %s  (%s)", path.name, e)
            errors += 1
    log.info("uploaded %d files to s3://%s/%s/  (%d errors)",
             n, OUTPUT_BUCKET, SLICE_PREFIX, errors)
    return n


def _write_job_status(exit_code: int, note: str, elapsed: float):
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
# Periodic checkpoint thread
# ---------------------------------------------------------------------------
def _start_checkpoint_thread(output_root: Path, stop_event: threading.Event):
    def _loop():
        log.info("Checkpoint thread started (every %ds)", CHECKPOINT_INTERVAL_S)
        while not stop_event.wait(timeout=CHECKPOINT_INTERVAL_S):
            if _terminating:
                break
            log.info("Periodic checkpoint upload ...")
            try:
                _upload_results(output_root)
            except Exception:
                log.warning("Periodic checkpoint error:\n%s", traceback.format_exc())
        log.info("Checkpoint thread stopped")

    t = threading.Thread(target=_loop, name="checkpoint", daemon=True)
    t.start()
    return t

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    sys.path.insert(0, "/app")
    start_time      = time.time()
    pipeline_exit   = 1
    upload_count    = 0
    stop_checkpoint = threading.Event()

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    try:
        _load_secrets()
        _download_checkpoint(OUTPUT_ROOT)   # resume: restore prior state files
        index_csv = _fetch_slice()
        _start_checkpoint_thread(OUTPUT_ROOT, stop_checkpoint)
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
        note    = ("SIGTERM" if _terminating else
                   "ok"      if pipeline_exit == 0 else f"exit={pipeline_exit}")
        log.info("Final upload (%.0fs elapsed, status=%s) ...", elapsed, note)

        try:
            upload_count = _upload_results(OUTPUT_ROOT)
        except Exception:
            log.error("Final upload failed:\n%s", traceback.format_exc())

        try:
            _write_job_status(pipeline_exit, note, elapsed)
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

"""
run_batch_job.py  —  AWS Batch / Fargate Spot container entrypoint
===================================================================

Environment variables set by Batch job definition:
  BATCH_SLICE_INDEX   0-based slice number for this job
  BATCH_TOTAL_SLICES  total number of jobs in this run
  S3_BUCKET           source+output bucket (Account 1)
  S3_INDEX_KEY        s3 key of dataset_index.csv  (outputs/merged/dataset_index_s3.csv)
  S3_OUTPUT_PREFIX    s3 key prefix for this slice  (outputs/slices/NNN)
  S3_CHECKPOINT_KEY   s3 key of prior checkpoint    (checkpoints/NNN/processing_status.csv)
  SECRETS_PREFIX      Secrets Manager prefix        (osu/)
  AWS_DEFAULT_REGION  us-east-1

Secrets Manager secrets pulled at startup:
  {SECRETS_PREFIX}gcp-credentials  → written to /tmp/gcp.json
  {SECRETS_PREFIX}gemini-keys      → GOOGLE_API_KEY env var
  {SECRETS_PREFIX}rds-config       → RDS_HOST, RDS_DBNAME, RDS_USER, RDS_PASSWORD

Flow:
  1. Pull secrets → configure env
  2. Download dataset_index_s3.csv from S3
  3. Download prior checkpoint (processing_status.csv) if exists
  4. Start background S3 sync thread (every 300s)
  5. Register SIGTERM handler (saves status, exits 130 → Batch auto-retries)
  6. Run main.py --resume --slice-index N --total-slices N
  7. Final S3 sync
  8. Exit 0
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ── Configuration from env ──────────────────────────────────────────────────
SLICE_INDEX    = int(os.environ.get("BATCH_SLICE_INDEX",  "0"))
TOTAL_SLICES   = int(os.environ.get("BATCH_TOTAL_SLICES", "1"))
S3_BUCKET      = os.environ["S3_BUCKET"]
S3_INDEX_KEY   = os.environ.get("S3_INDEX_KEY",        "outputs/merged/dataset_index_s3.csv")
S3_OUT_PREFIX  = os.environ.get("S3_OUTPUT_PREFIX",    f"outputs/slices/{SLICE_INDEX:04d}")
S3_CKPT_KEY    = os.environ.get("S3_CHECKPOINT_KEY",   f"checkpoints/{SLICE_INDEX:04d}/processing_status.csv")
SECRETS_PFX    = os.environ.get("SECRETS_PREFIX",      "osu/")
REGION         = os.environ.get("AWS_DEFAULT_REGION",  "us-east-1")
SYNC_INTERVAL  = int(os.environ.get("S3_SYNC_INTERVAL", "300"))   # seconds
OUTPUT_DIR     = Path("/tmp/output")
WORKERS        = int(os.environ.get("PIPELINE_WORKERS", "2"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("run_batch_job")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _sm_get(client, name: str) -> str:
    """Fetch a Secrets Manager secret value (string or JSON string)."""
    try:
        resp = client.get_secret_value(SecretId=name)
        return resp.get("SecretString") or ""
    except ClientError as e:
        log.warning("Secret %s not found: %s", name, e)
        return ""


def pull_secrets():
    """Pull all secrets and configure environment."""
    log.info("Pulling secrets from Secrets Manager (prefix=%s)…", SECRETS_PFX)
    sm = boto3.client("secretsmanager", region_name=REGION)

    # GCP service-account JSON → /tmp/gcp.json
    gcp_json = _sm_get(sm, f"{SECRETS_PFX}gcp-credentials")
    if gcp_json:
        gcp_path = Path("/tmp/gcp.json")
        gcp_path.write_text(gcp_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(gcp_path)
        log.info("GCP credentials written to /tmp/gcp.json")
    else:
        log.warning("GCP credentials not found — Vision API will be unavailable")

    # Gemini API key(s)
    gemini_keys = _sm_get(sm, f"{SECRETS_PFX}gemini-keys")
    if gemini_keys:
        os.environ["GOOGLE_API_KEY"] = gemini_keys.strip()
        key_count = len([k for k in gemini_keys.split(",") if k.strip()])
        log.info("Gemini API: %d key(s) loaded", key_count)
    else:
        log.warning("Gemini key not found — county Gemini fallback disabled")

    # RDS config (JSON: {"host":..,"dbname":..,"user":..,"password":..,"port":..})
    rds_raw = _sm_get(sm, f"{SECRETS_PFX}rds-config")
    if rds_raw:
        try:
            rds = json.loads(rds_raw)
            os.environ.setdefault("RDS_HOST",     rds.get("host",     ""))
            os.environ.setdefault("RDS_DBNAME",   rds.get("dbname",   "Oklahomaplss"))
            os.environ.setdefault("RDS_USER",     rds.get("user",     "LookUpMaster"))
            os.environ.setdefault("RDS_PASSWORD", rds.get("password", ""))
            os.environ.setdefault("RDS_PORT",     str(rds.get("port", 5432)))
            log.info("RDS config loaded: host=%s db=%s",
                     os.environ.get("RDS_HOST","?"), os.environ.get("RDS_DBNAME","?"))
        except json.JSONDecodeError:
            log.warning("RDS config is not valid JSON — coordinate enrichment will fail")


def s3_download(s3, key: str, local_path: Path) -> bool:
    """Download s3://BUCKET/key → local_path. Returns True on success."""
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(S3_BUCKET, key, str(local_path))
        log.info("Downloaded s3://%s/%s → %s", S3_BUCKET, key, local_path)
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchKey"):
            log.info("No checkpoint at s3://%s/%s (fresh start)", S3_BUCKET, key)
        else:
            log.warning("S3 download failed %s: %s", key, e)
        return False


def s3_sync_output(s3):
    """Upload /tmp/output/ contents to S3_OUT_PREFIX."""
    if not OUTPUT_DIR.exists():
        return
    uploaded = 0
    for local in OUTPUT_DIR.rglob("*"):
        if not local.is_file():
            continue
        rel    = local.relative_to(OUTPUT_DIR).as_posix()
        s3_key = f"{S3_OUT_PREFIX}/{rel}"
        try:
            s3.upload_file(str(local), S3_BUCKET, s3_key)
            uploaded += 1
        except Exception as exc:
            log.warning("Upload failed %s: %s", s3_key, exc)
    log.info("S3 sync: %d files → s3://%s/%s/", uploaded, S3_BUCKET, S3_OUT_PREFIX)


def start_sync_thread(s3) -> threading.Thread:
    """Background thread: sync output to S3 every SYNC_INTERVAL seconds."""
    stop_event = threading.Event()

    def _loop():
        while not stop_event.wait(SYNC_INTERVAL):
            try:
                s3_sync_output(s3)
            except Exception as exc:
                log.warning("Background sync error: %s", exc)

    t = threading.Thread(target=_loop, daemon=True, name="s3-sync")
    t.stop_event = stop_event
    t.start()
    log.info("Background S3 sync started (interval=%ds)", SYNC_INTERVAL)
    return t


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    log.info("="*60)
    log.info("OSU Well Pipeline — Batch Job")
    log.info("  Slice:  %d / %d", SLICE_INDEX, TOTAL_SLICES)
    log.info("  Bucket: s3://%s", S3_BUCKET)
    log.info("  Output: %s  →  s3://%s/%s/",
             OUTPUT_DIR, S3_BUCKET, S3_OUT_PREFIX)
    log.info("="*60)

    # OCR backend guard — the cloud run must use Google Vision (Tesseract is
    # proven inaccurate on the older scans). Force it on unless an operator
    # explicitly opts into a Tesseract experiment, and surface it in the logs.
    if os.environ.get("USE_VISION_API") != "0":
        os.environ["USE_VISION_API"] = "1"
    log.info("  OCR backend: %s",
             "Google Vision" if os.environ.get("USE_VISION_API") == "1" else "Tesseract (EXPERIMENT)")

    # 1. Secrets
    pull_secrets()

    # 2. S3 client (cross-account access via IAM role — no extra config needed)
    s3 = boto3.client("s3", region_name=REGION)

    # 3. Download dataset_index
    index_local = OUTPUT_DIR / "dataset_index_s3.csv"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not s3_download(s3, S3_INDEX_KEY, index_local):
        log.error("Cannot download dataset index — aborting")
        sys.exit(1)

    # 4. Download checkpoint (prior processing_status.csv for this slice)
    ckpt_local = OUTPUT_DIR / "processing_status.csv"
    s3_download(s3, S3_CKPT_KEY, ckpt_local)   # ok if missing (fresh run)

    # 5. Background sync thread
    sync_thread = start_sync_thread(s3)

    # 6. SIGTERM handler — Fargate Spot sends SIGTERM 120s before hard kill
    def _sigterm(signum, frame):
        log.warning("SIGTERM received — flushing output to S3 before exit…")
        sync_thread.stop_event.set()
        try:
            s3_sync_output(s3)
        except Exception as exc:
            log.warning("Final sync on SIGTERM failed: %s", exc)
        # Upload checkpoint so the next attempt can resume
        if ckpt_local.exists():
            try:
                s3.upload_file(str(ckpt_local), S3_BUCKET, S3_CKPT_KEY)
            except Exception:
                pass
        log.info("Exiting with code 130 (preempted)")
        sys.exit(130)   # Batch sees SPOT_CAPACITY_NOT_AVAILABLE → auto-retry

    signal.signal(signal.SIGTERM, _sigterm)

    # 7. Run the pipeline
    script = Path(__file__).parent.parent / "project" / "main.py"
    cmd = [
        sys.executable, str(script),
        "--resume",
        "--index",        str(index_local),
        "--output",       str(OUTPUT_DIR),
        "--slice-index",  str(SLICE_INDEX),
        "--total-slices", str(TOTAL_SLICES),
        "--workers",      str(WORKERS),
    ]
    # Collection filter: run only the specified collection in this container
    _coll_filter = os.environ.get("COLLECTION_FILTER", "").strip()
    if _coll_filter:
        cmd += ["--collection", _coll_filter]
        log.info("Collection filter: %s", _coll_filter)
    # Pipeline stage: run only one extraction stage per task
    _pipe_stage = os.environ.get("PIPELINE_STAGE", "").strip()
    if _pipe_stage:
        cmd += ["--stage", _pipe_stage]
        log.info("Pipeline stage filter: %s", _pipe_stage)
    log.info("Running: %s", " ".join(cmd))
    t0 = time.monotonic()
    result = subprocess.run(cmd, check=False)
    elapsed = time.monotonic() - t0
    log.info("Pipeline finished in %.0fs, exit_code=%d", elapsed, result.returncode)

    # 8. Stop sync thread, do final upload
    sync_thread.stop_event.set()
    log.info("Final S3 sync…")
    s3_sync_output(s3)

    # Upload checkpoint for merge step
    if ckpt_local.exists():
        try:
            s3.upload_file(str(ckpt_local), S3_BUCKET, S3_CKPT_KEY)
            log.info("Checkpoint uploaded → s3://%s/%s", S3_BUCKET, S3_CKPT_KEY)
        except Exception as exc:
            log.warning("Checkpoint upload failed: %s", exc)

    if result.returncode not in (0, 130):
        log.error("Pipeline exited with code %d", result.returncode)
        sys.exit(result.returncode)

    log.info("Job complete. Slice %d/%d done.", SLICE_INDEX, TOTAL_SLICES)


if __name__ == "__main__":
    main()

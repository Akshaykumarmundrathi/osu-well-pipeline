"""
run_enrich_job.py  —  AWS Batch enrichment container entrypoint
===============================================================

Merges all per-slice processing_status.csv files from S3, runs
run_coord_enrichment.py against RDS, and uploads the final
dot_coordinates.csv back to S3.

Environment variables (set by orchestrate.py job submission):
  S3_BUCKET           source+output bucket
  S3_OUTPUT_PREFIX    prefix for merged outputs  (outputs/merged)
  SECRETS_PREFIX      Secrets Manager prefix  (osu/)
  AWS_DEFAULT_REGION  us-east-1

Flow:
  1. Pull secrets → configure RDS env vars
  2. Download all outputs/slices/*/processing_status.csv from S3
  3. Merge into single processing_status.csv in /tmp/output/
  4. Run run_coord_enrichment.py  (reads /tmp/output/processing_status.csv)
  5. Upload dot_coordinates.csv + coord_resolution_failures.csv to S3
  6. Exit 0
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ── Configuration ────────────────────────────────────────────────────────────
S3_BUCKET     = os.environ["S3_BUCKET"]
S3_OUT_PREFIX = os.environ.get("S3_OUTPUT_PREFIX",   "outputs/merged")
SECRETS_PFX   = os.environ.get("SECRETS_PREFIX",     "osu/")
REGION        = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
OUTPUT_DIR    = Path("/tmp/output")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("run_enrich_job")


# ── Secrets ──────────────────────────────────────────────────────────────────

def pull_secrets():
    log.info("Pulling secrets (prefix=%s)…", SECRETS_PFX)
    sm = boto3.client("secretsmanager", region_name=REGION)

    def _get(name: str) -> str:
        try:
            return sm.get_secret_value(SecretId=name).get("SecretString", "") or ""
        except ClientError as e:
            log.warning("Secret %s not found: %s", name, e)
            return ""

    # RDS credentials
    rds_raw = _get(f"{SECRETS_PFX}rds-config")
    if rds_raw:
        try:
            rds = json.loads(rds_raw)
            os.environ.setdefault("RDS_HOST",     rds.get("host",     ""))
            os.environ.setdefault("RDS_DBNAME",   rds.get("dbname",   "Oklahomaplss"))
            os.environ.setdefault("RDS_USER",     rds.get("user",     "LookUpMaster"))
            os.environ.setdefault("RDS_PASSWORD", rds.get("password", ""))
            os.environ.setdefault("RDS_PORT",     str(rds.get("port", 5432)))
            log.info("RDS config: host=%s db=%s",
                     os.environ.get("RDS_HOST", "?"), os.environ.get("RDS_DBNAME", "?"))
        except json.JSONDecodeError:
            log.error("RDS config secret is not valid JSON — enrichment will fail")
    else:
        log.error("RDS config secret missing — enrichment will fail")

    # Gemini key (optional — county fallback)
    gemini = _get(f"{SECRETS_PFX}gemini-keys")
    if gemini:
        os.environ["GOOGLE_API_KEY"] = gemini.strip()
        log.info("Gemini keys loaded")


# ── S3 merge ─────────────────────────────────────────────────────────────────

def merge_slice_csvs(s3) -> Path:
    """
    Download all  outputs/slices/*/processing_status.csv  from S3,
    merge into /tmp/output/processing_status.csv and return its path.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    merged_path = OUTPUT_DIR / "processing_status.csv"

    log.info("Listing slice CSVs under s3://%s/outputs/slices/ …", S3_BUCKET)
    pag = s3.get_paginator("list_objects_v2")

    fieldnames = None
    row_count  = 0

    import csv
    import io

    out_fh   = None
    writer   = None

    for page in pag.paginate(Bucket=S3_BUCKET, Prefix="outputs/slices/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/processing_status.csv"):
                continue
            try:
                resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
                text = resp["Body"].read().decode("utf-8", errors="replace")
                reader = csv.DictReader(io.StringIO(text))
                slice_rows = list(reader)
                if not slice_rows:
                    continue
                if fieldnames is None:
                    fieldnames = reader.fieldnames or []
                    out_fh = open(merged_path, "w", newline="", encoding="utf-8")
                    writer = csv.DictWriter(out_fh, fieldnames=fieldnames,
                                           extrasaction="ignore")
                    writer.writeheader()
                for row in slice_rows:
                    writer.writerow(row)
                    row_count += 1
                log.info("  merged %d rows from %s", len(slice_rows), key)
            except Exception as exc:
                log.warning("  failed to read %s: %s", key, exc)

    if out_fh:
        out_fh.close()

    log.info("Merged %d total rows → %s", row_count, merged_path)
    return merged_path


# ── Upload outputs ────────────────────────────────────────────────────────────

def upload_file(s3, local: Path, s3_key: str):
    try:
        s3.upload_file(str(local), S3_BUCKET, s3_key)
        log.info("Uploaded %s → s3://%s/%s", local.name, S3_BUCKET, s3_key)
    except Exception as exc:
        log.warning("Upload failed %s: %s", s3_key, exc)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("OSU Well Pipeline — Enrichment Job")
    log.info("  Bucket: s3://%s", S3_BUCKET)
    log.info("  Output: %s  →  s3://%s/%s/", OUTPUT_DIR, S3_BUCKET, S3_OUT_PREFIX)
    log.info("=" * 60)

    # 1. Secrets
    pull_secrets()

    # 2. S3 client
    s3 = boto3.client("s3", region_name=REGION)

    # 3. Merge all slice processing_status.csv files
    merged_csv = merge_slice_csvs(s3)
    if not merged_csv.exists() or merged_csv.stat().st_size == 0:
        log.error("No merged CSV produced — nothing to enrich. Exiting.")
        sys.exit(1)

    # Upload merged CSV now (so it's available even if enrichment fails)
    upload_file(s3, merged_csv,
                f"{S3_OUT_PREFIX}/processing_status.csv")

    # 4. Run coord enrichment
    script = Path(__file__).parent.parent / "project" / "run_coord_enrichment.py"
    cmd = [
        sys.executable, str(script),
        "--output", str(OUTPUT_DIR),
        "--all-dot-done",        # bypass manual-review gate
        "--include-centroid",    # use section centroid for PLSS-complete no-dot records
    ]
    log.info("Running enrichment: %s", " ".join(cmd))
    t0     = time.monotonic()
    result = subprocess.run(cmd, check=False)
    elapsed = time.monotonic() - t0
    log.info("Enrichment finished in %.0fs, exit_code=%d", elapsed, result.returncode)

    if result.returncode not in (0,):
        log.error("Enrichment script returned non-zero exit code %d", result.returncode)
        # Still upload whatever partial outputs exist

    # 5. Upload enrichment outputs
    for fname in [
        "dot_coordinates.csv",
        "coord_resolution_failures.csv",
        "coord_resolution_log.csv",
    ]:
        local = OUTPUT_DIR / fname
        if local.exists():
            upload_file(s3, local, f"{S3_OUT_PREFIX}/{fname}")
        else:
            log.warning("Expected output not found: %s", fname)

    if result.returncode != 0:
        sys.exit(result.returncode)

    log.info("Enrichment job complete.")


if __name__ == "__main__":
    main()

"""
Entrypoint for one AWS Batch array-job task.

Each task processes a SLICE of the master dataset_index.csv:
  start = AWS_BATCH_JOB_ARRAY_INDEX * SLICE_SIZE
  end   = start + SLICE_SIZE

Reads:
  s3://INPUT_BUCKET/INDEX_KEY                    -- master dataset_index.csv
  s3://INPUT_BUCKET/zips/*.zip                   -- PDFs (via record.zip_path)
  Secrets Manager: GOOGLE_CREDS_SECRET_ID        -- JSON with:
      gcp_service_account: <full GCP service-account JSON>
      gemini_api_key:      <Gemini API key string>

Writes:
  s3://OUTPUT_BUCKET/results/slice-XXXXX/...     -- everything under
       /tmp/output (success.csv, failed.csv, processing_status.csv,
       run_insights.md/json, metadata/, grids/, locations/, counties/,
       logs/, manual_review/)
"""

import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import boto3

# -----------------------------------------------------------------------------
# Job parameters from AWS Batch environment
# -----------------------------------------------------------------------------
INPUT_BUCKET  = os.environ["INPUT_BUCKET"]
OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]
INDEX_KEY     = os.environ["INDEX_KEY"]                  # key inside INPUT_BUCKET
JOB_INDEX     = int(os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", "0"))
SLICE_SIZE    = int(os.environ.get("SLICE_SIZE",  "1000"))
WORKERS       = int(os.environ.get("WORKERS",     "8"))


def _load_secrets():
    """
    Pull GCP creds + Gemini API key from Secrets Manager (if configured)
    and inject them into the env exactly the way the pipeline expects.
    """
    secret_id = os.environ.get("GOOGLE_CREDS_SECRET_ID")
    if not secret_id:
        print("WARNING: GOOGLE_CREDS_SECRET_ID not set; skipping secret load")
        return
    sm = boto3.client("secretsmanager")
    sec = sm.get_secret_value(SecretId=secret_id)
    payload = json.loads(sec["SecretString"])

    gcp = payload.get("gcp_service_account")
    if gcp:
        path = Path("/tmp/gcp.json")
        path.write_text(
            json.dumps(gcp) if isinstance(gcp, dict) else str(gcp),
            encoding="utf-8",
        )
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)

    if "gemini_api_key" in payload:
        os.environ["GOOGLE_API_KEY"] = payload["gemini_api_key"]


def _fetch_slice() -> Path:
    """
    Download the master index from S3, write a CSV containing only this
    job's slice of records to /tmp, return the path.
    """
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=INPUT_BUCKET, Key=INDEX_KEY)
    text = obj["Body"].read().decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    total = len(rows)

    start = JOB_INDEX * SLICE_SIZE
    end   = min(start + SLICE_SIZE, total)
    if start >= total:
        print(f"job {JOB_INDEX}: start={start} >= total={total} (nothing to do)")
        sys.exit(0)
    slice_rows = rows[start:end]
    print(f"job {JOB_INDEX}: rows {start:,}..{end:,} of {total:,} "
          f"({len(slice_rows)} records)")

    out = Path("/tmp/index_slice.csv")
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(slice_rows)
    return out


def _run_pipeline(index_csv: Path, output_root: Path):
    """Invoke main.py for this slice. Inherits the env we just populated."""
    cmd = [
        "python", "/app/main.py",
        "--index",  str(index_csv),
        "--output", str(output_root),
        "--workers", str(WORKERS),
    ]
    print("EXEC:", " ".join(cmd))
    subprocess.run(cmd, check=False)   # don't fail the whole job on per-record errors


def _upload_results(output_root: Path):
    """Push every output file to s3://OUTPUT_BUCKET/results/slice-XXXXX/..."""
    from utils.s3_reader import upload_directory
    prefix = f"results/slice-{JOB_INDEX:05d}"
    n = upload_directory(output_root, OUTPUT_BUCKET, prefix)
    print(f"uploaded {n:,} files to s3://{OUTPUT_BUCKET}/{prefix}/")


def main():
    sys.path.insert(0, "/app")   # so utils.s3_reader is importable

    _load_secrets()
    index_csv = _fetch_slice()

    output_root = Path("/tmp/output")
    output_root.mkdir(parents=True, exist_ok=True)

    _run_pipeline(index_csv, output_root)
    _upload_results(output_root)
    print("job complete.")


if __name__ == "__main__":
    main()

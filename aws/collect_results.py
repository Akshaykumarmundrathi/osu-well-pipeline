"""
collect_results.py  —  Download and merge all slice outputs from S3
====================================================================

After all Batch jobs complete, this script:
  1. Downloads all per-slice processing_status.csv files → merges locally
  2. Downloads dot_coordinates.csv from outputs/merged/
  3. Optionally downloads grids/, metadata/, logs/ (large — off by default)

Useful for local analysis of the cloud run, or before running build_map_data.py
locally.

Usage:
    python aws/collect_results.py                          # CSVs only
    python aws/collect_results.py --all                    # everything (slow)
    python aws/collect_results.py --output D:\my_results   # custom local dir
    python aws/collect_results.py --dry-run                # list only, no download
    python aws/collect_results.py --merged-only            # only outputs/merged/
"""

import argparse
import csv
import io
import os
import sys
from pathlib import Path

import boto3

REGION    = "us-east-1"
ACCOUNT1  = os.environ["ACCOUNT1_ID"]
BUCKET    = os.environ.get("S3_BUCKET") or f"osu-well-records-{ACCOUNT1}"

_DEFAULT_OUTPUT = Path(os.environ.get(
    "OUTPUT_ROOT",
    str(Path(__file__).parent.parent / "project_outputs_cloud"),
))


def _p(msg): print(msg, flush=True)


def merge_slice_csvs(s3, local_dir: Path, dry_run: bool) -> int:
    """
    Download + merge all  outputs/slices/*/processing_status.csv  into
    local_dir/processing_status.csv.
    Returns number of rows merged.
    """
    local_dir.mkdir(parents=True, exist_ok=True)
    merged_path = local_dir / "processing_status.csv"

    pag = s3.get_paginator("list_objects_v2")
    fieldnames = None
    row_count  = 0
    out_fh     = None
    writer     = None

    _p(f"Scanning s3://{BUCKET}/outputs/slices/ for slice CSVs…")
    for page in pag.paginate(Bucket=BUCKET, Prefix="outputs/slices/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/processing_status.csv"):
                continue
            if dry_run:
                _p(f"  [dry-run] would download {key}")
                continue
            try:
                resp = s3.get_object(Bucket=BUCKET, Key=key)
                text = resp["Body"].read().decode("utf-8", errors="replace")
                reader = csv.DictReader(io.StringIO(text))
                rows   = list(reader)
                if not rows:
                    continue
                if fieldnames is None:
                    fieldnames = reader.fieldnames or []
                    out_fh = open(merged_path, "w", newline="", encoding="utf-8")
                    writer = csv.DictWriter(out_fh, fieldnames=fieldnames,
                                           extrasaction="ignore")
                    writer.writeheader()
                for row in rows:
                    writer.writerow(row)
                    row_count += 1
            except Exception as exc:
                _p(f"  WARN: failed to read {key}: {exc}")

    if out_fh:
        out_fh.close()

    if not dry_run:
        _p(f"Merged {row_count:,} rows → {merged_path}")
    return row_count


def download_merged(s3, local_dir: Path, dry_run: bool):
    """Download everything under outputs/merged/ (CSVs + merged artifacts)."""
    pag = s3.get_paginator("list_objects_v2")
    local_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for page in pag.paginate(Bucket=BUCKET, Prefix="outputs/merged/"):
        for obj in page.get("Contents", []):
            key  = obj["Key"]
            # Relative path under outputs/merged/
            rel  = key[len("outputs/merged/"):]
            if not rel:
                continue
            local = local_dir / rel
            if dry_run:
                _p(f"  [dry-run] would download s3://{BUCKET}/{key}")
                count += 1
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            try:
                s3.download_file(BUCKET, key, str(local))
                count += 1
            except Exception as exc:
                _p(f"  WARN: {key}: {exc}")
    _p(f"Downloaded {count} files from outputs/merged/")


def download_prefix(s3, s3_prefix: str, local_dir: Path, dry_run: bool,
                    max_files: int | None = None):
    """Download all objects under s3_prefix into local_dir (mirroring structure)."""
    pag = s3.get_paginator("list_objects_v2")
    local_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for page in pag.paginate(Bucket=BUCKET, Prefix=s3_prefix):
        for obj in page.get("Contents", []):
            if max_files and count >= max_files:
                _p(f"  (stopped at {max_files} files)")
                return count
            key = obj["Key"]
            rel = key[len(s3_prefix.rstrip("/") + "/"):]
            if not rel:
                continue
            local = local_dir / rel
            if dry_run:
                _p(f"  [dry-run] {key}")
                count += 1
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            try:
                s3.download_file(BUCKET, key, str(local))
                count += 1
                if count % 1000 == 0:
                    _p(f"  … {count:,} files downloaded")
            except Exception as exc:
                _p(f"  WARN: {key}: {exc}")
    return count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile",  default=os.environ.get("AWS_PROFILE", "akshay"),
                    help="AWS profile (Account 1 — where the bucket lives)")
    ap.add_argument("--output",   default=str(_DEFAULT_OUTPUT),
                    help="Local directory for downloaded files")
    ap.add_argument("--dry-run",  action="store_true",
                    help="List what would be downloaded without downloading")
    ap.add_argument("--merged-only", action="store_true",
                    help="Only download outputs/merged/ (skips per-slice merging)")
    ap.add_argument("--all",      action="store_true",
                    help="Download all outputs including grids, metadata, logs (large)")
    ap.add_argument("--grids",    action="store_true", help="Download grid PNGs")
    ap.add_argument("--metadata", action="store_true", help="Download metadata JSONs")
    ap.add_argument("--logs",     action="store_true", help="Download per-PDF logs")
    args = ap.parse_args()

    local_dir = Path(args.output)
    _p(f"Collecting results from s3://{BUCKET}")
    _p(f"  → {local_dir}")
    if args.dry_run:
        _p("  [DRY RUN — no files will be downloaded]")

    sess = boto3.Session(profile_name=args.profile, region_name=REGION)
    s3   = sess.client("s3")

    if not args.merged_only:
        merge_slice_csvs(s3, local_dir, args.dry_run)

    # Always download merged/ outputs (dot_coordinates.csv etc.)
    download_merged(s3, local_dir, args.dry_run)

    if args.all or args.grids:
        _p("\nDownloading grid PNGs…")
        n = download_prefix(s3, "outputs/slices/", local_dir / "grids_all",
                            args.dry_run)
        _p(f"  {n:,} grid files")

    if args.all or args.metadata:
        _p("\nDownloading metadata JSONs…")
        n = download_prefix(s3, "outputs/slices/", local_dir / "metadata_all",
                            args.dry_run)
        _p(f"  {n:,} metadata files")

    if args.all or args.logs:
        _p("\nDownloading per-PDF logs…")
        n = download_prefix(s3, "outputs/slices/", local_dir / "logs_all",
                            args.dry_run)
        _p(f"  {n:,} log files")

    _p("\nDone.")


if __name__ == "__main__":
    main()

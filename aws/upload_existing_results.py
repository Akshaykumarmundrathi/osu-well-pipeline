"""
upload_existing_results.py
==========================
One-time script: upload D:\project_outputs\ to S3 Account 1 so the
cloud pipeline can resume from existing processed records.

Usage:
    python aws/upload_existing_results.py
    python aws/upload_existing_results.py --dry-run
    python aws/upload_existing_results.py --only-csv       # just the CSVs, skip images
    python aws/upload_existing_results.py --source D:\project_outputs_local

What it uploads (Account 1 bucket):
    processing_status.csv        -> s3://bucket/outputs/merged/processing_status.csv
    success.csv / failed.csv     -> s3://bucket/outputs/merged/
    dot_locations.csv            -> s3://bucket/outputs/merged/
    dot_coordinates.csv          -> s3://bucket/outputs/merged/      (if exists)
    metadata/**                  -> s3://bucket/outputs/merged/metadata/
    logs/**                      -> s3://bucket/outputs/merged/logs/
    grids/**                     -> s3://bucket/outputs/merged/grids/
    counties/**                  -> s3://bucket/outputs/merged/counties/
    locations/**                 -> s3://bucket/outputs/merged/locations/
    dots/**                      -> s3://bucket/outputs/merged/dots/
"""

import argparse
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

BUCKET      = os.environ.get("S3_BUCKET") or os.environ["INPUT_BUCKET"]
KEY_PREFIX  = "outputs/merged"
ACCOUNT_1_PROFILE = os.environ.get("AWS_PROFILE_ACCOUNT1", "akshay")  # aws profile for account 1

# Only upload these extensions (skip .tmp, .pid, etc.)
ALLOWED_EXTS = {".csv", ".json", ".png", ".log", ".md", ".txt", ".jsonl"}

# Folders to always include (relative to source root)
INCLUDE_DIRS = {"metadata", "logs", "grids", "counties", "locations", "dots", "manual_review"}

# Top-level files to always include
INCLUDE_FILES = {
    "processing_status.csv", "success.csv", "dot_locations.csv",
    "latlong_records.csv",   "dot_coordinates.csv",
    "coord_resolution_failures.csv", "coord_resolution_log.csv",
    "failure_analysis.csv",  "run_insights.json", "run_insights.md",
    "county_constraints.json",
}


def upload_file(s3, local_path: Path, s3_key: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"  DRY  {s3_key}")
        return True
    try:
        s3.upload_file(str(local_path), BUCKET, s3_key)
        return True
    except ClientError as e:
        print(f"  FAIL {s3_key}: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source",
                    default=os.environ.get("OUTPUT_ROOT", r"D:\project_outputs"))
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--only-csv", action="store_true", help="Skip images/logs, only upload CSVs+JSONs")
    ap.add_argument("--prefix",   default=KEY_PREFIX)
    args = ap.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"ERROR: source {source} does not exist"); sys.exit(1)

    # Use Account 1 profile (or default credentials if cross-account role assumed)
    session = boto3.Session(profile_name=ACCOUNT_1_PROFILE) if ACCOUNT_1_PROFILE else boto3.Session()
    s3 = session.client("s3", region_name="us-east-1")

    # Verify bucket access
    try:
        s3.head_bucket(Bucket=BUCKET)
        print(f"Bucket access OK: s3://{BUCKET}")
    except ClientError as e:
        print(f"Cannot access s3://{BUCKET}: {e}")
        print("Make sure AWS credentials for Account 1 are configured.")
        print(f"  Set AWS_PROFILE_ACCOUNT1 env var to your Account 1 profile name.")
        sys.exit(1)

    total_files = uploaded = skipped = failed = 0
    total_bytes = 0

    print(f"\nSource:  {source}")
    print(f"Target:  s3://{BUCKET}/{args.prefix}/")
    print(f"Dry run: {args.dry_run}\n")

    # Upload top-level files
    for f in source.iterdir():
        if not f.is_file(): continue
        if f.name not in INCLUDE_FILES: continue
        if args.only_csv and f.suffix not in (".csv", ".json", ".md", ".txt"): continue
        s3_key = f"{args.prefix}/{f.name}"
        total_files += 1; total_bytes += f.stat().st_size
        if upload_file(s3, f, s3_key, args.dry_run):
            print(f"  OK   {f.name}  ({f.stat().st_size/1024/1024:.1f} MB)")
            uploaded += 1
        else:
            failed += 1

    # Upload subdirectories
    for dir_name in INCLUDE_DIRS:
        sub = source / dir_name
        if not sub.exists(): continue
        for local_path in sub.rglob("*"):
            if not local_path.is_file(): continue
            if local_path.suffix not in ALLOWED_EXTS: continue
            if args.only_csv and local_path.suffix not in (".csv", ".json"): continue
            rel = local_path.relative_to(source).as_posix()
            s3_key = f"{args.prefix}/{rel}"
            total_files += 1; total_bytes += local_path.stat().st_size
            if upload_file(s3, local_path, s3_key, args.dry_run):
                uploaded += 1
            else:
                failed += 1
        if not args.dry_run:
            print(f"  {dir_name}/  uploaded")

    print(f"\n{'DRY RUN SUMMARY' if args.dry_run else 'UPLOAD COMPLETE'}")
    print(f"  Files:  {uploaded:,} uploaded  {failed:,} failed  (of {total_files:,} total)")
    print(f"  Size:   {total_bytes/1024/1024:.0f} MB")
    print(f"  S3:     s3://{BUCKET}/{args.prefix}/")


if __name__ == "__main__":
    main()

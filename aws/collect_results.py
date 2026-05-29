"""
aws/collect_results.py — Download Batch results + rebuild the GitHub Pages map.
================================================================================

Run after orchestrate_robust.py finishes (all slices done).

Step 1 — Download:
  Downloads every results/slice-NNNNN/processing_status.csv from Account 2 S3
  into a local merge directory.

Step 2 — Merge:
  Merges all slice CSVs into a single processing_status.csv (deduplicating
  by pdf_stem, last-write wins).

Step 3 — Enrich:
  Runs run_coord_enrichment.py --all-dot-done --include-centroid to resolve
  lat/lon coordinates from the PLSS RDS database.

Step 4 — Rebuild map:
  Runs build_map_data.py --push to regenerate well_locations.json and push
  to GitHub Pages.

Usage
-----
    python aws/collect_results.py --profile mano
    python aws/collect_results.py --profile mano --skip-enrich   # download + merge only
    python aws/collect_results.py --profile mano --skip-push     # skip git push
    python aws/collect_results.py --profile mano --dry-run       # count only, no download
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.config import Config

# ---------------------------------------------------------------------------
# Config (all overridable via env vars / .env.account2)
# ---------------------------------------------------------------------------
OUTPUT_BUCKET   = os.environ.get("OUTPUT_BUCKET", "osu-pipeline-results-mano")
REGION          = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

_LOCAL_OUTPUT   = Path(os.environ.get("LOCAL_OUTPUT", r"D:\project_outputs_local"))
_MERGE_DIR      = _LOCAL_OUTPUT / "_s3_merge"
_STATUS_CSV_OUT = _LOCAL_OUTPUT / "processing_status.csv"

_PROJECT_DIR    = Path(__file__).parent.parent / "project"
_CFG            = Config(retries={"mode": "adaptive", "max_attempts": 6})


# ---------------------------------------------------------------------------
# S3 download
# ---------------------------------------------------------------------------

def _s3_client(profile: str | None) -> boto3.client:
    sess = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return sess.client("s3", region_name=REGION, config=_CFG)


def download_results(s3, dry_run: bool) -> int:
    """Download all processing_status.csv files from S3. Returns count."""
    print(f"\n[1/4] Downloading slice CSVs from s3://{OUTPUT_BUCKET}/results/ ...")

    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=OUTPUT_BUCKET, Prefix="results/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/processing_status.csv"):
                keys.append(key)

    print(f"  Found {len(keys)} slice CSVs in S3.")
    if dry_run:
        print(f"  [DRY RUN] would download {len(keys)} files.")
        return len(keys)

    _MERGE_DIR.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    for key in keys:
        # key = results/slice-00001/processing_status.csv
        slice_id = key.split("/")[1]           # slice-00001
        local    = _MERGE_DIR / slice_id / "processing_status.csv"
        local.parent.mkdir(parents=True, exist_ok=True)
        if local.exists():
            # Quick size check to skip already-downloaded files
            remote_size = s3.head_object(Bucket=OUTPUT_BUCKET, Key=key)["ContentLength"]
            if local.stat().st_size == remote_size:
                continue
        obj = s3.get_object(Bucket=OUTPUT_BUCKET, Key=key)
        local.write_bytes(obj["Body"].read())
        downloaded += 1
        if downloaded % 50 == 0:
            print(f"    {downloaded}/{len(keys)} downloaded ...", flush=True)

    total_local = len(list(_MERGE_DIR.rglob("processing_status.csv")))
    print(f"  Downloaded {downloaded} new files.  Total local: {total_local}")
    return total_local


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_csvs(dry_run: bool) -> int:
    """Merge all slice processing_status.csv into one. Returns row count."""
    print(f"\n[2/4] Merging slice CSVs ...")

    slice_files = sorted(_MERGE_DIR.rglob("processing_status.csv"))
    if not slice_files:
        print(f"  No slice CSVs found in {_MERGE_DIR}. Run download step first.")
        return 0

    print(f"  Merging {len(slice_files)} slice files ...")

    rows_by_stem: dict[str, dict] = {}
    fieldnames: list[str] = []

    for f in slice_files:
        try:
            with f.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                if not fieldnames and reader.fieldnames:
                    fieldnames = list(reader.fieldnames)
                for row in reader:
                    stem = row.get("pdf_stem", "")
                    if stem:
                        rows_by_stem[stem] = row   # last write wins
        except Exception as e:
            print(f"  WARN: could not read {f}: {e}")

    n_rows = len(rows_by_stem)
    print(f"  Merged {n_rows:,} unique records.")

    if dry_run:
        print(f"  [DRY RUN] would write {_STATUS_CSV_OUT}")
        return n_rows

    _LOCAL_OUTPUT.mkdir(parents=True, exist_ok=True)
    with _STATUS_CSV_OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows_by_stem.values())

    print(f"  Written: {_STATUS_CSV_OUT}  ({n_rows:,} rows)")
    return n_rows


# ---------------------------------------------------------------------------
# Enrich
# ---------------------------------------------------------------------------

def run_enrichment(dry_run: bool):
    """Run run_coord_enrichment.py to resolve lat/lon coordinates."""
    print(f"\n[3/4] Running coordinate enrichment ...")

    script = _PROJECT_DIR / "run_coord_enrichment.py"
    if not script.exists():
        # Try repo root
        script = Path(__file__).parent.parent / "project" / "run_coord_enrichment.py"
    if not script.exists():
        print(f"  WARN: {script} not found — skipping enrichment.")
        print("  Run manually:  python project/run_coord_enrichment.py "
              "--all-dot-done --include-centroid")
        return

    cmd = [
        sys.executable, str(script),
        "--output", str(_LOCAL_OUTPUT),
        "--all-dot-done",
        "--include-centroid",
    ]
    print(f"  Command: {' '.join(str(c) for c in cmd)}")

    if dry_run:
        print(f"  [DRY RUN] would run enrichment.")
        return

    result = subprocess.run(cmd, cwd=str(_PROJECT_DIR.parent))
    if result.returncode != 0:
        print(f"  WARN: enrichment exited {result.returncode}. "
              "Check output above for details.")
    else:
        print("  Enrichment complete.")


# ---------------------------------------------------------------------------
# Rebuild map
# ---------------------------------------------------------------------------

def rebuild_map(push: bool, dry_run: bool):
    """Run build_map_data.py to regenerate well_locations.json and push."""
    print(f"\n[4/4] Rebuilding map {'+ pushing to GitHub' if push else '(local only)'} ...")

    script = _PROJECT_DIR / "build_map_data.py"
    if not script.exists():
        script = Path(__file__).parent.parent / "project" / "build_map_data.py"
    if not script.exists():
        print(f"  WARN: {script} not found — skipping map rebuild.")
        print("  Run manually:  python project/build_map_data.py --push")
        return

    cmd = [sys.executable, str(script), "--output", str(_LOCAL_OUTPUT)]
    if push:
        cmd.append("--push")

    print(f"  Command: {' '.join(str(c) for c in cmd)}")

    if dry_run:
        print(f"  [DRY RUN] would rebuild map.")
        return

    result = subprocess.run(cmd, cwd=str(Path(__file__).parent.parent))
    if result.returncode != 0:
        print(f"  WARN: build_map_data.py exited {result.returncode}.")
    else:
        print("  Map rebuilt successfully.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Download Batch results, merge, enrich, and rebuild the map"
    )
    ap.add_argument("--profile",      default=None,
                    help="AWS CLI profile for Account 2 (e.g. mano)")
    ap.add_argument("--dry-run",      action="store_true",
                    help="Count files and show plan without downloading")
    ap.add_argument("--skip-download", action="store_true",
                    help="Skip S3 download (use already-downloaded files)")
    ap.add_argument("--skip-merge",   action="store_true",
                    help="Skip CSV merge step")
    ap.add_argument("--skip-enrich",  action="store_true",
                    help="Skip coordinate enrichment step")
    ap.add_argument("--skip-push",    action="store_true",
                    help="Build map locally but do not git push")
    ap.add_argument("--output",       default=str(_LOCAL_OUTPUT),
                    help=f"Local output root (default: {_LOCAL_OUTPUT})")
    args = ap.parse_args()

    # Override global output root if specified
    global _LOCAL_OUTPUT, _MERGE_DIR, _STATUS_CSV_OUT
    _LOCAL_OUTPUT   = Path(args.output)
    _MERGE_DIR      = _LOCAL_OUTPUT / "_s3_merge"
    _STATUS_CSV_OUT = _LOCAL_OUTPUT / "processing_status.csv"

    print("=" * 65)
    print("  OSU Well Pipeline — Collect Results")
    print("=" * 65)
    print(f"  S3 bucket  : s3://{OUTPUT_BUCKET}")
    print(f"  Local root : {_LOCAL_OUTPUT}")
    print(f"  Dry run    : {args.dry_run}")
    print()

    s3 = _s3_client(args.profile)

    if not args.skip_download:
        n = download_results(s3, args.dry_run)
        if n == 0 and not args.dry_run:
            print("  No results found — has the pipeline finished running?")
            sys.exit(0)

    if not args.skip_merge:
        n_rows = merge_csvs(args.dry_run)
        if n_rows == 0 and not args.dry_run:
            print("  Merge produced 0 rows — check the merge directory.")
            sys.exit(1)

    if not args.skip_enrich:
        run_enrichment(args.dry_run)

    rebuild_map(push=not args.skip_push, dry_run=args.dry_run)

    print("\n" + "=" * 65)
    print("  Collection complete.")
    print("=" * 65)


if __name__ == "__main__":
    main()

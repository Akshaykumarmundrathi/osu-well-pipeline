"""
After all Batch array tasks finish, download and merge per-slice CSVs
into canonical output files on your PC:

  D:\\project_outputs\\success.csv
  D:\\project_outputs\\manual_review\\failed.csv
  D:\\project_outputs\\processing_status.csv
  D:\\project_outputs\\dot_coordinates.csv
  D:\\project_outputs\\run_insights_combined.json
  D:\\project_outputs\\job_status_summary.json   <- per-slice health

Also prints a pass/fail summary so you know which slices need re-running.

Usage:
  python aws/merge_results.py \\
      --bucket osu-well-records-225989338968 \\
      --prefix results/ \\
      --out    D:\\project_outputs
"""

import argparse
import csv
import io
import json
from collections import defaultdict
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------
def list_slice_prefixes(s3, bucket: str, prefix: str) -> list[str]:
    """Return per-slice subprefixes ('results/slice-00000/' etc.)."""
    paginator = s3.get_paginator("list_objects_v2")
    seen = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []) or []:
            seen.add(cp["Prefix"])
    return sorted(seen)


def _get_object_safe(s3, bucket: str, key: str):
    """Return S3 object or None if not found."""
    try:
        return s3.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def stream_csv(s3, bucket: str, key: str):
    """Yield row dicts from a CSV in S3. Skips silently if the key is absent."""
    obj = _get_object_safe(s3, bucket, key)
    if obj is None:
        return
    body = obj["Body"].read().decode("utf-8")
    yield from csv.DictReader(io.StringIO(body))


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------
def merge_csv(s3, bucket: str, slice_prefixes: list[str],
              filename: str, out_path: Path) -> int:
    """Concatenate `filename` from every slice into one CSV at `out_path`.
    Returns total rows written."""
    rows_written = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = None
        fieldnames = None
        for sp in slice_prefixes:
            key = f"{sp}{filename}"
            for row in stream_csv(s3, bucket, key):
                if writer is None:
                    fieldnames = list(row.keys())
                    writer = csv.DictWriter(f, fieldnames=fieldnames,
                                            extrasaction="ignore")
                    writer.writeheader()
                writer.writerow(row)
                rows_written += 1
    print(f"  {filename:<30} -> {out_path.name}  ({rows_written:,} rows)")
    return rows_written


def merge_insights(s3, bucket: str, slice_prefixes: list[str],
                   out_path: Path):
    """Combine per-slice run_insights.json into a single tally."""
    totals: dict = defaultdict(lambda: defaultdict(int))
    per_slice = []
    for sp in slice_prefixes:
        key = f"{sp}run_insights.json"
        obj = _get_object_safe(s3, bucket, key)
        if obj is None:
            continue
        try:
            data = json.loads(obj["Body"].read().decode("utf-8"))
        except json.JSONDecodeError:
            continue
        per_slice.append({"slice": sp, **data})
        for stage, stats in data.get("stages", {}).items():
            for k in ("detected", "failed", "skipped", "already"):
                totals[stage][k] += stats.get(k, 0)
    out = {
        "total_slices": len(per_slice),
        "rollup": {k: dict(v) for k, v in totals.items()},
        "per_slice": per_slice,
    }
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"  run_insights_combined.json -> {out_path.name}")


def collect_job_status(s3, bucket: str, slice_prefixes: list[str],
                       out_path: Path):
    """Download per-slice job_status.json and produce a summary."""
    statuses = []
    failed_slices = []
    for sp in slice_prefixes:
        key = f"{sp}job_status.json"
        obj = _get_object_safe(s3, bucket, key)
        if obj is None:
            slice_id = sp.split("/")[-2]   # e.g. "slice-00003"
            statuses.append({"slice": sp, "status": "MISSING"})
            failed_slices.append(sp)
            continue
        try:
            d = json.loads(obj["Body"].read().decode("utf-8"))
        except json.JSONDecodeError:
            d = {"exit_code": -1, "note": "bad json"}
        d["slice"] = sp
        if d.get("exit_code", -1) != 0:
            failed_slices.append(sp)
        statuses.append(d)

    total   = len(statuses)
    success = sum(1 for s in statuses if s.get("exit_code") == 0)
    print(f"\n  Job status: {success}/{total} slices succeeded")
    if failed_slices:
        print(f"  FAILED/MISSING slices ({len(failed_slices)}):")
        for s in failed_slices[:20]:
            print(f"    {s}")
        if len(failed_slices) > 20:
            print(f"    ... and {len(failed_slices)-20} more")

    summary = {
        "total_slices": total,
        "succeeded": success,
        "failed": len(failed_slices),
        "failed_slices": failed_slices,
        "all_statuses": statuses,
    }
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"  job_status_summary.json -> {out_path.name}")
    return failed_slices


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Merge per-slice Batch results from S3 into local CSVs."
    )
    ap.add_argument("--bucket", default="osu-well-records-225989338968",
                    help="S3 bucket (default: osu-well-records-225989338968)")
    ap.add_argument("--prefix", default="results/",
                    help="S3 prefix for slice folders (default: results/)")
    ap.add_argument("--out",    type=Path, default=Path(r"D:\project_outputs"),
                    help=r"Local output root (default: D:\project_outputs)")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    try:
        slice_prefixes = list_slice_prefixes(s3, args.bucket, args.prefix)
        print(f"Found {len(slice_prefixes)} slices under "
              f"s3://{args.bucket}/{args.prefix}\n")

        if not slice_prefixes:
            print("No slices found — Batch jobs may not have completed yet.")
            return

        args.out.mkdir(parents=True, exist_ok=True)

        merge_csv(s3, args.bucket, slice_prefixes,
                  "success.csv",              args.out / "success.csv")
        merge_csv(s3, args.bucket, slice_prefixes,
                  "failed.csv",               args.out / "failed.csv")
        merge_csv(s3, args.bucket, slice_prefixes,
                  "processing_status.csv",    args.out / "processing_status.csv")
        merge_csv(s3, args.bucket, slice_prefixes,
                  "dot_coordinates.csv",      args.out / "dot_coordinates.csv")

        merge_insights(s3, args.bucket, slice_prefixes,
                       args.out / "run_insights_combined.json")
        failed = collect_job_status(s3, args.bucket, slice_prefixes,
                                    args.out / "job_status_summary.json")

        print("\nMerge complete.")
        if failed:
            print(f"\nWARNING: {len(failed)} slices failed. Re-run those slices.")
    finally:
        try:
            s3.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

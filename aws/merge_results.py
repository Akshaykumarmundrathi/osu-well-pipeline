"""
After all Batch array tasks finish, download and merge per-slice CSVs
into the canonical four files on your PC:

  D:\\project_outputs\\success.csv
  D:\\project_outputs\\manual_review\\failed.csv
  D:\\project_outputs\\processing_status.csv
  D:\\project_outputs\\latlong_records.csv  (only rows that have coords)

Also concatenates every slice's run_insights.json into a single
run_insights_combined.json so cross-slice trends are visible.

Usage:
  python aws/merge_results.py \
      --bucket osu-well-records \
      --prefix results/ \
      --out    D:\\project_outputs
"""

import argparse
import csv
import io
import json
from collections import defaultdict
from pathlib import Path

import boto3


def list_slice_prefixes(s3, bucket: str, prefix: str) -> list[str]:
    """Return per-slice subprefixes ('results/slice-00000/' etc.)."""
    paginator = s3.get_paginator("list_objects_v2")
    seen = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []) or []:
            seen.add(cp["Prefix"])
    return sorted(seen)


def stream_csv(s3, bucket: str, key: str):
    """Yield row dicts from a CSV in S3. Skips silently if the key is absent."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except s3.exceptions.NoSuchKey:
        return
    body = obj["Body"].read().decode("utf-8")
    for row in csv.DictReader(io.StringIO(body)):
        yield row


def merge_csv(s3, bucket: str, slice_prefixes: list[str],
              filename: str, out_path: Path):
    """Concatenate `filename` from every slice into one CSV at `out_path`."""
    fieldnames = None
    rows_written = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = None
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
    print(f"  {filename:<25} -> {out_path}  ({rows_written:,} rows)")


def merge_insights(s3, bucket: str, slice_prefixes: list[str], out_path: Path):
    """Combine per-slice run_insights.json into a single tally."""
    totals = defaultdict(lambda: defaultdict(int))
    per_slice = []
    for sp in slice_prefixes:
        key = f"{sp}run_insights.json"
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
        except s3.exceptions.NoSuchKey:
            continue
        data = json.loads(obj["Body"].read().decode("utf-8"))
        per_slice.append({"slice": sp, **data})
        for stage, stats in data.get("stages", {}).items():
            for k in ("detected", "failed", "skipped", "already"):
                totals[stage][k] += stats.get(k, 0)
    out = {"per_slice": per_slice, "rollup": {k: dict(v) for k, v in totals.items()}}
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"  run_insights_combined.json -> {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="results/")
    ap.add_argument("--out",    type=Path, required=True,
                    help=r"Local output root, e.g. D:\project_outputs")
    args = ap.parse_args()

    s3 = boto3.client("s3")
    slice_prefixes = list_slice_prefixes(s3, args.bucket, args.prefix)
    print(f"found {len(slice_prefixes)} slices under "
          f"s3://{args.bucket}/{args.prefix}")

    merge_csv(s3, args.bucket, slice_prefixes,
              "success.csv",            args.out / "success.csv")
    merge_csv(s3, args.bucket, slice_prefixes,
              "manual_review/failed.csv", args.out / "manual_review" / "failed.csv")
    merge_csv(s3, args.bucket, slice_prefixes,
              "processing_status.csv",  args.out / "processing_status.csv")
    merge_csv(s3, args.bucket, slice_prefixes,
              "latlong_records.csv",    args.out / "latlong_records.csv")

    merge_insights(s3, args.bucket, slice_prefixes,
                   args.out / "run_insights_combined.json")
    print("merge complete.")


if __name__ == "__main__":
    main()

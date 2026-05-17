"""
Scan an S3 prefix for ExportedFolderContents (N).zip files and write a
dataset_index.csv whose `zip_path` column holds s3:// URIs. Run once
locally from your PC after uploading the ZIPs.

Usage:
  python aws/scan_s3.py --bucket osu-well-records --prefix zips/ \
                        --out dataset_index_s3.csv
  aws s3 cp dataset_index_s3.csv s3://osu-well-records/index/dataset_index.csv
"""

import argparse
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3

# Make the project importable so we reuse DatasetRecord and helpers.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project"))
from scan_dataset import DatasetRecord, _safe, _FIELDS              # noqa: E402
from utils.s3_reader import list_pdfs_in_s3_zip                     # noqa: E402

_ZIP_RE = re.compile(r"ExportedFolderContents\s*\((\d+)\)\.zip$", re.I)


def _now():
    """UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def scan_bucket(bucket: str, prefix: str) -> list[DatasetRecord]:
    """List every ExportedFolderContents (N).zip under prefix and index PDFs."""
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    zips = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            m = _ZIP_RE.search(key)
            if m:
                zips.append((key, int(m.group(1))))
    zips.sort(key=lambda kv: kv[1])

    records: list[DatasetRecord] = []
    ts = _now()
    for key, num in zips:
        zip_uri = f"s3://{bucket}/{key}"
        col_name = Path(key).name
        col_safe = f"ExportedFolderContents_{num}"
        print(f"  indexing {col_name}  ({zip_uri})")
        try:
            entries = list_pdfs_in_s3_zip(zip_uri)
        except Exception as exc:
            print(f"  WARNING: {col_name}: {exc}")
            continue
        for e in entries:
            records.append(DatasetRecord(
                pdf_stem        = Path(e["pdf_name"]).stem,
                pdf_path        = f"{zip_uri}::{e['internal_path']}",
                collection      = col_name,
                collection_num  = num,
                year            = e["year"],
                month           = e["month"],
                collection_safe = col_safe,
                month_safe      = _safe(e["month"]),
                file_size_bytes = e["file_size"],
                scan_timestamp  = ts,
                zip_path        = zip_uri,
                internal_path   = e["internal_path"],
            ))
    return records


def write_csv(records, out_path: Path):
    """Persist DatasetRecord rows to CSV."""
    from dataclasses import asdict
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS)
        w.writeheader()
        w.writerows(asdict(r) for r in records)
    print(f"Wrote {len(records):,} records -> {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="zips/")
    ap.add_argument("--out",    type=Path, default=Path("dataset_index_s3.csv"))
    args = ap.parse_args()

    records = scan_bucket(args.bucket, args.prefix)
    write_csv(records, args.out)


if __name__ == "__main__":
    main()

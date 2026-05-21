"""
Scan an S3 bucket for well-record PDFs and write a dataset_index.csv.

Two modes:
  zip   (default) — scan for ExportedFolderContents (N).zip files under
                    --prefix, index PDFs inside each ZIP (legacy layout).
  flat            — scan for standalone PDFs uploaded by fastsync under
                    --prefix pdfs/ExportedFolderContents_N/year/month/stem.pdf
                    (new layout, no ZIP wrapper).

Usage:
  # ZIP layout:
  python aws/scan_s3.py --bucket osu-well-records-225989338968 \
                        --prefix zips/ --out dataset_index_s3.csv
  # Flat layout:
  python aws/scan_s3.py --bucket osu-well-records-225989338968 \
                        --prefix pdfs/ --mode flat \
                        --out dataset_index_s3_flat.csv
  # Upload the index:
  aws s3 cp dataset_index_s3.csv s3://osu-well-records-225989338968/index/dataset_index.csv
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

_ZIP_RE  = re.compile(r"ExportedFolderContents\s*\((\d+)\)\.zip$", re.I)
_FLAT_RE = re.compile(r"ExportedFolderContents_(\d+)/", re.I)


def _now():
    """UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def scan_bucket_zip(bucket: str, prefix: str) -> list[DatasetRecord]:
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


def scan_bucket_flat(bucket: str, prefix: str) -> list[DatasetRecord]:
    """
    Index standalone PDFs under prefix/ExportedFolderContents_N/year/month/stem.pdf.
    Sets zip_path="" and internal_path="" so _make_manager routes to
    get_pdf_bytes_s3_flat().
    """
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    records: list[DatasetRecord] = []
    ts = _now()

    prefix = prefix.rstrip("/") + "/"
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith(".pdf"):
                continue

            # Expected: prefix/ExportedFolderContents_N/year/month/stem.pdf
            rel = key[len(prefix):]                   # strip leading prefix
            parts = [p for p in rel.split("/") if p]  # [col, year, month, stem.pdf]
            if len(parts) < 4:
                continue

            col_part, year, month, pdf_name = parts[0], parts[1], parts[2], parts[-1]
            m = _FLAT_RE.match(col_part + "/")
            if not m:
                continue
            num      = int(m.group(1))
            col_safe = f"ExportedFolderContents_{num}"
            col_name = col_part

            records.append(DatasetRecord(
                pdf_stem        = Path(pdf_name).stem,
                pdf_path        = f"s3://{bucket}/{key}",
                collection      = col_name,
                collection_num  = num,
                year            = year,
                month           = month,
                collection_safe = col_safe,
                month_safe      = _safe(month),
                file_size_bytes = obj.get("Size", 0),
                scan_timestamp  = ts,
                zip_path        = "",
                internal_path   = "",
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
    ap.add_argument("--mode",   choices=["zip", "flat"], default="zip",
                    help="'zip' (default) for ZIP-wrapped PDFs; "
                         "'flat' for standalone PDFs under pdfs/ExportedFolderContents_N/")
    ap.add_argument("--out", "--output", dest="out",
                    type=Path, default=Path("dataset_index_s3.csv"),
                    help="Output CSV path")
    args = ap.parse_args()

    if args.mode == "flat":
        records = scan_bucket_flat(args.bucket, args.prefix)
    else:
        records = scan_bucket_zip(args.bucket, args.prefix)
    write_csv(records, args.out)


if __name__ == "__main__":
    main()

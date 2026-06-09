"""
fix_skipped_location.py
=======================
One-shot script: reset location_status from 'skipped' → 'pending' for all
records where:
  - location_status == 'skipped'   (marked by the old tier gate bug)
  - latlong_lat is empty           (NOT skipped legitimately due to lat/lon found)

Run BEFORE restarting the pipeline so those records get processed.

Usage:
    python fix_skipped_location.py [--csv PATH] [--dry-run]
"""

import argparse
import csv
import os
import shutil
import sys
from pathlib import Path

DEFAULT_CSV = Path(os.environ.get("OUTPUT_ROOT", r"D:\project_outputs")) / "processing_status.csv"


def main():
    ap = argparse.ArgumentParser(description="Reset wrongly-skipped location stages to pending")
    ap.add_argument("--csv",     default=str(DEFAULT_CSV), help="Path to processing_status.csv")
    ap.add_argument("--dry-run", action="store_true",      help="Report only, do not write")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found", file=sys.stderr)
        sys.exit(1)

    # Raise field size limit to survive any residual large fields
    csv.field_size_limit(2_000_000)

    rows = []
    reset_count = 0
    legit_skip_count = 0

    with csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            loc_status = row.get("location_status", "")
            lat = row.get("latlong_lat", "").strip()

            if loc_status == "skipped" and not lat:
                # Wrongly skipped by the old tier gate — reset to pending
                row["location_status"]     = "pending"
                row["location_confidence"] = ""
                row["location_error_type"] = ""
                row["location_section"]    = ""
                row["location_township"]   = ""
                row["location_range"]      = ""
                reset_count += 1
                if reset_count <= 10 or reset_count % 50 == 0:
                    print(f"  reset: {row.get('pdf_stem', '?')[:60]}")
            elif loc_status == "skipped" and lat:
                legit_skip_count += 1

            rows.append(row)

    print(f"\nSummary:")
    print(f"  Total rows         : {len(rows):,}")
    print(f"  Reset to pending   : {reset_count:,}  (wrongly skipped by tier gate)")
    print(f"  Left as skipped    : {legit_skip_count:,}  (legitimately skipped — lat/lon found)")

    if args.dry_run:
        print("\n[dry-run] No changes written.")
        return

    if reset_count == 0:
        print("\nNothing to reset — CSV unchanged.")
        return

    # Backup
    bak = csv_path.with_suffix(".csv.fix_bak")
    shutil.copy2(csv_path, bak)
    print(f"\nBackup written: {bak}")

    # Write
    tmp = csv_path.with_suffix(".csv.new")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    tmp.replace(csv_path)
    print(f"Written: {csv_path}  ({reset_count:,} location stages reset to pending)")


if __name__ == "__main__":
    main()

"""
fix_mid_misclass.py -- reset records poisoned by the casing-table/MID bug
==========================================================================

Root cause (see ISSUES_AND_FIXES.md P1+P2): on 1930s-40s early-tier forms
the grid detector picked the mid-page CASING RECORD table (577-812 px wide)
instead of the real PLSS grid, and the form classifier labeled it MID --
sending every downstream stage to the wrong page regions. Location success
on these records: 1%.

Fixed in code by TIER_GRID_W_MAX + classifier tier guard. This script resets
ALL stages for early-tier records whose grid_form_type is MID (or whose
detected grid is table-sized) so the re-run picks them up with the fix.

Usage:
    python fix_mid_misclass.py            # dry run
    python fix_mid_misclass.py --apply    # reset + report
Then:
    python run_c2345.py --workers 2       # reprocesses the reset records
"""
import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)

STATUS_CSV = Path(r"D:\project_outputs\processing_status.csv")

# Early-tier collections (1-6) per config.TIER_RANGES.
# Collection strings may carry a .zip suffix depending on the scan source.
_EARLY_RE = re.compile(r"ExportedFolderContents \(([1-6])\)(\.zip)?$")

RESET_COLS_BY_STAGE = {
    "grid":     ["grid_status", "grid_confidence", "grid_error_type",
                 "grid_method", "grid_form_type", "grid_image_path"],
    "location": ["location_status", "location_confidence", "location_error_type",
                 "location_section", "location_township", "location_range"],
    "county":   ["county_status", "county_confidence", "county_error_type",
                 "county_name"],
    "dot":      ["dot_status", "dot_confidence", "dot_error_type",
                 "dot_row", "dot_col", "dot_nw"],
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with STATUS_CSV.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    affected = 0
    for row in rows:
        if not _EARLY_RE.search(row.get("collection", "")):
            continue
        if row.get("grid_form_type") != "MID":
            continue
        affected += 1
        if args.apply:
            for cols in RESET_COLS_BY_STAGE.values():
                for c in cols:
                    if c in row:
                        row[c] = "pending" if c.endswith("_status") else ""

    print(f"{affected} early-tier MID-misclassified records "
          f"{'RESET' if args.apply else 'would be reset (dry run)'}")

    if not args.apply or not affected:
        if not args.apply:
            print("Re-run with --apply to write changes.")
        return

    backup = STATUS_CSV.with_suffix(".csv.mid_fix_bak")
    shutil.copy2(STATUS_CSV, backup)
    print(f"Backup -> {backup}")

    tmp = STATUS_CSV.with_suffix(".csv.new")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(STATUS_CSV)
    print(f"Done. Reprocess with: python run_c2345.py --workers 2")


if __name__ == "__main__":
    main()

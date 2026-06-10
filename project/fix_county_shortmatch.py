"""
fix_county_shortmatch.py -- reset county stage for records poisoned by the
short-text fuzzy match bug (fixed in commit addb7e4).

Bug: rapidfuzz WRatio partial-matched 1-2 char OCR noise (e.g. 'N') against
county names ('blaiNe') at score 90, which then won the anchor-weak fallback
over legitimate candidates. ~92 records got 'Blaine County' from a stray 'N'.

Detection: a per-PDF log shows the bug when its final anchor-weak (or anchor)
county result name also appears as a match from a 1-2 char text candidate,
and NO >=3-char candidate produced the same name.

Usage:
    python fix_county_shortmatch.py            # dry run -- list affected stems
    python fix_county_shortmatch.py --apply    # reset county stage to pending
"""
import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)

LOGS_ROOT  = Path(r"D:\project_outputs\logs")
STATUS_CSV = Path(r"D:\project_outputs\processing_status.csv")

RE_SHORT = re.compile(r"text='(.{1,2})' -> '([A-Za-z ]+ County)' score=\d+")
RE_LONG  = re.compile(r"text='(.{3,})' -> '([A-Za-z ]+ County)' score=\d+")
RE_FINAL = re.compile(r"County \(anchor(?:-weak)?\) = '([A-Za-z ]+ County)'")


def find_affected() -> dict[str, str]:
    """Return {pdf_stem: wrong_county} for logs showing the bug pattern."""
    affected: dict[str, str] = {}
    for log_path in LOGS_ROOT.rglob("*.log"):
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        finals = set(RE_FINAL.findall(text))
        if not finals:
            continue
        short_names = {m[1] for m in RE_SHORT.findall(text)}
        long_names  = {m[1] for m in RE_LONG.findall(text)}
        for name in finals:
            if name in short_names and name not in long_names:
                affected[log_path.stem] = name
                break
    return affected


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry run)")
    args = ap.parse_args()

    print("Scanning per-PDF logs for short-text county matches...")
    affected = find_affected()
    print(f"  {len(affected)} affected records found")
    for stem, name in sorted(affected.items())[:10]:
        print(f"    {stem}  ->  {name}")
    if len(affected) > 10:
        print(f"    ... and {len(affected) - 10} more")

    if not affected:
        return

    rows = []
    with STATUS_CSV.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    reset = 0
    for row in rows:
        if row["pdf_stem"] in affected and row.get("county_status") == "done":
            row["county_status"]     = "pending"
            row["county_confidence"] = ""
            row["county_error_type"] = ""
            row["county_name"]       = ""
            reset += 1

    print(f"  {reset} rows to reset (county done -> pending)")

    if not args.apply:
        print("\nDry run -- re-run with --apply to write changes.")
        return

    backup = STATUS_CSV.with_suffix(".csv.county_fix_bak")
    shutil.copy2(STATUS_CSV, backup)
    print(f"  Backup -> {backup}")

    tmp = STATUS_CSV.with_suffix(".csv.new")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(STATUS_CSV)
    print(f"  {reset} county stages reset. Re-run county stage with:")
    print(f"    python run_c2345.py --stage county --workers 2")


if __name__ == "__main__":
    main()

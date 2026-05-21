"""
Count PDFs per year per collection - compare S3 vs D drive.
Collections on D drive: 1, 2, 12, 13
Collections 3-11: S3 only
"""
import subprocess, json, os, sys
from collections import defaultdict

def s3_year_counts(col_num):
    """Return dict {year: count} for a given S3 collection."""
    prefix = f"s3://osu-well-records-225989338968/pdfs/ExportedFolderContents_{col_num}/"
    result = subprocess.run(
        ["aws", "s3", "ls", prefix],
        capture_output=True, text=True
    )
    years = {}
    for line in result.stdout.strip().splitlines():
        parts = line.strip().split()
        if parts and parts[-1].endswith('/'):
            year = parts[-1].rstrip('/')
            if year.isdigit():
                # Count files in this year
                yr_result = subprocess.run(
                    ["aws", "s3", "ls", prefix + year + "/", "--recursive"],
                    capture_output=True, text=True
                )
                count = sum(1 for l in yr_result.stdout.strip().splitlines() if l.strip() and '.pdf' in l.lower())
                years[year] = count
    return years

def drive_year_counts(col_num):
    """Return dict {year: count} for a collection on D drive."""
    col_map = {1: "ExportedFolderContents (1)", 2: "ExportedFolderContents (2)",
               12: "ExportedFolderContents (12)", 13: "ExportedFolderContents (13)"}
    if col_num not in col_map:
        return {}
    base = os.path.join("D:\\", col_map[col_num])
    if not os.path.exists(base):
        return {}
    years = {}
    for year in os.listdir(base):
        ypath = os.path.join(base, year)
        if os.path.isdir(ypath) and year.isdigit():
            count = sum(1 for root, dirs, files in os.walk(ypath)
                       for f in files if f.lower().endswith('.pdf'))
            years[year] = count
    return years

DRIVE_COLS = [1, 2, 12, 13]
S3_ONLY_COLS = [3, 4, 5, 6, 7, 8, 9, 10, 11]

print("=" * 80)
print("PDF COUNT COMPARISON: S3 vs D DRIVE — PER YEAR PER COLLECTION")
print("=" * 80)

grand_s3 = 0
grand_drive = 0

# Collections with both S3 and D drive
for col in DRIVE_COLS:
    print(f"\n{'='*60}")
    print(f"  COLLECTION {col}  (S3 + D Drive comparison)")
    print(f"{'='*60}")
    print(f"  {'Year':<8} {'S3':>8} {'D Drive':>10} {'Diff':>8} {'Match':>7}")
    print(f"  {'-'*7} {'-'*8} {'-'*10} {'-'*8} {'-'*7}")

    s3  = s3_year_counts(col)
    drv = drive_year_counts(col)

    all_years = sorted(set(list(s3.keys()) + list(drv.keys())))
    col_s3 = 0
    col_drv = 0
    for yr in all_years:
        sv = s3.get(yr, 0)
        dv = drv.get(yr, 0)
        diff = sv - dv
        match = "OK" if sv == dv else f"{diff:+d}"
        col_s3  += sv
        col_drv += dv
        print(f"  {yr:<8} {sv:>8,} {dv:>10,} {diff:>+8,} {match:>7}")

    print(f"  {'TOTAL':<8} {col_s3:>8,} {col_drv:>10,} {col_s3-col_drv:>+8,}")
    grand_s3 += col_s3
    grand_drive += col_drv

# S3-only collections (no local copy)
print(f"\n{'='*60}")
print(f"  COLLECTIONS 3-11  (S3 only — not on D drive)")
print(f"{'='*60}")
print(f"  {'Col':<6} {'Year':<8} {'S3 Count':>10}")
print(f"  {'-'*5} {'-'*7} {'-'*10}")

for col in S3_ONLY_COLS:
    s3 = s3_year_counts(col)
    col_total = sum(s3.values())
    grand_s3 += col_total
    for yr in sorted(s3.keys()):
        print(f"  {col:<6} {yr:<8} {s3[yr]:>10,}")
    print(f"  {'':6} {'TOTAL':<8} {col_total:>10,}")
    print()

print(f"\n{'='*80}")
print(f"  GRAND TOTAL  S3: {grand_s3:>10,}   D Drive: {grand_drive:>10,}")
print(f"{'='*80}")

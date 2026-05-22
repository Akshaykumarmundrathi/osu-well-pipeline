"""
s3_vs_local_verify.py
---------------------
Compares PDF counts between D: drive collections and S3, collection/year/month.
Produces a detailed CSV and console table showing discrepancies.
Also flags months where S3 > D: drive (potential duplicates to delete).

D: drive path:  D:/ExportedFolderContents (N)/YYYY/MM - Month/
S3 path:        s3://osu-well-records-225989338968/pdfs/ExportedFolderContents_N/YYYY/MM - Month/

Usage:
    python s3_vs_local_verify.py              # compare only
    python s3_vs_local_verify.py --delete     # delete S3 extras where S3 > local
    python s3_vs_local_verify.py --upload     # upload missing files to S3
"""

import boto3, os, sys, csv, io, json
from datetime import datetime

BUCKET      = "osu-well-records-225989338968"
S3_PREFIX   = "pdfs/"
LOCAL_ROOT  = "D:/"
NUM_COLS    = 13

# Map collection number to folder names
def local_col_path(n):
    return os.path.join(LOCAL_ROOT, f"ExportedFolderContents ({n})")

def s3_col_prefix(n):
    return f"{S3_PREFIX}ExportedFolderContents_{n}/"


def s3_count_folder(s3, prefix):
    """Count PDFs under an S3 prefix (paginates)."""
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".pdf"):
                count += 1
                keys.append(obj["Key"])
    return count, keys


def local_count_folder(path):
    """Count PDFs in a local folder (non-recursive)."""
    try:
        files = [f for f in os.listdir(path) if f.lower().endswith(".pdf")]
        return len(files), files
    except Exception:
        return 0, []


def list_local_years(col_path):
    try:
        return sorted([d for d in os.listdir(col_path)
                       if os.path.isdir(os.path.join(col_path, d)) and d.isdigit()])
    except Exception:
        return []


def list_local_months(year_path):
    try:
        return sorted([d for d in os.listdir(year_path)
                       if os.path.isdir(os.path.join(year_path, d))])
    except Exception:
        return []


def list_s3_subdirs(s3, prefix):
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix, Delimiter="/", MaxKeys=200)
    return [cp["Prefix"] for cp in resp.get("CommonPrefixes", [])]


def main():
    delete_extras = "--delete" in sys.argv
    upload_missing = "--upload" in sys.argv

    s3 = boto3.client("s3", region_name="us-east-1")
    print(f"S3 vs D: drive PDF verification  [{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}]")
    print(f"Mode: delete_extras={delete_extras}, upload_missing={upload_missing}")
    print()

    rows = []          # (col, year, month, local_cnt, s3_cnt, diff, status)
    totals_local = 0
    totals_s3    = 0
    issues       = []

    for col_n in range(1, NUM_COLS + 1):
        local_col = local_col_path(col_n)
        s3_col    = s3_col_prefix(col_n)

        if not os.path.isdir(local_col):
            print(f"[Col {col_n:2d}] LOCAL NOT FOUND: {local_col}")
            continue

        years = list_local_years(local_col)
        # Also check S3 for extra years
        s3_year_prefixes = list_s3_subdirs(s3, s3_col)
        s3_years = set(pfx.split("/")[-2] for pfx in s3_year_prefixes)
        all_years = sorted(set(years) | s3_years)

        col_local = 0
        col_s3    = 0
        print(f"[Col {col_n:2d}] Processing {len(all_years)} years...", flush=True)

        for year in all_years:
            local_year_path = os.path.join(local_col, year)
            s3_year_prefix  = f"{s3_col}{year}/"

            # List months from both sides
            local_months = list_local_months(local_year_path) if os.path.isdir(local_year_path) else []
            s3_month_prefixes = list_s3_subdirs(s3, s3_year_prefix)
            s3_months = set(pfx.split("/")[-2] for pfx in s3_month_prefixes)
            all_months = sorted(set(local_months) | s3_months)

            for month in all_months:
                local_month_path = os.path.join(local_year_path, month)
                s3_month_prefix  = f"{s3_year_prefix}{month}/"

                local_cnt, local_files = local_count_folder(local_month_path)
                s3_cnt,    s3_keys     = s3_count_folder(s3, s3_month_prefix)

                diff = s3_cnt - local_cnt
                if diff == 0:
                    status = "OK"
                elif diff < 0:
                    # S3 missing files
                    status = f"S3_MISSING_{-diff}"
                    issues.append({"col": col_n, "year": year, "month": month,
                                   "local": local_cnt, "s3": s3_cnt, "diff": diff,
                                   "type": "S3_MISSING",
                                   "local_path": local_month_path,
                                   "s3_prefix": s3_month_prefix})
                else:
                    # S3 has extra files
                    status = f"S3_EXTRA_{diff}"
                    issues.append({"col": col_n, "year": year, "month": month,
                                   "local": local_cnt, "s3": s3_cnt, "diff": diff,
                                   "type": "S3_EXTRA",
                                   "local_path": local_month_path,
                                   "s3_prefix": s3_month_prefix,
                                   "s3_keys": s3_keys,
                                   "local_files": local_files})

                rows.append((col_n, year, month, local_cnt, s3_cnt, diff, status))
                col_local += local_cnt
                col_s3    += s3_cnt

        print(f"[Col {col_n:2d}] Local={col_local:6d}  S3={col_s3:6d}  diff={col_s3-col_local:+d}")
        totals_local += col_local
        totals_s3    += col_s3

    print()
    print("=" * 70)
    print(f"TOTALS:  Local={totals_local:,}   S3={totals_s3:,}   diff={totals_s3-totals_local:+,}")
    print(f"Issues:  {len(issues)} folders with mismatches")
    print()

    # Breakdown of issues
    missing = [i for i in issues if i["type"] == "S3_MISSING"]
    extra   = [i for i in issues if i["type"] == "S3_EXTRA"]
    print(f"  S3 MISSING (need upload): {len(missing)} folders")
    print(f"  S3 EXTRA (duplicates):    {len(extra)} folders")

    if missing:
        print("\n  Top missing folders:")
        for iss in sorted(missing, key=lambda x: -(-x["diff"]))[:20]:
            print(f"    Col{iss['col']:2d} {iss['year']}/{iss['month']}: "
                  f"local={iss['local']}, s3={iss['s3']}, missing={-iss['diff']}")

    if extra:
        print("\n  S3 extra folders (S3 > local):")
        for iss in extra:
            print(f"    Col{iss['col']:2d} {iss['year']}/{iss['month']}: "
                  f"local={iss['local']}, s3={iss['s3']}, extra={iss['diff']}")

    # Save detailed CSV
    out_csv = os.path.join(os.path.dirname(__file__), "s3_vs_local_comparison.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["collection", "year", "month", "local_count", "s3_count", "diff", "status"])
        for row in rows:
            w.writerow(row)
    print(f"\nDetailed CSV saved: {out_csv}")

    # Save issues JSON
    out_json = os.path.join(os.path.dirname(__file__), "s3_issues.json")
    with open(out_json, "w", encoding="utf-8") as f:
        # Don't write s3_keys (too large) unless needed
        clean_issues = [{k: v for k, v in iss.items() if k not in ("s3_keys", "local_files")}
                        for iss in issues]
        json.dump(clean_issues, f, indent=2)
    print(f"Issues JSON saved:  {out_json}")

    # Handle deletions (S3 extra)
    if delete_extras and extra:
        print(f"\nDeleting {sum(i['diff'] for i in extra)} extra S3 files...")
        s3_del = boto3.client("s3", region_name="us-east-1")
        deleted = 0
        for iss in extra:
            _, s3_keys_all = s3_count_folder(s3_del, iss["s3_prefix"])
            local_cnt, local_files_raw = local_count_folder(iss["local_path"])
            local_names = set(local_files_raw)
            for key in s3_keys_all:
                fname = key.split("/")[-1]
                if fname not in local_names:
                    print(f"  DELETE: {key}")
                    s3_del.delete_object(Bucket=BUCKET, Key=key)
                    deleted += 1
        print(f"Deleted {deleted} extra S3 files.")

    print("\nDone.")
    return rows, issues


if __name__ == "__main__":
    main()

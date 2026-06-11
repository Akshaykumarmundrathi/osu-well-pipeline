"""
audit_consistency.py -- cross-check extracted county vs PLSS-cell county
=========================================================================

Ground-truth guard (post-hoc, flags only): for every resolved well in
dot_coordinates.csv, ask the PLSS database which county the resolved
section actually sits in, and compare with the county the OCR extracted.

A mismatch means ONE of: wrong county OCR, wrong STR extraction, or a
border section (sections can straddle counties — both names reported).
Mismatches are appended to the row's `flags` column ("county_mismatch:
<plss_county>") so the live map surfaces them in the popup, and written
to county_mismatch_report.csv for review.

Usage:
    python audit_consistency.py            # report only
    python audit_consistency.py --apply    # also write flags into dot_coordinates.csv
"""
import argparse
import csv
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)

# .env for RDS creds
_ENV = Path(r"D:\project_modular\.env")
for _line in _ENV.read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

DOT_CSV = Path(r"D:\project_outputs\dot_coordinates.csv")
REPORT  = Path(r"D:\project_outputs\county_mismatch_report.csv")


def parse_dir(v: str, default: str):
    m = re.match(r"^\s*(\d{1,3})\s*([NSEW]?)", (v or "").upper())
    if not m:
        return None
    return int(m.group(1)), (m.group(2) or default)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    import psycopg2
    conn = psycopg2.connect(
        host=os.environ["RDS_HOST"], port=int(os.environ.get("RDS_PORT", 5432)),
        dbname=os.environ["RDS_DBNAME"], user=os.environ["RDS_USER"],
        password=os.environ["RDS_PASSWORD"], connect_timeout=20)
    cur = conn.cursor()

    rows = list(csv.DictReader(DOT_CSV.open(newline="", encoding="utf-8",
                                            errors="replace")))
    fieldnames = list(rows[0].keys()) if rows else []
    if "flags" not in fieldnames:
        fieldnames.append("flags")

    cache: dict[tuple, set] = {}
    checked = mismatch = agree = border = 0
    report = []
    for r in rows:
        if not (r.get("resolved_lat") or "").strip():
            continue
        extracted = (r.get("county_name") or "").replace("County", "").strip().lower()
        if not extracted:
            continue
        sec = re.sub(r"\D", "", r.get("section") or "")
        twp = parse_dir(r.get("township"), "N")
        rng = parse_dir(r.get("range"), "W")
        if not sec or not twp or not rng:
            continue
        key = (int(sec), twp[0], twp[1], rng[0], rng[1])
        if key not in cache:
            cur.execute(
                'SELECT DISTINCT county_name FROM plss_grid WHERE sect_num=%s '
                'AND township=%s AND north_south=%s AND "range"=%s AND east_west=%s',
                key)
            cache[key] = {row[0].replace("County", "").strip().lower()
                          for row in cur.fetchall() if row[0]}
        plss = cache[key]
        if not plss:
            continue
        checked += 1
        if extracted in plss:
            agree += 1
            if len(plss) > 1:
                border += 1
            continue
        mismatch += 1
        report.append({"pdf_stem": r["pdf_stem"], "extracted": r.get("county_name"),
                       "plss_counties": "|".join(sorted(plss)),
                       "section": r.get("section"), "township": r.get("township"),
                       "range": r.get("range"),
                       "resolution": r.get("resolution_source")})
        if args.apply:
            tag = f"county_mismatch:{sorted(plss)[0]}"
            cur_flags = (r.get("flags") or "").strip()
            if "county_mismatch" not in cur_flags:
                r["flags"] = f"{cur_flags};{tag}".strip(";")

    conn.close()
    print(f"checked {checked} resolved wells with full STR+county")
    print(f"  agree    : {agree} ({agree*100//max(checked,1)}%)  "
          f"(of which border sections: {border})")
    print(f"  mismatch : {mismatch} ({mismatch*100//max(checked,1)}%)")
    top = Counter((x["extracted"], x["plss_counties"]) for x in report)
    for (e, p), n in top.most_common(8):
        print(f"   {n:>4}  extracted={e!r:<22} plss={p}")

    with REPORT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["pdf_stem", "extracted", "plss_counties",
                                          "section", "township", "range", "resolution"])
        w.writeheader(); w.writerows(report)
    print(f"report -> {REPORT}")

    if args.apply:
        shutil.copy2(DOT_CSV, DOT_CSV.with_suffix(".csv.flags_bak"))
        tmp = DOT_CSV.with_suffix(".csv.new")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fieldnames})
        tmp.replace(DOT_CSV)
        print(f"flags written into {DOT_CSV}")


if __name__ == "__main__":
    main()

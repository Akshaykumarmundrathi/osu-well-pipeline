"""
re_resolve_mismatches.py -- fix east/west-mirrored coordinates, county-pinned
==============================================================================

audit_consistency.py found 2,023 resolved wells whose extracted county
disagrees with the county of their resolved PLSS cell — overwhelmingly an
east/west mirror: a range extracted WITHOUT its E/W suffix was defaulted
to W during resolution, dropping eastern wells into western Oklahoma.

Deterministic repair (the extracted county is the ground truth pin):
  for each mismatched well, query its STR in ALL direction combinations
  (N/S x E/W as needed); keep the combination whose PLSS cells lie in the
  extracted county. Exactly-one-match -> re-resolve (quadrant cell when
  dot_nw known, else section centroid), update coordinates,
  resolution_source='county_pinned_<how>'. Ambiguous/none -> left alone,
  reported.

Usage:
    python re_resolve_mismatches.py            # dry run, stats only
    python re_resolve_mismatches.py --apply    # write dot_coordinates.csv
"""
import argparse
import csv
import os
import re
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)

_ENV = Path(r"D:\project_modular\.env")
for _line in _ENV.read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        k, v = _line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

DOT_CSV = Path(r"D:\project_outputs\dot_coordinates.csv")
REPORT  = Path(r"D:\project_outputs\county_mismatch_report.csv")


def num_of(v: str) -> int | None:
    d = re.sub(r"\D", "", v or "")
    return int(d) if d else None


def suffix_of(v: str, allowed: str) -> str:
    s = (v or "").strip().upper()
    return s[-1] if s and s[-1] in allowed else ""


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

    mismatched = {r["pdf_stem"] for r in csv.DictReader(
        REPORT.open(newline="", encoding="utf-8"))}
    print(f"{len(mismatched)} mismatched wells from report")

    rows = list(csv.DictReader(DOT_CSV.open(newline="", encoding="utf-8",
                                            errors="replace")))
    fieldnames = list(rows[0].keys())

    fixed = ambiguous = nomatch = 0
    cache: dict[tuple, list] = {}
    for r in rows:
        if r["pdf_stem"] not in mismatched:
            continue
        county = (r.get("county_name") or "").replace("County", "").strip()
        sec = num_of(r.get("section"))
        twp = num_of(r.get("township"))
        rng = num_of(r.get("range"))
        if not (county and sec and twp and rng and 1 <= sec <= 36):
            continue
        ns_fixed = suffix_of(r.get("township"), "NS")
        ew_fixed = suffix_of(r.get("range"), "EW")
        candidates = []
        for ns in ([ns_fixed] if ns_fixed else ["N", "S"]):
            for ew in ([ew_fixed] if ew_fixed else ["E", "W"]):
                key = (sec, twp, ns, rng, ew, county.lower())
                if key not in cache:
                    cur.execute(
                        'SELECT MIN(minx),MIN(miny),MAX(maxx),MAX(maxy),COUNT(*) '
                        'FROM plss_grid WHERE sect_num=%s AND township=%s AND '
                        'north_south=%s AND "range"=%s AND east_west=%s AND '
                        'county_name ILIKE %s',
                        (sec, twp, ns, rng, ew, f"%{county}%"))
                    cache[key] = cur.fetchone()
                row = cache[key]
                if row and row[4]:
                    candidates.append((ns, ew, row))
        if len(candidates) != 1:
            ambiguous += (1 if len(candidates) > 1 else 0)
            nomatch   += (1 if not candidates else 0)
            continue
        ns, ew, (minx, miny, maxx, maxy, _) = candidates[0]
        # quadrant cell if dot_nw available
        how = "county_pinned_centroid"
        lat = (miny + maxy) / 2
        lon = (minx + maxx) / 2
        quad = (r.get("unet_nw") or r.get("dot_nw") or "").strip()
        if quad:
            cur.execute(
                'SELECT minx,miny,maxx,maxy FROM plss_grid WHERE sect_num=%s '
                'AND township=%s AND north_south=%s AND "range"=%s AND '
                'east_west=%s AND quadrant_label=%s LIMIT 1',
                (sec, twp, ns, rng, ew, quad))
            qrow = cur.fetchone()
            if qrow:
                lat = (qrow[1] + qrow[3]) / 2
                lon = (qrow[0] + qrow[2]) / 2
                how = "county_pinned_quadrant"
        r["resolved_lat"] = f"{lat:.7f}"
        r["resolved_lon"] = f"{lon:.7f}"
        r["resolution_source"] = how
        r["township"] = f"{twp}{ns}"
        r["range"] = f"{rng}{ew}"
        fixed += 1

    conn.close()
    print(f"fixed: {fixed}   ambiguous(skip): {ambiguous}   no-match(skip): {nomatch}")

    if args.apply and fixed:
        shutil.copy2(DOT_CSV, DOT_CSV.with_suffix(".csv.mirror_bak"))
        tmp = DOT_CSV.with_suffix(".csv.new")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader(); w.writerows(rows)
        tmp.replace(DOT_CSV)
        print(f"written -> {DOT_CSV}")
    elif not args.apply:
        print("dry run — use --apply to write")


if __name__ == "__main__":
    main()

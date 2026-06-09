"""
Test-100 Quality Analysis
Reads processing_status.csv and produces a per-collection / per-tier breakdown.
Run after the test100 pipeline completes all 1300 records.
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

CSV = Path(r"D:\project_modular\project_outputs_test100\processing_status.csv")

# Tier boundaries (mirrors config.py)
TIERS = [
    (range(1,  7),  "EARLY      (Coll 1-6,   ~1911-1970)"),
    (range(7,  9),  "TRANSITION (Coll 7-8,   ~1971-1982)"),
    (range(9,  11), "MID        (Coll 9-10,  ~1983-2000)"),
    (range(11, 13), "LATE       (Coll 11-12, ~2001-2018)"),
    (range(13, 14), "MODERN     (Coll 13,    ~2019-2024)"),
]

def tier(n):
    for rng, label in TIERS:
        if n in rng:
            return label
    return "UNKNOWN"

def pct(num, den):
    if den == 0: return "  n/a"
    return f"{100*num/den:5.1f}%"

def mode(counts_dict):
    """Return the top error type(s)."""
    if not counts_dict:
        return ""
    top = sorted(counts_dict.items(), key=lambda x: -x[1])[:3]
    return "  |  ".join(f"{k}({v})" for k, v in top)

def main():
    if not CSV.exists():
        print(f"CSV not found: {CSV}")
        sys.exit(1)

    rows = []
    with CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    print(f"Rows loaded: {total}")
    print()

    # ----- Overall counts -----
    stages = ["latlong", "grid", "location", "county", "dot"]
    print("=" * 80)
    print("OVERALL STAGE SUMMARY")
    print("=" * 80)
    hdr = f"{'Stage':<12}  {'Done':>6}  {'Skipped':>8}  {'Failed':>8}  {'Pending':>8}  {'Done%':>7}"
    print(hdr)
    print("-" * len(hdr))
    for st in stages:
        d = sum(1 for r in rows if r[f"{st}_status"] == "done")
        sk = sum(1 for r in rows if r[f"{st}_status"] == "skipped")
        fa = sum(1 for r in rows if r[f"{st}_status"] == "failed")
        pe = sum(1 for r in rows if r[f"{st}_status"] == "pending")
        print(f"  {st:<10}  {d:>6}  {sk:>8}  {fa:>8}  {pe:>8}  {pct(d, total):>7}")
    print()

    # ----- Enrichable summary -----
    enrichable = sum(1 for r in rows if
        (r["latlong_lat"] and r["latlong_lon"]) or r["dot_status"] == "done")
    ll_only = sum(1 for r in rows if
        (r["latlong_lat"] and r["latlong_lon"]) and r["dot_status"] != "done")
    dot_only = sum(1 for r in rows if
        not (r["latlong_lat"] and r["latlong_lon"]) and r["dot_status"] == "done")
    both = sum(1 for r in rows if
        (r["latlong_lat"] and r["latlong_lon"]) and r["dot_status"] == "done")
    print(f"Enrichable (latlong OR dot): {enrichable}/{total} = {pct(enrichable, total)}")
    print(f"  Latlong only : {ll_only}")
    print(f"  Dot only     : {dot_only}")
    print(f"  Both         : {both}")
    print()

    # ----- Per-collection breakdown -----
    by_coll = defaultdict(list)
    for r in rows:
        by_coll[int(r["collection_num"])].append(r)

    print("=" * 80)
    print("PER-COLLECTION BREAKDOWN")
    print("=" * 80)
    hdr2 = (f"{'Coll':>5}  {'N':>4}  {'Grid%':>6}  {'STR%':>6}  "
            f"{'Cnty%':>6}  {'Dot%':>6}  {'LL%':>6}  {'Enrich%':>8}")
    print(hdr2)
    print("-" * len(hdr2))
    for cn in sorted(by_coll.keys()):
        g = by_coll[cn]
        n = len(g)
        grid_done  = sum(1 for r in g if r["grid_status"]     == "done")
        str_done   = sum(1 for r in g if r["location_status"] == "done")
        cnty_done  = sum(1 for r in g if r["county_status"]   == "done")
        dot_done   = sum(1 for r in g if r["dot_status"]      == "done")
        ll_done    = sum(1 for r in g if r["latlong_lat"] and r["latlong_lon"])
        enrich     = sum(1 for r in g if
            (r["latlong_lat"] and r["latlong_lon"]) or r["dot_status"] == "done")
        print(f"  {cn:>3}  {n:>4}  "
              f"{pct(grid_done, n):>6}  {pct(str_done, n):>6}  "
              f"{pct(cnty_done, n):>6}  {pct(dot_done, n):>6}  "
              f"{pct(ll_done, n):>6}  {pct(enrich, n):>8}")
    print()

    # ----- Per-tier breakdown -----
    print("=" * 80)
    print("PER-TIER BREAKDOWN")
    print("=" * 80)
    for coll_range, t_label in TIERS:
        g = [r for r in rows if int(r["collection_num"]) in coll_range]
        if not g:
            continue
        n = len(g)
        grid_done  = sum(1 for r in g if r["grid_status"]     == "done")
        str_done   = sum(1 for r in g if r["location_status"] == "done")
        cnty_done  = sum(1 for r in g if r["county_status"]   == "done")
        dot_done   = sum(1 for r in g if r["dot_status"]      == "done")
        ll_done    = sum(1 for r in g if r["latlong_lat"] and r["latlong_lon"])
        enrich     = sum(1 for r in g if
            (r["latlong_lat"] and r["latlong_lon"]) or r["dot_status"] == "done")
        print(f"\n  {t_label}  (n={n})")
        print(f"    Grid:     {grid_done:>3}/{n}  {pct(grid_done, n)}")
        print(f"    STR:      {str_done:>3}/{n}  {pct(str_done, n)}")
        print(f"    County:   {cnty_done:>3}/{n}  {pct(cnty_done, n)}")
        print(f"    Dot:      {dot_done:>3}/{n}  {pct(dot_done, n)}")
        print(f"    LatLon:   {ll_done:>3}/{n}  {pct(ll_done, n)}")
        print(f"    Enrichable:{enrich:>3}/{n}  {pct(enrich, n)}")

        # Failure modes
        for stage in stages:
            errors = defaultdict(int)
            for r in g:
                if r[f"{stage}_status"] == "failed":
                    errors[r.get(f"{stage}_error_type", "?")] += 1
            if errors:
                print(f"    {stage} failures: {mode(errors)}")
    print()

    # ----- Grid method breakdown -----
    print("=" * 80)
    print("GRID METHOD BREAKDOWN (detected records only)")
    print("=" * 80)
    method_counts = defaultdict(int)
    for r in rows:
        if r["grid_status"] == "done":
            method_counts[r.get("grid_method", "unknown")] += 1
    for m, c in sorted(method_counts.items(), key=lambda x: -x[1]):
        print(f"  {m:<30}  {c:>5}")
    print()

    # ----- Pending records -----
    pending = [r for r in rows if any(
        r[f"{st}_status"] == "pending" for st in stages)]
    if pending:
        print(f"WARNING: {len(pending)} records still have pending stages!")
        by_c = defaultdict(int)
        for r in pending:
            by_c[r["collection_num"]] += 1
        for cn, cnt in sorted(by_c.items()):
            print(f"  Coll {cn}: {cnt} pending")
    else:
        print("All 1300 records fully processed (no pending stages).")

    print()
    print("Done.")

if __name__ == "__main__":
    main()

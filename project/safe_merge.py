"""safe_merge.py -- additive, non-destructive merge for the tracking CSVs.

GUARANTEE: merging new records can only ADD rows or UPGRADE a record that had
no result. It never deletes a row and never downgrades a successful/mapped
record to a worse one. Always backs up the target before writing.

This is the ONLY sanctioned way to fold shards / new results into
processing_status.csv or dot_coordinates.csv. (The old consolidate_status.py
truncated the master; do not use it.)

Rules per stem:
  - stem only in base  -> kept as-is
  - stem only in new   -> added
  - stem in both       -> keep base UNLESS new is strictly better:
        status   : new has more stages 'done' than base
        dotcoords: new has resolved_lat/lon and base does not
     (otherwise base wins -> existing good data is never disturbed)

Usage:
  python safe_merge.py status  <shard1.csv> [shard2.csv ...]
  python safe_merge.py dotcoords <new_dot_coordinates.csv> [...]
"""
import csv, os, shutil, sys, time
from pathlib import Path

csv.field_size_limit(2_000_000)
OUT = Path(r"D:\project_outputs")
STAGES = ["latlong", "grid", "location", "county", "dot"]


def _rows(p):
    with open(p, newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f)
        return rd.fieldnames, list(rd)


def _done_count(r):
    return sum(1 for s in STAGES if (r.get(f"{s}_status") or "") == "done")


def _has_coord(r):
    return bool((r.get("resolved_lat") or "").strip() and (r.get("resolved_lon") or "").strip())


def merge(kind: str, incoming: list[str]):
    target = OUT / ("processing_status.csv" if kind == "status" else "dot_coordinates.csv")
    better = (_done_count if kind == "status" else
              (lambda r: 1 if _has_coord(r) else 0))
    cols, base = _rows(target)
    by = {r["pdf_stem"]: r for r in base if r.get("pdf_stem")}
    added = upgraded = protected = 0
    for path in incoming:
        ncols, rows = _rows(path)
        for c in ncols:
            if c not in cols:
                cols.append(c)
        for r in rows:
            s = r.get("pdf_stem", "")
            if not s:
                continue
            if s not in by:
                by[s] = r; added += 1
            elif better(r) > better(by[s]):
                by[s] = r; upgraded += 1
            else:
                protected += 1
    bak = target.with_suffix(f".csv.safemrg_{time.strftime('%Y%m%d_%H%M%S')}_bak")
    shutil.copy2(target, bak)
    tmp = target.with_suffix(".csv.smtmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in by.values():
            w.writerow({k: r.get(k, "") for k in cols})
    os.replace(tmp, target)
    print(f"{target.name}: +{added} added, {upgraded} upgraded, {protected} protected "
          f"(unchanged). total {len(by):,}. backup -> {bak.name}")


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("status", "dotcoords"):
        print(__doc__); sys.exit(1)
    merge(sys.argv[1], sys.argv[2:])


if __name__ == "__main__":
    main()

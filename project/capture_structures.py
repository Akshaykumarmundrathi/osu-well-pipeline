"""
capture_structures.py -- harvest structural fingerprints from metadata.json
============================================================================

Walks $OUTPUT_ROOT/metadata/**/metadata.json and writes one row per record
to form_structures.csv: page geometry, grid bbox/position/AR, form type,
zones, anchor phrase, STR field presence, dot position. This is the dataset
for studying how form layouts evolve across years/collections.

Usage:
    python capture_structures.py                      # all records
    python capture_structures.py --collection 3       # one collection
    python capture_structures.py --summary            # aggregate pivots only

Output:
    $OUTPUT_ROOT/form_structures.csv
"""
import argparse
import collections
import csv
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", r"D:\project_outputs"))
META_ROOT   = OUTPUT_ROOT / "metadata"
OUT_CSV     = OUTPUT_ROOT / "form_structures.csv"

FIELDS = [
    "pdf_stem", "collection", "year", "month",
    # grid geometry
    "grid_detected", "grid_method", "form_type", "grid_zone",
    "grid_x", "grid_y", "grid_w", "grid_h", "grid_ar", "grid_page",
    "anchor_phrase", "grid_confidence",
    # hints the classifier emitted
    "str_zone", "county_format_hint", "str_strategy_hint",
    # location outcome
    "loc_detected", "has_section", "has_township", "has_range",
    "loc_confidence", "loc_raw_len", "loc_error",
    # county outcome
    "county_detected", "county_name", "county_method", "county_score",
    # dot outcome
    "dot_detected", "dot_row", "dot_col", "dot_x_norm", "dot_y_norm",
    "dot_threshold", "dot_confidence",
    # timing
    "grid_s", "loc_s", "county_s", "dot_s",
]


def _row_from_meta(d: dict) -> dict:
    src   = d.get("source", {})
    st    = d.get("stages", {})
    grid  = st.get("grid", {}) or {}
    loc   = st.get("location", {}) or {}
    cty   = st.get("county", {}) or {}
    dot   = st.get("dot", {}) or {}

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    bbox = grid.get("bbox") or [None] * 4
    gx, gy, gx1, gy1 = [_num(v) for v in (list(bbox) + [None] * 4)[:4]]
    gw = (gx1 - gx) if (gx is not None and gx1 is not None) else None
    gh = (gy1 - gy) if (gy is not None and gy1 is not None) else None
    # Old runs stored bbox as [x,y,w,h]; new runs as [x0,y0,x1,y1].
    # Negative width/height means old convention — recover w/h directly.
    if gw is not None and gw <= 0:
        gw = gx1
    if gh is not None and gh <= 0:
        gh = gy1
    ar = round(gw / gh, 3) if (gw and gh) else None

    return {
        "pdf_stem":   src.get("pdf_stem", ""),
        "collection": src.get("collection", ""),
        "year":       src.get("year", ""),
        "month":      src.get("month", ""),
        "grid_detected":   grid.get("detected", ""),
        "grid_method":     grid.get("method", "") or "",
        "form_type":       grid.get("form_type", "") or "",
        "grid_zone":       grid.get("grid_zone", "") or "",
        "grid_x": gx, "grid_y": gy, "grid_w": gw, "grid_h": gh,
        "grid_ar": ar,
        "grid_page":       grid.get("page", ""),
        "anchor_phrase":   grid.get("anchor_phrase", "") or "",
        "grid_confidence": grid.get("confidence", ""),
        "str_zone":           grid.get("str_zone", "") or "",
        "county_format_hint": grid.get("county_format_hint", "") or "",
        "str_strategy_hint":  grid.get("str_strategy_hint", "") or "",
        "loc_detected":  loc.get("detected", ""),
        "has_section":   bool((loc.get("section")  or "").strip()),
        "has_township":  bool((loc.get("township") or "").strip()),
        "has_range":     bool((loc.get("range")    or "").strip()),
        "loc_confidence": loc.get("confidence", ""),
        "loc_raw_len":   len(loc.get("raw_text") or ""),
        "loc_error":     loc.get("error", "") or "",
        "county_detected": cty.get("detected", ""),
        "county_name":     cty.get("name", "") or "",
        "county_method":   cty.get("method", "") or "",
        "county_score":    cty.get("fuzzy_score", ""),
        "dot_detected":  dot.get("detected", ""),
        "dot_row":       dot.get("row", ""),
        "dot_col":       dot.get("col", ""),
        "dot_x_norm":    dot.get("x_norm", ""),
        "dot_y_norm":    dot.get("y_norm", ""),
        "dot_threshold": dot.get("threshold", ""),
        "dot_confidence": dot.get("confidence", ""),
        "grid_s":   round(grid.get("_elapsed", 0) or 0, 1),
        "loc_s":    round(loc.get("_elapsed", 0) or 0, 1),
        "county_s": round(cty.get("_elapsed", 0) or 0, 1),
        "dot_s":    round(dot.get("_elapsed", 0) or 0, 1),
    }


def harvest(collection_filter: str | None) -> list[dict]:
    rows = []
    for meta_path in META_ROOT.rglob("metadata.json"):
        if collection_filter and f"_{collection_filter}" not in meta_path.parts[len(META_ROOT.parts)]:
            continue
        try:
            d = json.loads(meta_path.read_text(encoding="utf-8"))
            rows.append(_row_from_meta(d))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def print_summary(rows: list[dict]) -> None:
    print(f"\n{'=' * 64}\n  Form structure summary -- {len(rows)} records\n{'=' * 64}")

    # form type by year
    print("\n[1] Form type by year (top type per year)")
    by_year = collections.defaultdict(collections.Counter)
    for r in rows:
        if r["form_type"]:
            by_year[r["year"]][r["form_type"]] += 1
    for y in sorted(by_year):
        top = by_year[y].most_common(3)
        line = "  ".join(f"{t}={n}" for t, n in top)
        print(f"  {y}: {line}")

    # grid size & AR by form type
    print("\n[2] Median grid geometry by form type")
    geo = collections.defaultdict(list)
    for r in rows:
        if r["grid_w"] and r["grid_h"]:
            geo[r["form_type"]].append((r["grid_w"], r["grid_h"], r["grid_ar"]))
    for t in sorted(geo, key=lambda k: -len(geo[k])):
        v = sorted(geo[t])
        n = len(v)
        med = v[n // 2]
        print(f"  {t:<14} n={n:<5} W={med[0]} H={med[1]} AR={med[2]}")

    # grid zone distribution by form type
    print("\n[3] Grid position zone by form type")
    gz = collections.defaultdict(collections.Counter)
    for r in rows:
        if r["form_type"] and r["grid_zone"]:
            gz[r["form_type"]][r["grid_zone"]] += 1
    for t in sorted(gz, key=lambda k: -sum(gz[k].values())):
        line = "  ".join(f"{z}={n}" for z, n in gz[t].most_common(3))
        print(f"  {t:<14} {line}")

    # dot success by form type + threshold
    print("\n[4] Dot detection by form type")
    ds = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        if r["form_type"] and str(r["grid_detected"]) == "True":
            ds[r["form_type"]][0 if str(r["dot_detected"]) == "True" else 1] += 1
    for t in sorted(ds, key=lambda k: -sum(ds[k])):
        d, f = ds[t]
        tot = d + f
        print(f"  {t:<14} {d * 100 // tot if tot else 0:>3}%  ({d}/{tot})")

    # anchor phrase coverage by year
    print("\n[5] Anchor phrase coverage by year (% records with printed anchor)")
    ay = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        if str(r["grid_detected"]) == "True":
            ay[r["year"]][0 if r["anchor_phrase"] else 1] += 1
    for y in sorted(ay):
        a, no = ay[y]
        tot = a + no
        print(f"  {y}: {a * 100 // tot if tot else 0:>3}%  ({a}/{tot})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", default=None,
                    help="collection number filter, e.g. 3")
    ap.add_argument("--summary", action="store_true",
                    help="print aggregate pivots (also written to CSV)")
    args = ap.parse_args()

    print(f"Harvesting metadata from {META_ROOT} ...")
    rows = harvest(args.collection)
    print(f"  {len(rows)} records")

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"  Wrote {OUT_CSV}")

    if args.summary or True:
        print_summary(rows)


if __name__ == "__main__":
    main()

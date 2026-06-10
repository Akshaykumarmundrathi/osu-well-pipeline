"""
inspect_locations.py -- build a manual-review pack for location failures
=========================================================================

The single largest recoverable pool: records where the GRID was detected
but LOCATION (Section/Township/Range) failed -- 1,146 in the C2-C5 sample,
concentrated in 1939-1947. This script samples those records, copies their
debug images into one flat review folder, and writes an index CSV with the
OCR raw text so you can study WHERE the STR actually sits on these forms.

Usage:
    python inspect_locations.py                          # 100 samples, 1939-1947
    python inspect_locations.py --year-from 1926 --year-to 1950 --limit 200
    python inspect_locations.py --all-failures           # no year filter

Output:
    $OUTPUT_ROOT/location_review/
        index.csv                 stem, year, form_type, str_zone, hints, raw OCR
        {NN}_{stem}_grid.png      detected grid (proves form is readable)
        {NN}_{stem}_county_page.png  full page w/ green box (shows page layout)
"""
import argparse
import csv
import json
import os
import random
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)

OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", r"D:\project_outputs"))
STATUS_CSV  = OUTPUT_ROOT / "processing_status.csv"
META_ROOT   = OUTPUT_ROOT / "metadata"
REVIEW_DIR  = OUTPUT_ROOT / "location_review"

IDX_FIELDS = ["n", "pdf_stem", "collection", "year", "month", "form_type",
              "str_zone", "str_strategy_hint", "grid_method", "loc_error",
              "raw_text_snippet", "grid_png", "page_png"]


def _meta_for(row: dict) -> dict:
    """Load metadata.json for a status row (best effort)."""
    coll  = (row.get("collection") or "").replace(" (", "_").replace(")", "").replace(" ", "_")
    year  = row.get("year") or "unknown"
    month = (row.get("month") or "unknown").replace(" - ", "___").replace(" ", "_")
    p = META_ROOT / coll / year / month / row["pdf_stem"] / "metadata.json"
    if not p.exists():
        hits = list(META_ROOT.rglob(f"{row['pdf_stem']}/metadata.json"))
        if not hits:
            return {}
        p = hits[0]
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year-from", type=int, default=1939)
    ap.add_argument("--year-to",   type=int, default=1947)
    ap.add_argument("--all-failures", action="store_true")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--seed",  type=int, default=42)
    args = ap.parse_args()

    # Snapshot first — holding the live CSV open blocks the pipeline's
    # atomic rename on Windows (see ISSUES_AND_FIXES.md P7).
    snap = REVIEW_DIR.parent / "_status_snapshot_locreview.csv"
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(STATUS_CSV, snap)

    rows = []
    with snap.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            if r.get("grid_status") != "done" or r.get("location_status") != "failed":
                continue
            if not args.all_failures:
                try:
                    y = int(r.get("year") or 0)
                except ValueError:
                    continue
                if not (args.year_from <= y <= args.year_to):
                    continue
            rows.append(r)

    print(f"{len(rows)} grid-done / location-failed records match")
    random.seed(args.seed)
    random.shuffle(rows)
    sample = rows[: args.limit]

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    index = []
    for n, r in enumerate(sample, 1):
        meta = _meta_for(r)
        st   = meta.get("stages", {})
        grid = st.get("grid", {}) or {}
        loc  = st.get("location", {}) or {}
        cty  = st.get("county", {}) or {}

        grid_png = page_png = ""
        gp = grid.get("image_path") or r.get("grid_image_path") or ""
        if gp:
            src = Path(gp) if Path(gp).is_absolute() else OUTPUT_ROOT / gp
            if src.exists():
                grid_png = f"{n:03d}_{r['pdf_stem'][:40]}_grid.png"
                shutil.copy2(src, REVIEW_DIR / grid_png)
        ap_ = cty.get("annotated_path") or ""
        if ap_:
            src = Path(ap_) if Path(ap_).is_absolute() else OUTPUT_ROOT / ap_
            if src.exists():
                page_png = f"{n:03d}_{r['pdf_stem'][:40]}_page.png"
                shutil.copy2(src, REVIEW_DIR / page_png)

        index.append({
            "n": n, "pdf_stem": r["pdf_stem"],
            "collection": r.get("collection", ""), "year": r.get("year", ""),
            "month": r.get("month", ""),
            "form_type": grid.get("form_type", "") or r.get("grid_form_type", ""),
            "str_zone": grid.get("str_zone", ""),
            "str_strategy_hint": grid.get("str_strategy_hint", ""),
            "grid_method": grid.get("method", ""),
            "loc_error": loc.get("error", "") or r.get("location_error_type", ""),
            "raw_text_snippet": (loc.get("raw_text") or "")[:200],
            "grid_png": grid_png, "page_png": page_png,
        })

    idx_path = REVIEW_DIR / "index.csv"
    with idx_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=IDX_FIELDS)
        w.writeheader()
        w.writerows(index)

    with_page = sum(1 for i in index if i["page_png"])
    print(f"Review pack: {len(index)} records -> {REVIEW_DIR}")
    print(f"  {with_page} have a full-page PNG, index at {idx_path}")
    print("Study question per record: where IS the Sec/Twp/Rge on this form,")
    print("and does str_zone / str_strategy_hint point there?")


if __name__ == "__main__":
    main()

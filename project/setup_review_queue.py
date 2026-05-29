"""
setup_review_queue.py
=====================
Generates manual-review CSV files for well records that could not be
automatically resolved.  Run this once to create your review workbooks,
then fill in the FILL_* columns, then run apply_manual_review.py.

Outputs (D:/project_outputs_local/review/)
-----------------------------------------
  01_bounds_invalid.csv    449 records — one PLSS field present, one missing
  02_no_plss_address.csv   292 records — section known, BOTH twp+rng missing
  03_no_section.csv        132 records — dot detected but section blank
  README.txt               Step-by-step instructions

Usage
-----
  python setup_review_queue.py
  python setup_review_queue.py --output D:\\project_outputs_local
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_DEFAULT_OUTPUT = r"D:\project_outputs_local"
_D_PROJECT      = Path(r"D:\project_modular\project")
if _D_PROJECT.exists() and str(_D_PROJECT) not in sys.path:
    sys.path.insert(0, str(_D_PROJECT))


def _load_normalizers():
    try:
        from coord.field_normalizer import norm_section, norm_township, norm_range
        return norm_section, norm_township, norm_range
    except ImportError:
        return (lambda v: str(v or "").strip(),) * 3


# ---------------------------------------------------------------------------
# Category classifiers
# ---------------------------------------------------------------------------

def _classify_record(r: dict, norm_sect, norm_twp, norm_rng) -> str | None:
    """
    Return category string or None if record is already resolved / not relevant.
    Categories: 'bounds_invalid', 'no_plss', 'no_section'
    """
    sect = norm_sect(r.get("location_section", ""))
    twp  = norm_twp( r.get("location_township", ""))
    rng  = norm_rng( r.get("location_range", ""))
    dr   = str(r.get("dot_row", "")).strip()
    dc   = str(r.get("dot_col", "")).strip()
    conf = str(r.get("dot_confidence", "0")).strip()

    # Must have a detected dot position
    if not dr or not dc or conf in ("", "0", "0.0"):
        return None  # no_dot: needs pipeline re-run, not manual PLSS entry

    if not sect:
        return "no_section"
    if not twp and not rng:
        return "no_plss"
    # Has section + at least one of twp/rng: bounds_invalid territory
    # (The enricher already skips if both are empty; here one is missing)
    return None   # will be caught from coord_resolution_failures.csv


def _what_is_missing(flags: str, township: str, rng: str) -> str:
    """Human-readable label of what the user needs to supply."""
    if "township_invalid:None" in flags or not township:
        return "TOWNSHIP"
    if "range_invalid:None" in flags or not rng:
        return "RANGE"
    # Township number was extracted but is out of valid PLSS range.
    # E.g. Carter County (all S) got "18N" from OCR — likely a digit error.
    if "township_N_too_high" in flags or "township_S_too_high" in flags:
        return "TOWNSHIP (digit error — OCR value looks wrong)"
    if "range_E_too_high" in flags or "range_W_too_high" in flags:
        return "RANGE (digit error — OCR value looks wrong)"
    # Both range and township present but some other validation failure
    return "BOTH FIELDS (double-check PDF)"


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_review_queue(output_root: Path) -> None:
    review_dir = output_root / "review"
    review_dir.mkdir(parents=True, exist_ok=True)

    norm_sect, norm_twp, norm_rng = _load_normalizers()

    # --- Load source data ---------------------------------------------------
    status_csv   = output_root / "processing_status.csv"
    failures_csv = output_root / "coord_resolution_failures.csv"

    if not status_csv.exists():
        print(f"ERROR: {status_csv} not found. Run the pipeline first.")
        sys.exit(1)

    status_by_stem: dict[str, dict] = {}
    with status_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            status_by_stem[row["pdf_stem"]] = row

    fail_by_stem: dict[str, dict] = {}
    if failures_csv.exists():
        with failures_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                fail_by_stem[row["pdf_stem"]] = row

    resolved_stems: set[str] = set()
    coord_csv = output_root / "dot_coordinates.csv"
    if coord_csv.exists():
        with coord_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("resolved_lat"):
                    resolved_stems.add(row["pdf_stem"])

    # --- Category 1: bounds_invalid -----------------------------------------
    bounds_rows = []
    for stem, fail in fail_by_stem.items():
        if fail.get("resolution_source") != "bounds_invalid":
            continue
        if stem in resolved_stems:
            continue
        flags   = fail.get("flags", "")
        missing = _what_is_missing(flags, fail.get("township", ""), fail.get("range", ""))
        src     = status_by_stem.get(stem, {})
        bounds_rows.append({
            "review_status":    "",
            "pdf_stem":         stem,
            "pdf_path":         fail.get("pdf_path", ""),
            "collection":       fail.get("collection", ""),
            "year":             fail.get("year", ""),
            "month":            fail.get("month", ""),
            "county_name":      fail.get("county_name", ""),
            "section":          fail.get("section", ""),
            "township_given":   fail.get("township", ""),
            "range_given":      fail.get("range", ""),
            "what_is_missing":  missing,
            "dot_nw":           fail.get("unet_nw", ""),
            "dot_row":          fail.get("dot_row", ""),
            "dot_col":          fail.get("dot_col", ""),
            "dot_x_norm":       fail.get("dot_x_norm", ""),
            "dot_y_norm":       fail.get("dot_y_norm", ""),
            "flags":            flags,
            # --- USER FILLS BELOW ---
            "FILL_township":    "",   # e.g. "25N"  or  "25"
            "FILL_range":       "",   # e.g. "13E"  or  "13"
            "notes":            "",
        })

    # Sort: county + year so related records cluster together
    bounds_rows.sort(key=lambda r: (r["county_name"], r["year"], r["pdf_stem"]))

    out1 = review_dir / "01_bounds_invalid.csv"
    _write_csv(out1, bounds_rows, [
        "review_status", "pdf_stem", "pdf_path", "collection", "year", "month",
        "county_name", "section", "township_given", "range_given",
        "what_is_missing",
        "dot_nw", "dot_row", "dot_col", "dot_x_norm", "dot_y_norm",
        "flags",
        "FILL_township", "FILL_range", "notes",
    ])
    print(f"  01_bounds_invalid.csv  -> {len(bounds_rows):3d} records  [{out1}]")

    # --- Category 2: no_plss_address ----------------------------------------
    # dot_done records with dot detected + section present, but BOTH twp AND rng missing
    dot_done = [r for r in status_by_stem.values() if r.get("dot_status") == "done"]
    noplss_rows = []
    for r in dot_done:
        stem = r["pdf_stem"]
        if stem in resolved_stems or stem in fail_by_stem:
            continue
        sect = norm_sect(r.get("location_section", ""))
        twp  = norm_twp( r.get("location_township", ""))
        rng  = norm_rng( r.get("location_range", ""))
        dr   = str(r.get("dot_row", "")).strip()
        dc   = str(r.get("dot_col", "")).strip()
        conf = str(r.get("dot_confidence", "0")).strip()
        if not dr or not dc or conf in ("", "0", "0.0"):
            continue   # no dot
        if not sect:
            continue   # goes to no_section
        if twp or rng:
            continue   # has at least one PLSS field
        noplss_rows.append({
            "review_status":    "",
            "pdf_stem":         stem,
            "pdf_path":         r.get("pdf_path", ""),
            "collection":       r.get("collection", ""),
            "year":             r.get("year", ""),
            "month":            r.get("month", ""),
            "county_name":      r.get("county_name", ""),
            "section":          sect,
            "dot_nw":           r.get("dot_nw", ""),
            "dot_row":          dr,
            "dot_col":          dc,
            "dot_x_norm":       r.get("dot_x_norm", ""),
            "dot_y_norm":       r.get("dot_y_norm", ""),
            # --- USER FILLS BELOW ---
            "FILL_township":    "",   # e.g. "25N"
            "FILL_range":       "",   # e.g. "13E"
            "notes":            "",
        })

    noplss_rows.sort(key=lambda r: (r["county_name"], r["year"], r["pdf_stem"]))

    out2 = review_dir / "02_no_plss_address.csv"
    _write_csv(out2, noplss_rows, [
        "review_status", "pdf_stem", "pdf_path", "collection", "year", "month",
        "county_name", "section",
        "dot_nw", "dot_row", "dot_col", "dot_x_norm", "dot_y_norm",
        "FILL_township", "FILL_range", "notes",
    ])
    print(f"  02_no_plss_address.csv -> {len(noplss_rows):3d} records  [{out2}]")

    # --- Category 3: no_section ---------------------------------------------
    nosect_rows = []
    for r in dot_done:
        stem = r["pdf_stem"]
        if stem in resolved_stems or stem in fail_by_stem:
            continue
        sect = norm_sect(r.get("location_section", ""))
        twp  = norm_twp( r.get("location_township", ""))
        rng  = norm_rng( r.get("location_range", ""))
        dr   = str(r.get("dot_row", "")).strip()
        dc   = str(r.get("dot_col", "")).strip()
        conf = str(r.get("dot_confidence", "0")).strip()
        if not dr or not dc or conf in ("", "0", "0.0"):
            continue
        if sect:
            continue   # has section
        nosect_rows.append({
            "review_status":    "",
            "pdf_stem":         stem,
            "pdf_path":         r.get("pdf_path", ""),
            "collection":       r.get("collection", ""),
            "year":             r.get("year", ""),
            "month":            r.get("month", ""),
            "county_name":      r.get("county_name", ""),
            "township_given":   twp,
            "range_given":      rng,
            "dot_nw":           r.get("dot_nw", ""),
            "dot_row":          dr,
            "dot_col":          dc,
            "dot_x_norm":       r.get("dot_x_norm", ""),
            "dot_y_norm":       r.get("dot_y_norm", ""),
            "location_section_raw": r.get("location_section", ""),
            # --- USER FILLS BELOW ---
            "FILL_section":     "",   # e.g. "14"
            "FILL_township":    "",   # e.g. "25N"  (fill only if township_given is also blank)
            "FILL_range":       "",   # e.g. "13E"  (fill only if range_given is also blank)
            "notes":            "",
        })

    nosect_rows.sort(key=lambda r: (r["county_name"], r["year"], r["pdf_stem"]))

    out3 = review_dir / "03_no_section.csv"
    _write_csv(out3, nosect_rows, [
        "review_status", "pdf_stem", "pdf_path", "collection", "year", "month",
        "county_name", "township_given", "range_given",
        "dot_nw", "dot_row", "dot_col", "dot_x_norm", "dot_y_norm",
        "location_section_raw",
        "FILL_section", "FILL_township", "FILL_range", "notes",
    ])
    print(f"  03_no_section.csv      -> {len(nosect_rows):3d} records  [{out3}]")

    # --- Category 4: no_dot_partial_plss -----------------------------------
    # Records where the dot model returned no position (dot_status=done, conf=0)
    # OR the grid was not found (dot_status=skipped), but:
    #   - section IS present
    #   - exactly ONE of township/range is present (other is missing)
    # After the user fills the missing field, running apply_manual_review.py
    # will place these wells at the section CENTRE (approximate, ±½ mile).
    # Less precise than a dot-interpolated location, but far better than nothing.
    nodot_partial_rows = []
    for r in status_by_stem.values():
        stem     = r["pdf_stem"]
        dot_st   = r.get("dot_status", "")
        if dot_st not in ("done", "skipped"):
            continue
        if stem in resolved_stems:
            continue
        if stem in fail_by_stem:
            continue   # already in bounds_invalid list
        sect = norm_sect(r.get("location_section",  ""))
        twp  = norm_twp( r.get("location_township", ""))
        rng  = norm_rng( r.get("location_range",    ""))
        if not sect:
            continue    # no section → can't do centroid even with twp+rng
        # Must be missing exactly one of twp/rng (not both, not neither)
        if bool(twp) == bool(rng):
            continue    # both present (already handled elsewhere) or both missing
        # For dot_done: must have NO dot position (conf=0 or empty dot_row)
        if dot_st == "done":
            dr   = str(r.get("dot_row",       "")).strip()
            conf = str(r.get("dot_confidence","0")).strip()
            if dr and conf not in ("", "0", "0.0"):
                continue   # has a dot — covered by other categories
        nodot_partial_rows.append({
            "review_status":    "",
            "pdf_stem":         stem,
            "pdf_path":         r.get("pdf_path",    ""),
            "collection":       r.get("collection",  ""),
            "year":             r.get("year",         ""),
            "month":            r.get("month",        ""),
            "county_name":      r.get("county_name",  ""),
            "section":          sect,
            "township_given":   twp,
            "range_given":      rng,
            "dot_status":       dot_st,
            "what_is_missing":  "TOWNSHIP" if rng else "RANGE",
            "result_precision": "APPROXIMATE (section centre, ±0.5 mi)",
            # --- USER FILLS BELOW ---
            "FILL_township":    "",   # fill if township_given is blank
            "FILL_range":       "",   # fill if range_given is blank
            "notes":            "",
        })

    nodot_partial_rows.sort(key=lambda r: (r["county_name"], r["year"], r["pdf_stem"]))

    out4 = review_dir / "04_approx_centroid_partial_plss.csv"
    _write_csv(out4, nodot_partial_rows, [
        "review_status", "pdf_stem", "pdf_path", "collection", "year", "month",
        "county_name", "section", "township_given", "range_given",
        "dot_status", "what_is_missing", "result_precision",
        "FILL_township", "FILL_range", "notes",
    ])
    print(f"  04_approx_centroid_partial_plss.csv -> {len(nodot_partial_rows):3d} records  [{out4}]")

    # --- README -------------------------------------------------------------
    _write_readme(review_dir, len(bounds_rows), len(noplss_rows), len(nosect_rows),
                  len(nodot_partial_rows))
    print(f"  README.txt             -> {review_dir / 'README.txt'}")

    total_rev = len(bounds_rows)+len(noplss_rows)+len(nosect_rows)+len(nodot_partial_rows)
    print(f"\nTotal reviewable: {total_rev} records")
    print(f"  START with 01_bounds_invalid.csv -- easiest, one field per record.")
    print(f"  Category 4 (04_approx...) gives approximate section-centre coords.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig for Excel BOM
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _write_readme(review_dir: Path, n1: int, n2: int, n3: int, n4: int = 0) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    text = f"""MANUAL REVIEW QUEUE — PLSS Coordinate Enrichment
Generated: {now}
=============================================================

CURRENT STATUS
--------------
  Resolved (precise dot interpolation) : ~2,178 wells
  Resolved (approximate section centre): ~1,199 wells
  TOTAL mapped: ~3,377 / 4,591 = 73.4%

  Completing these review files can add another 400-900 wells.

=============================================================
FILES (ordered easiest → hardest)
=============================================================

01_bounds_invalid.csv  ({n1} records)
  ▶ EASIEST — one PLSS field is already known, just one is wrong/missing.
  Result: PRECISE dot-interpolated coordinate if corrected.
  What to do:
    1. Open the PDF listed in "pdf_path"
    2. Look at the HEADER BLOCK (top of page 1) for the PLSS address.
       Usually printed as: Sec. 14, Twp. 25N, Rge. 13E
    3. Fill in the corrected/missing field:
       • FILL_township  — format: 25N  or  25S  or  25
       • FILL_range     — format: 13E  or  13W  or  13
    4. Set review_status = "confirmed"
    5. If unreadable or PLSS not shown: set review_status = "skip"

02_no_plss_address.csv  ({n2} records)
  ▶ MEDIUM — section known, BOTH township AND range missing.
  Result: PRECISE dot-interpolated coordinate if corrected.
  What to do: fill BOTH FILL_township and FILL_range.

03_no_section.csv  ({n3} records)
  ▶ HARDER — dot detected but section number missing.
  Result: PRECISE dot-interpolated coordinate if corrected.
  What to do:
    Fill FILL_section (1-36), plus FILL_township/FILL_range if also blank.

04_approx_centroid_partial_plss.csv  ({n4} records)
  ▶ MEDIUM — no dot detected (or no grid image), section known, ONE PLSS field missing.
  Result: APPROXIMATE location (section centre, ±0.5 mile accuracy).
  These will NOT have a precise dot position — just the section centroid.
  What to do:
    Fill the missing field shown in "what_is_missing":
       • FILL_township  — e.g. 25N  (if township_given column is blank)
       • FILL_range     — e.g. 13E  (if range_given column is blank)
    Set review_status = "confirmed"
    The result_precision column reminds you this gives ±0.5 mile accuracy.

=============================================================
HOW TO READ OKLAHOMA PLSS FROM THE PDF
=============================================================

The PLSS address is almost always in the header block at the TOP
of the first page, formatted like:

  Section 14, Township 25 North, Range 13 East
  Sec. 14, T25N, R13E
  S14-T25N-R13E

Key rules:
  • Section: a number 1–36
  • Township: a number + N or S (e.g. 25N means 25 North)
              Valid Oklahoma range: 1N–29N, 1S–8S
  • Range: a number + E or W (e.g. 13E means 13 East)
           Valid Oklahoma range: 1E–26E, 1W–28W

Counties EAST of Indian Meridian (~97.1°W longitude) use Range EAST:
  Tulsa, Creek, Okmulgee, Wagoner, Muskogee, Cherokee, Sequoyah,
  Rogers, Washington, Nowata, Craig, Mayes, Ottawa, Delaware, Adair

Counties WEST use Range WEST.

Oklahoma county PLSS hints:
  Nowata County    → Range 14E–17E,  Township 24N–27N
  Washington County → Range 12E–14E, Township 25N–27N
  Creek County     → Range 7E–13E,   Township 14N–20N
  Tulsa County     → Range 11E–14E,  Township 17N–21N
  Pawnee County    → Range 5E–9E,    Township 18N–22N
  Carter County    → Range 1W–6W,    Township 1S–8S  (SOUTH, not NORTH!)
  Stephens County  → Range 4W–8W,    Township 1N–5N

=============================================================
AFTER FILLING IN THE CSV FILES
=============================================================

Run:
  cd D:\\project_modular\\project
  python apply_manual_review.py

This will:
  1. Read all filled CSVs from this review/ folder
  2. Show how many new wells would be resolved (dry run)
  3. Run with --apply to actually update dot_coordinates.csv

=============================================================
TIPS FOR SPEED
=============================================================

• In Excel: use Ctrl+D to fill down repeated values

• The "what_is_missing" column tells you exactly which field to fill
  (TOWNSHIP or RANGE) — saves time reading every column.

• Sort by county_name to batch similar records together.

• Save frequently. The CSV is safe to partially fill.

• For category 04 records: even a rough section-centre coordinate
  (±0.5 mile) is better than no coordinate at all for most use cases.

=============================================================
"""
    (review_dir / "README.txt").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate manual PLSS review queue")
    ap.add_argument("--output", default=_DEFAULT_OUTPUT,
                    help=f"Pipeline output root (default: {_DEFAULT_OUTPUT})")
    args = ap.parse_args()

    output_root = Path(args.output)
    print(f"\n=== Building manual review queue in {output_root / 'review'} ===\n")
    build_review_queue(output_root)
    print(f"\nDone. Open the CSVs in Excel, fill in FILL_* columns, then run:")
    print(f"  python apply_manual_review.py\n")


if __name__ == "__main__":
    main()

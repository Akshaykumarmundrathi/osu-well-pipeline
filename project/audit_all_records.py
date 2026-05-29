"""
audit_all_records.py
====================
Exhaustive audit of every record in the pipeline (1911–1916).
Reads processing_status.csv + dot_coordinates.csv and produces
a full set of categorised CSV files showing exactly what happened
to each record and why it did or did not get mapped.

Output: D:\\project_outputs_local\\audit\\
  00_summary_by_year.csv         -- year × category counts + mapping %
  00_summary_by_county.csv       -- county × category counts
  01_A_resolved.csv              -- 2,000+ mapped records with source/flags
  02_B_coord_failed.csv          -- has dot + valid PLSS but RDS miss/parse fail
  03_C_bounds_invalid.csv        -- has dot, one PLSS field out of bounds
  04_D_no_plss.csv               -- has dot, both township+range missing
  05_E_no_section.csv            -- has dot, section missing
  06_F_no_dot_found.csv          -- dot detection returned nothing (conf=0)
  07_G_no_grid.csv               -- grid detection found nothing, dot skipped
  08_centroid_candidates.csv     -- F+G records with full PLSS (Pass 5 eligible)
  09_all_records.csv             -- every record, one row, all fields merged

Usage
-----
  python audit_all_records.py
  python audit_all_records.py --output D:\\project_outputs_local
  python audit_all_records.py --output D:\\project_outputs_local --open
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

_DEFAULT_OUTPUT = r"D:\project_outputs_local"
_D_PROJECT      = Path(r"D:\project_modular\project")
if _D_PROJECT.exists() and str(_D_PROJECT) not in sys.path:
    sys.path.insert(0, str(_D_PROJECT))


# ---------------------------------------------------------------------------
# Category definitions
# ---------------------------------------------------------------------------

CAT_RESOLVED        = "A_resolved"
CAT_COORD_FAILED    = "B_coord_failed"
CAT_BOUNDS_INVALID  = "C_bounds_invalid"
CAT_NO_PLSS         = "D_no_plss"
CAT_NO_SECTION      = "E_no_section"
CAT_NO_DOT          = "F_no_dot_found"
CAT_NO_GRID         = "G_no_grid"
CAT_UNKNOWN         = "X_unknown"

CAT_LABELS = {
    CAT_RESOLVED:       "Resolved (mapped to lat/lon)",
    CAT_COORD_FAILED:   "Coord failed (RDS miss or parse error)",
    CAT_BOUNDS_INVALID: "Bounds invalid (PLSS value out of OK range)",
    CAT_NO_PLSS:        "No PLSS address (township+range both missing)",
    CAT_NO_SECTION:     "Section missing",
    CAT_NO_DOT:         "Dot not found (model returned confidence=0)",
    CAT_NO_GRID:        "No grid detected (dot stage skipped)",
    CAT_UNKNOWN:        "Unclassified",
}

RECOVERY_HINTS = {
    CAT_RESOLVED:       "Already mapped. Verify coordinates look correct on map.",
    CAT_COORD_FAILED:   "Section likely not in RDS. Manual verify PDF then contact data team.",
    CAT_BOUNDS_INVALID: "One PLSS value is out of Oklahoma range — open PDF and fill FILL_* in 01_bounds_invalid.csv (review queue).",
    CAT_NO_PLSS:        "Township and range both unreadable from PDF — fill FILL_township + FILL_range in 02_no_plss_address.csv.",
    CAT_NO_SECTION:     "Section number unreadable — fill FILL_section in 03_no_section.csv.",
    CAT_NO_DOT:         "Well dot not detected on grid image. Run with --include-centroid for approximate section-centre location, or re-run dot detection.",
    CAT_NO_GRID:        "Grid image not available for dot detection. Run with --include-centroid for approximate section-centre location.",
    CAT_UNKNOWN:        "Edge case — inspect manually.",
}


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def load_data(output_root: Path) -> tuple[list[dict], dict[str, dict]]:
    """Load processing_status.csv and dot_coordinates.csv."""
    status_csv = output_root / "processing_status.csv"
    coord_csv  = output_root / "dot_coordinates.csv"

    if not status_csv.exists():
        print(f"ERROR: {status_csv} not found.")
        sys.exit(1)

    with status_csv.open(newline="", encoding="utf-8") as f:
        status_rows = list(csv.DictReader(f))

    coord_data: dict[str, dict] = {}
    if coord_csv.exists():
        with coord_csv.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                coord_data[r["pdf_stem"]] = r

    print(f"  Loaded {len(status_rows):,} status rows, "
          f"{len(coord_data):,} coord rows")
    return status_rows, coord_data


def _plss_completeness(r: dict) -> tuple[bool, bool, bool]:
    """Returns (has_section, has_township, has_range)."""
    has_s = bool(r.get("location_section",  "").strip())
    has_t = bool(r.get("location_township", "").strip())
    has_r = bool(r.get("location_range",    "").strip())
    return has_s, has_t, has_r


def _pdf_open_hint(r: dict) -> str:
    """Best path to open the source PDF for review."""
    zip_p = r.get("zip_path",      "").strip()
    int_p = r.get("internal_path", "").strip()
    pdf_p = r.get("pdf_path",      "").strip()
    if zip_p and int_p:
        return f"{zip_p}  >>  {int_p}"
    return pdf_p or ""


def _resolution_flags(r: dict, coord: dict | None) -> tuple[str, str]:
    """Return (resolution_source, flags) from coord row or '' if not available."""
    if coord:
        return coord.get("resolution_source", ""), coord.get("flags", "")
    return "", ""


def classify(status_rows: list[dict], coord_data: dict[str, dict]) -> dict[str, list[dict]]:
    """
    Assign every record to exactly one category.
    Returns {category: [merged_row, ...]}
    """
    cats: dict[str, list[dict]] = defaultdict(list)

    for r in status_rows:
        stem     = r.get("pdf_stem", "")
        dot_st   = r.get("dot_status", "")
        dot_row  = r.get("dot_row",  "").strip()
        dot_col  = r.get("dot_col",  "").strip()
        has_s, has_t, has_r = _plss_completeness(r)
        coord    = coord_data.get(stem)
        resolved = bool(coord and coord.get("resolved_lat", "").strip())

        # Build a merged row with essential fields from both sources
        merged = {
            # Identity
            "year":         r.get("year", ""),
            "month":        r.get("month", ""),
            "collection":   r.get("collection", ""),
            "pdf_stem":     stem,
            "pdf_path":     r.get("pdf_path", ""),
            "zip_path":     r.get("zip_path", ""),
            "internal_path": r.get("internal_path", ""),
            "open_hint":    _pdf_open_hint(r),
            # County
            "county_name":  r.get("county_name", ""),
            # PLSS from OCR
            "location_section":  r.get("location_section",  ""),
            "location_township": r.get("location_township", ""),
            "location_range":    r.get("location_range",    ""),
            # PLSS completeness flags
            "has_section":  "yes" if has_s else "no",
            "has_township": "yes" if has_t else "no",
            "has_range":    "yes" if has_r else "no",
            "has_full_plss": "yes" if (has_s and has_t and has_r) else "no",
            # Dot detection
            "dot_status":     dot_st,
            "dot_row":        dot_row,
            "dot_col":        dot_col,
            "dot_nw":         r.get("dot_nw", ""),
            "dot_confidence": r.get("dot_confidence", ""),
            "dot_x_norm":     r.get("dot_x_norm", ""),
            "dot_y_norm":     r.get("dot_y_norm", ""),
            # Coordinate result
            "resolved_lat":       coord.get("resolved_lat",       "") if coord else "",
            "resolved_lon":       coord.get("resolved_lon",       "") if coord else "",
            "resolution_source":  coord.get("resolution_source",  "") if coord else "",
            "coord_flags":        coord.get("flags",              "") if coord else "",
            # Quadrant
            "ocr_quadrant_db":    coord.get("ocr_quadrant_db", "") if coord else "",
            "unet_nw":            coord.get("unet_nw",         "") if coord else r.get("dot_nw", ""),
            # Grid stage
            "grid_confidence":    r.get("grid_confidence", ""),
            "grid_method":        r.get("grid_method",     ""),
            "grid_image_path":    r.get("grid_image_path", ""),
            "grid_error_type":    r.get("grid_error_type", ""),
            # Pipeline status fields
            "latlong_status":  r.get("latlong_status",  ""),
            "grid_status":     r.get("grid_status",     ""),
            "location_status": r.get("location_status", ""),
            "county_status":   r.get("county_status",   ""),
            "location_confidence": r.get("location_confidence", ""),
            "county_confidence":   r.get("county_confidence",   ""),
            # Model tier
            "model_tier": r.get("model_tier", ""),
            "decade":     r.get("decade",     ""),
        }

        # Assign category
        if resolved:
            cat = CAT_RESOLVED
        elif dot_st == "skipped":
            cat = CAT_NO_GRID
        elif dot_st == "done" and dot_row and dot_col:
            # Has dot position — why not resolved?
            src = merged["resolution_source"]
            if src == "bounds_invalid":
                cat = CAT_BOUNDS_INVALID
            elif src in ("rds_miss", "parse_failed"):
                cat = CAT_COORD_FAILED
            elif not has_s and not has_t and not has_r:
                cat = CAT_COORD_FAILED     # shouldn't happen but guard
            else:
                cat = CAT_COORD_FAILED     # catch-all for non-empty src
        elif dot_st == "done" and not (dot_row and dot_col):
            # Dot stage ran but found nothing
            if not has_s and not has_t and not has_r:
                cat = CAT_NO_PLSS   # truly nothing
            elif not has_s:
                cat = CAT_NO_SECTION
            elif not has_t and not has_r:
                cat = CAT_NO_PLSS
            else:
                cat = CAT_NO_DOT
        else:
            cat = CAT_UNKNOWN

        # Reclassify if coord has richer info
        if not resolved and coord:
            src = merged["resolution_source"]
            if src == "bounds_invalid":
                cat = CAT_BOUNDS_INVALID
            elif dot_st == "done" and dot_row and dot_col:
                if not has_s:
                    cat = CAT_NO_SECTION
                elif not has_t and not has_r:
                    cat = CAT_NO_PLSS
                elif src in ("rds_miss",):
                    cat = CAT_COORD_FAILED

        merged["category"] = cat
        merged["category_label"] = CAT_LABELS.get(cat, cat)
        merged["recovery_hint"]  = RECOVERY_HINTS.get(cat, "")

        # Centroid candidate flag
        merged["centroid_candidate"] = (
            "yes" if (
                cat in (CAT_NO_DOT, CAT_NO_GRID) and
                has_s and has_t and has_r
            ) else "no"
        )

        cats[cat].append(merged)

    return dict(cats)


def write_category_csv(path: Path, rows: list[dict], extra_cols: list[str] | None = None) -> None:
    """Write a category-specific CSV, ordering the most useful columns first."""
    if not rows:
        return

    base_cols = [
        "year", "month", "collection", "county_name",
        "pdf_stem",
        "location_section", "location_township", "location_range",
        "has_section", "has_township", "has_range", "has_full_plss",
        "category", "recovery_hint",
        "resolution_source", "coord_flags",
        "resolved_lat", "resolved_lon",
        "dot_status", "dot_row", "dot_col", "dot_nw", "dot_confidence",
        "dot_x_norm", "dot_y_norm",
        "grid_confidence", "grid_method",
        "centroid_candidate",
        "open_hint",
        "pdf_path",
    ]
    if extra_cols:
        base_cols += [c for c in extra_cols if c not in base_cols]

    # Add any remaining columns not yet listed
    all_keys = list(dict.fromkeys(
        base_cols + [k for k in rows[0].keys() if k not in base_cols]
    ))

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r.get("year",""), r.get("pdf_stem",""))))
    print(f"  {path.name:50s} {len(rows):5,d} rows")


def write_summary_by_year(audit_dir: Path,
                          cats: dict[str, list[dict]],
                          all_rows_count: int) -> None:
    """Write year × category matrix as a CSV."""
    # Build year → category → count
    year_cats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total_by_year: dict[str, int] = defaultdict(int)

    cat_order = [CAT_RESOLVED, CAT_BOUNDS_INVALID, CAT_NO_PLSS, CAT_NO_SECTION,
                 CAT_COORD_FAILED, CAT_NO_DOT, CAT_NO_GRID, CAT_UNKNOWN]

    for cat, rows in cats.items():
        for r in rows:
            yr = r.get("year", "0") or "0"
            year_cats[yr][cat] += 1
            total_by_year[yr] += 1

    path = audit_dir / "00_summary_by_year.csv"
    fieldnames = (["year", "total", "resolved", "mapped_pct"] +
                  [c.replace("_", " ") for c in cat_order] +
                  ["centroid_eligible"])

    rows_out = []
    for yr in sorted(year_cats.keys()):
        total = total_by_year[yr]
        resolved = year_cats[yr].get(CAT_RESOLVED, 0)
        centroid_elig = sum(
            1 for r in (cats.get(CAT_NO_DOT, []) + cats.get(CAT_NO_GRID, []))
            if r.get("year", "0") == yr and r.get("centroid_candidate") == "yes"
        )
        row = {
            "year":    yr,
            "total":   total,
            "resolved": resolved,
            "mapped_pct": f"{resolved/total*100:.1f}%" if total else "0%",
            "centroid_eligible": centroid_elig,
        }
        for cat in cat_order:
            row[cat.replace("_", " ")] = year_cats[yr].get(cat, 0)
        rows_out.append(row)

    # Totals row
    grand_total    = sum(total_by_year.values())
    grand_resolved = sum(year_cats[yr].get(CAT_RESOLVED, 0)
                         for yr in year_cats)
    grand_centroid = sum(
        1 for rows in [cats.get(CAT_NO_DOT, []), cats.get(CAT_NO_GRID, [])]
        for r in rows if r.get("centroid_candidate") == "yes"
    )
    total_row = {
        "year": "TOTAL",
        "total": grand_total,
        "resolved": grand_resolved,
        "mapped_pct": f"{grand_resolved/grand_total*100:.1f}%" if grand_total else "0%",
        "centroid_eligible": grand_centroid,
    }
    for cat in cat_order:
        total_row[cat.replace("_", " ")] = sum(
            year_cats[yr].get(cat, 0) for yr in year_cats
        )
    rows_out.append(total_row)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows_out)
    print(f"  {path.name:50s} {len(rows_out):5,d} rows")


def write_summary_by_county(audit_dir: Path, cats: dict[str, list[dict]]) -> None:
    """Write county × category matrix."""
    county_cats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total_by_county: dict[str, int] = defaultdict(int)

    cat_order = [CAT_RESOLVED, CAT_BOUNDS_INVALID, CAT_NO_PLSS, CAT_NO_SECTION,
                 CAT_COORD_FAILED, CAT_NO_DOT, CAT_NO_GRID, CAT_UNKNOWN]

    for cat, rows in cats.items():
        for r in rows:
            county = r.get("county_name", "UNKNOWN") or "UNKNOWN"
            county_cats[county][cat] += 1
            total_by_county[county] += 1

    path = audit_dir / "00_summary_by_county.csv"
    fieldnames = (["county", "total", "resolved", "mapped_pct"] +
                  [c.replace("_", " ") for c in cat_order] +
                  ["centroid_eligible"])

    rows_out = []
    for county in sorted(county_cats.keys()):
        total = total_by_county[county]
        resolved = county_cats[county].get(CAT_RESOLVED, 0)
        centroid_elig = sum(
            1 for r in (cats.get(CAT_NO_DOT, []) + cats.get(CAT_NO_GRID, []))
            if (r.get("county_name","") or "UNKNOWN") == county and
               r.get("centroid_candidate") == "yes"
        )
        row = {
            "county": county,
            "total":  total,
            "resolved": resolved,
            "mapped_pct": f"{resolved/total*100:.1f}%" if total else "0%",
            "centroid_eligible": centroid_elig,
        }
        for cat in cat_order:
            row[cat.replace("_", " ")] = county_cats[county].get(cat, 0)
        row["centroid_eligible"] = centroid_elig
        rows_out.append(row)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows_out, key=lambda r: -r["total"]))
    print(f"  {path.name:50s} {len(rows_out):5,d} rows")


def run(output_root: Path) -> None:
    audit_dir = output_root / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Exhaustive Pipeline Audit ===")
    print(f"  Output root : {output_root}")
    print(f"  Audit dir   : {audit_dir}")

    # Load data
    print("\nLoading data...")
    status_rows, coord_data = load_data(output_root)

    # Classify every record
    print("Classifying records...")
    cats = classify(status_rows, coord_data)

    # Print category summary
    total = len(status_rows)
    print(f"\n  {'Category':<42}  {'Count':>6}  {'Pct':>6}")
    print(f"  {'-'*56}")
    for cat in sorted(cats.keys()):
        n = len(cats[cat])
        pct = n / total * 100
        centroid = sum(1 for r in cats[cat] if r.get("centroid_candidate") == "yes")
        centroid_note = f"  ({centroid} centroid-eligible)" if centroid else ""
        print(f"  {CAT_LABELS.get(cat,cat):<42}  {n:>6,}  {pct:>5.1f}%{centroid_note}")
    print(f"  {'-'*56}")
    print(f"  {'TOTAL':<42}  {total:>6,}  100.0%")

    # Centroid summary
    total_centroid = sum(
        1 for cat in (CAT_NO_DOT, CAT_NO_GRID)
        for r in cats.get(cat, [])
        if r.get("centroid_candidate") == "yes"
    )
    print(f"\n  Section-centroid eligible (Pass 5): {total_centroid:,}")
    resolved_now = len(cats.get(CAT_RESOLVED, []))
    centroid_pct = (resolved_now + total_centroid) / total * 100
    print(f"  Potential after centroid pass     : "
          f"{resolved_now + total_centroid:,} / {total:,} = {centroid_pct:.1f}%")

    # Write summary tables
    print(f"\nWriting audit CSVs to {audit_dir}...\n")
    write_summary_by_year(audit_dir, cats, total)
    write_summary_by_county(audit_dir, cats)

    # Write per-category CSVs
    write_category_csv(
        audit_dir / "01_A_resolved.csv",
        cats.get(CAT_RESOLVED, []),
        extra_cols=["resolved_lat", "resolved_lon", "resolution_source", "coord_flags",
                    "ocr_quadrant_db", "unet_nw"],
    )
    write_category_csv(
        audit_dir / "02_B_coord_failed.csv",
        cats.get(CAT_COORD_FAILED, []),
        extra_cols=["resolution_source", "coord_flags"],
    )
    write_category_csv(
        audit_dir / "03_C_bounds_invalid.csv",
        cats.get(CAT_BOUNDS_INVALID, []),
        extra_cols=["coord_flags"],
    )
    write_category_csv(
        audit_dir / "04_D_no_plss.csv",
        cats.get(CAT_NO_PLSS, []),
    )
    write_category_csv(
        audit_dir / "05_E_no_section.csv",
        cats.get(CAT_NO_SECTION, []),
    )
    write_category_csv(
        audit_dir / "06_F_no_dot_found.csv",
        cats.get(CAT_NO_DOT, []),
        extra_cols=["grid_confidence", "grid_method", "grid_image_path"],
    )
    write_category_csv(
        audit_dir / "07_G_no_grid.csv",
        cats.get(CAT_NO_GRID, []),
        extra_cols=["grid_confidence", "grid_method", "grid_error_type"],
    )

    # Centroid candidates (F + G with complete PLSS)
    centroid_rows = [
        r for cat in (CAT_NO_DOT, CAT_NO_GRID)
        for r in cats.get(cat, [])
        if r.get("centroid_candidate") == "yes"
    ]
    write_category_csv(
        audit_dir / "08_centroid_candidates.csv",
        centroid_rows,
        extra_cols=["dot_status", "grid_confidence", "grid_method"],
    )

    # Edge cases
    if cats.get(CAT_UNKNOWN):
        write_category_csv(
            audit_dir / "09_X_unknown.csv",
            cats.get(CAT_UNKNOWN, []),
        )

    # Master: all records
    all_merged = [r for rows in cats.values() for r in rows]
    write_category_csv(
        audit_dir / "10_all_records.csv",
        all_merged,
    )

    # Write README
    readme = audit_dir / "README.txt"
    with readme.open("w", encoding="utf-8") as f:
        f.write("Pipeline Audit Output\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Generated from: {output_root}\n")
        f.write(f"Total records : {total:,}\n")
        f.write(f"Resolved      : {resolved_now:,} ({resolved_now/total*100:.1f}%)\n")
        f.write(f"Centroid-elig : {total_centroid:,} (Pass 5 approximate coords)\n\n")
        f.write("FILES\n-----\n")
        f.write("  00_summary_by_year.csv    - year-by-year breakdown\n")
        f.write("  00_summary_by_county.csv  - county-by-county breakdown\n")
        f.write("  01_A_resolved.csv         - successfully mapped records\n")
        f.write("  02_B_coord_failed.csv     - dot found, RDS couldn't resolve\n")
        f.write("  03_C_bounds_invalid.csv   - PLSS value out of OK range\n")
        f.write("  04_D_no_plss.csv          - both twp+rng missing\n")
        f.write("  05_E_no_section.csv       - section number missing\n")
        f.write("  06_F_no_dot_found.csv     - dot model found nothing (conf=0)\n")
        f.write("  07_G_no_grid.csv          - grid not detected, dot skipped\n")
        f.write("  08_centroid_candidates.csv- F+G with full PLSS (centroid eligible)\n")
        f.write("  10_all_records.csv        - master list, all categories merged\n\n")
        f.write("RECOVERY OPTIONS\n----------------\n")
        for cat, hint in RECOVERY_HINTS.items():
            f.write(f"  {cat}: {hint}\n")
        f.write("\nTo add approximate section-centroid coordinates for F+G records:\n")
        f.write("  cd C:\\Users\\akshay\\Downloads\\project_modular\\project\n")
        f.write("  python run_coord_enrichment.py --all-dot-done --include-centroid\n")
        f.write("\nFor manual review (categories C, D, E):\n")
        f.write("  cd D:\\project_modular\\project\n")
        f.write("  python apply_manual_review.py  (after filling review CSVs)\n")

    print(f"\n  README.txt written")
    print(f"\n=== Audit complete ===")
    print(f"  Open {audit_dir} in Explorer to review the files.")
    if total_centroid > 0:
        print(f"\n  TIP: Run with --include-centroid to add {total_centroid:,} "
              f"approximate coordinates:")
        print(f"  cd C:\\Users\\akshay\\Downloads\\project_modular\\project")
        print(f"  python run_coord_enrichment.py --all-dot-done --include-centroid")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Exhaustive audit of all pipeline records"
    )
    ap.add_argument("--output", default=_DEFAULT_OUTPUT,
                    help=f"Pipeline output root (default: {_DEFAULT_OUTPUT})")
    ap.add_argument("--open", action="store_true",
                    help="Open the audit folder in Windows Explorer after writing")
    args = ap.parse_args()

    run(Path(args.output))

    if args.open:
        audit_dir = Path(args.output) / "audit"
        os.startfile(str(audit_dir))


if __name__ == "__main__":
    main()

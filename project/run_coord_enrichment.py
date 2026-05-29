"""
run_coord_enrichment.py  —  Resolve dot positions to exact lat/lon
===================================================================

Reads processed records from processing_status.csv, queries the PLSS
PostgreSQL database (AWS RDS), and writes exact lat/lon coordinates for
every dot-detected well.

How it works
------------
1. Reads D:\\project_outputs_local\\processing_status.csv
2. Normalises column names to match coord_enricher expectations
3. For each dot-detected record:
     a. Looks up Section/Township/Range in plss_grid (RDS)
     b. Finds the exact 8x8 grid cell via dot_nw label (e.g. "SW-NW-NE")
     c. Bilinear-interpolates using (dot_x_norm, dot_y_norm) -> exact lat/lon
4. Validates against county_name for sanity
5. Writes outputs to D:\\project_outputs_local\\

Two-pass enrichment
-------------------
Pass 1: normal enrichment using per-county direction statistics from RDS
  - When OCR gives bare "13" instead of "13N", uses county stats to pick
    the most probable direction first (e.g. Carter County -> S first)
Pass 2: for records that returned rds_miss in Pass 1, tries all valid
  NS x EW direction combinations + adjacent sections (+-1 and +-2)

Outputs
-------
  dot_coordinates.csv           -- all records, resolved + unresolved
  coord_resolution_failures.csv -- records that could not be resolved + reason
  coord_resolution_log.csv      -- compact per-record trace

Prerequisites
-------------
  pip install psycopg2-binary python-dotenv

Add to your .env file (C:\\Users\\akshay\\Downloads\\project_modular\\.env):
  RDS_HOST=your-endpoint.rds.amazonaws.com
  RDS_PORT=5432
  RDS_DBNAME=Oklahomaplss
  RDS_USER=LookUpMaster
  RDS_PASSWORD=your-password

Usage
-----
  python run_coord_enrichment.py
  python run_coord_enrichment.py --output D:\\project_outputs_local
  python run_coord_enrichment.py --year 1913     # single year only
  python run_coord_enrichment.py --dry-run       # test DB connection only
  python run_coord_enrichment.py --all-dot-done  # bypass review gate
  python run_coord_enrichment.py --all-dot-done --include-centroid
                                                 # also use section centroid
                                                 # for PLSS-complete no-dot records
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env from Downloads project root
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).parent.parent / ".env"
if _ENV_PATH.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_PATH, override=False)
    except ImportError:
        # Manual parse if python-dotenv not installed
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip().strip('"\'')

# ---------------------------------------------------------------------------
# Make D:\project_modular\project importable (coord + ocr modules live there)
# Override via env var D_PROJECT_ROOT if repo is on a different drive/path.
# ---------------------------------------------------------------------------
_D_PROJECT = Path(os.environ.get("D_PROJECT_ROOT", r"D:\project_modular\project"))
if _D_PROJECT.exists() and str(_D_PROJECT) not in sys.path:
    sys.path.insert(0, str(_D_PROJECT))

# ---------------------------------------------------------------------------
# STR value normalisation
# ---------------------------------------------------------------------------

def _normalise_str_value(v: str) -> str:
    """
    Normalise a Section/Township/Range string from OCR output.

    Strips surrounding whitespace and collapses spurious internal spaces
    between a number and its trailing direction letter so the RDS lookup
    matches the canonical DB format:
      '25 N'  -> '25N'   '14 W' -> '14W'   ' 7T '  -> '7T'
    """
    v = (v or "").strip()
    # Collapse "25 N" / "14 W" style -> "25N" / "14W"
    v = re.sub(r"(\d)\s+([NSEWnsew])$", r"\1\2", v)
    return v


def _row_col_to_quad(dot_row: str, dot_col: str) -> str:
    """
    Derive the DB-format quadrant label (e.g. 'NW-SW-NE') from UNet
    (row, col) output -- used when dot_nw is missing or empty.
    Purely geometric, no OCR involved.
    """
    try:
        from ocr.quadrant_extractor import row_col_to_label
        label = row_col_to_label(int(dot_row), int(dot_col))
        return label or ""
    except Exception:
        return ""


def _xynorm_to_quad(x_norm: str, y_norm: str) -> str:
    """
    Derive quadrant label from continuous (x_norm, y_norm) coords -- used when
    a human reviewer corrected the dot position and we need to re-derive the cell.
    (x,y) in [0,1] -> (col,row) in 1..8 -> quadrant label.
    """
    try:
        from ocr.quadrant_extractor import row_col_to_label
        col = int(float(x_norm) * 8) + 1
        row = int(float(y_norm) * 8) + 1
        col = max(1, min(8, col))
        row = max(1, min(8, row))
        return row_col_to_label(row, col) or ""
    except Exception:
        return ""


_DEFAULT_OUTPUT = r"D:\project_outputs_local"
_REVIEW_CSV     = "manual_review_results.csv"


# ---------------------------------------------------------------------------
# Column normalisation
# ---------------------------------------------------------------------------
# Downloads project uses location_section/township/range;
# coord_enricher expects section/township/range.

def _normalise_row(r: dict, review: dict | None = None) -> dict:
    """
    Map Downloads-project field names -> coord_enricher field names.
    Also synthesises fields the enricher needs but we derive here.

    review: row from manual_review_results.csv for this record (may be None).
    When the reviewer clicked a corrected dot position, we use that instead
    of the UNet-predicted x/y, and re-derive the quadrant label from the
    corrected position.
    """
    # Import canonical normalisers (available once D: project is on sys.path).
    # Fallbacks keep the function working even if the coord package isn't loaded yet.
    try:
        from coord.field_normalizer import (
            norm_section, norm_township, norm_range,
            norm_county, norm_quadrant as _norm_quad,
        )
    except ImportError:
        norm_section  = lambda v: _normalise_str_value(v)
        norm_township = lambda v: _normalise_str_value(v)
        norm_range    = lambda v: _normalise_str_value(v)
        norm_county   = lambda v, m=None: str(v or "").strip()
        _norm_quad    = lambda v: str(v or "").strip()

    out = dict(r)

    # Core PLSS address -- canonical normalisation (spacing, caps, T/R prefix,
    # float artefacts like "25.0", out-of-range values -> "")
    out["section"]  = norm_section( r.get("location_section",  ""))
    out["township"] = norm_township(r.get("location_township", ""))
    out["range"]    = norm_range(   r.get("location_range",    ""))

    # County -- strip junk phrases ("Not detected.", "None", "N/A" -> "")
    if out.get("county_name"):
        out["county_name"] = norm_county(out["county_name"])

    # Apply human-corrected dot position if the reviewer clicked a correction
    human_xn = (review or {}).get("human_x_norm", "").strip()
    human_yn = (review or {}).get("human_y_norm", "").strip()
    if human_xn and human_yn:
        out["dot_x_norm"] = human_xn
        out["dot_y_norm"] = human_yn
        # Re-derive quadrant from corrected position
        dot_nw = _xynorm_to_quad(human_xn, human_yn)
        if dot_nw:
            out["dot_nw"] = dot_nw
        out["quadrant_source"] = "human_correction"
    else:
        out.setdefault("quadrant_source", "unet")

    # Quadrant label -- derive from UNet (row, col) if still missing
    dot_nw = (out.get("dot_nw") or "").strip()
    if not dot_nw and r.get("dot_row") and r.get("dot_col"):
        dot_nw = _row_col_to_quad(r["dot_row"], r["dot_col"])
        if dot_nw:
            out["dot_nw"] = dot_nw

    # Normalise dot_nw to canonical hyphenated form (e.g. "ne_ne_sw" -> "NE-NE-SW")
    if out.get("dot_nw"):
        normed = _norm_quad(out["dot_nw"])
        if normed:
            out["dot_nw"] = normed

    # location_quadrant_pdf: we have no independent OCR quadrant from the PDF text
    # in the current pipeline (only UNet dot position).  Leave blank so the
    # enricher's _quadrant_meta() correctly reports single_source rather than
    # falsely claiming both sources agree.
    out["location_quadrant_pdf"] = ""
    out["location_quadrant_db"]  = ""   # no independent OCR quad; enricher uses dot_nw directly

    # dot_found -- always true here because we only pass confirmed records
    out["dot_found"] = "true"

    # decade
    try:
        yr = int(r.get("year", 0))
        out["decade"] = str((yr // 10) * 10)
    except (ValueError, TypeError):
        out["decade"] = ""

    if not out.get("model_tier"):
        out["model_tier"] = "early"

    out.setdefault("latlong_found",      "")
    out.setdefault("latlong_method",     "")
    out.setdefault("resolved_lat",       "")
    out.setdefault("resolved_lon",       "")
    out.setdefault("resolution_source",  "")
    out.setdefault("quadrant_source",    "unet")

    return out


# ---------------------------------------------------------------------------
# Review decisions loader
# ---------------------------------------------------------------------------

def _load_review_decisions(output_root: Path) -> dict[str, dict]:
    """
    Load manual_review_results.csv and return a dict keyed by pdf_stem.
    Only 'confirmed' rows are returned -- skipped/rejected are excluded.
    """
    review_csv = output_root / _REVIEW_CSV
    decisions: dict[str, dict] = {}
    if not review_csv.exists():
        return decisions
    with review_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("decision") == "confirmed":
                decisions[row["pdf_stem"]] = row
    return decisions


# ---------------------------------------------------------------------------
# Dry-run: test RDS connectivity
# ---------------------------------------------------------------------------

def _test_connection() -> bool:
    host     = os.environ.get("RDS_HOST", "")
    port     = int(os.environ.get("RDS_PORT", 5432))
    dbname   = os.environ.get("RDS_DBNAME", "")
    user     = os.environ.get("RDS_USER", "")
    password = os.environ.get("RDS_PASSWORD", "")

    missing = [k for k, v in [("RDS_HOST", host), ("RDS_DBNAME", dbname),
                                ("RDS_USER", user), ("RDS_PASSWORD", password)] if not v]
    if missing:
        print(f"\nERROR: Missing RDS credentials: {', '.join(missing)}")
        print("Add to your .env file:")
        print("  RDS_HOST=your-endpoint.rds.amazonaws.com")
        print("  RDS_PORT=5432")
        print("  RDS_DBNAME=Oklahomaplss")
        print("  RDS_USER=LookUpMaster")
        print("  RDS_PASSWORD=your-password")
        return False

    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2 not installed.  Run:  pip install psycopg2-binary")
        return False

    print(f"Connecting to {host}:{port}/{dbname} as {user}...")
    try:
        conn = psycopg2.connect(
            host=host, port=port, dbname=dbname,
            user=user, password=password,
            sslmode="require", connect_timeout=15,
        )
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM plss_grid LIMIT 1")
        count = cur.fetchone()[0]
        conn.close()
        print(f"  Connected OK - plss_grid has {count:,} rows")
        return True
    except Exception as e:
        print(f"  Connection FAILED: {e}")
        return False


# ---------------------------------------------------------------------------
# Main enrichment
# ---------------------------------------------------------------------------

def run(output_root: Path, year_filter: int | None, dry_run: bool,
        all_dot_done: bool = False,
        include_centroid: bool = False) -> None:
    """
    all_dot_done=True    : enrich every dot_done record regardless of review status.
    include_centroid=True: also add PLSS-complete no-dot records as centroid
                           candidates (Pass 5 section-centre approximation).
                           Requires all_dot_done=True.
    """
    if dry_run:
        _test_connection()
        return

    if not _test_connection():
        sys.exit(1)

    # Import after sys.path is patched
    try:
        from coord.coord_enricher import enrich_with_coordinates
    except ImportError as e:
        print(f"\nERROR: Cannot import coord module from {_D_PROJECT}: {e}")
        print("Make sure D:\\project_modular\\project exists and contains coord/")
        sys.exit(1)

    status_csv = output_root / "processing_status.csv"
    if not status_csv.exists():
        print(f"ERROR: {status_csv} not found.")
        sys.exit(1)

    # Load review decisions (used for human dot corrections even in all_dot_done mode)
    review_decisions = _load_review_decisions(output_root)

    # Read processing status
    with status_csv.open(newline="", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    if year_filter:
        all_rows = [r for r in all_rows if r.get("year") == str(year_filter)]
        print(f"Filtered to year {year_filter}: {len(all_rows)} records")

    # -- Record selection -------------------------------------------------------
    if all_dot_done:
        # All records that have a detected dot -- ignore review gate
        all_rows = [r for r in all_rows if r.get("dot_status") == "done"]
        n_with_review = sum(1 for r in all_rows
                            if r["pdf_stem"] in review_decisions)
        print(f"All dot_done records         : {len(all_rows):,}")
        print(f"  of which reviewed/confirmed: {n_with_review:,}  "
              f"(human corrections applied where available)")
        print(f"  unreviewed (raw UNet only) : {len(all_rows) - n_with_review:,}")
    elif review_decisions:
        confirmed_stems = set(review_decisions.keys())
        all_rows = [r for r in all_rows if r["pdf_stem"] in confirmed_stems]
        print(f"Review decisions loaded      : {len(review_decisions):,} confirmed")
        print(f"After review filter          : {len(all_rows):,} records to enrich")
    else:
        all_rows = [r for r in all_rows if r.get("dot_status") == "done"]
        print(f"No review file found -- all dot_done: {len(all_rows):,}")

    if not all_rows:
        print("No records to process.")
        return

    # -- Centroid candidates (Pass 5) -----------------------------------------
    # Records where dot detection returned nothing OR grid was unavailable,
    # but all three PLSS fields (section, township, range) are present.
    # We include these as centroid_candidate=true so Pass 5 can approximate
    # their position as the section centre (±½ mile accuracy).
    n_centroid_candidates = 0
    if include_centroid and all_dot_done:
        with status_csv.open(newline="", encoding="utf-8") as f:
            raw_status_rows = list(csv.DictReader(f))
        if year_filter:
            raw_status_rows = [r for r in raw_status_rows
                                if r.get("year") == str(year_filter)]

        done_stems = {r["pdf_stem"] for r in all_rows}

        def _has_full_plss(r: dict) -> bool:
            return bool(
                r.get("location_section",  "").strip() and
                r.get("location_township", "").strip() and
                r.get("location_range",    "").strip()
            )

        centroid_rows = []
        for r in raw_status_rows:
            if r.get("pdf_stem") in done_stems:
                continue   # already in dot_done batch
            # Include dot_skipped records (no grid image) with full PLSS
            if r.get("dot_status") == "skipped" and _has_full_plss(r):
                centroid_rows.append(r)
            # Include dot_done records where dot position is empty (confidence=0)
            elif (r.get("dot_status") == "done"
                  and not r.get("dot_row", "").strip()
                  and not r.get("dot_col", "").strip()
                  and _has_full_plss(r)):
                centroid_rows.append(r)

        if centroid_rows:
            n_centroid_candidates = len(centroid_rows)
            # Normalise centroid rows, mark them as centroid_candidate
            centroid_norm = []
            for r in centroid_rows:
                nr = _normalise_row(r, review_decisions.get(r["pdf_stem"]))
                nr["dot_found"] = "false"           # no dot — skip Passes 1-4
                nr["centroid_candidate"] = "true"   # flag for Pass 5
                centroid_norm.append(nr)
            all_rows.extend(centroid_rows)           # for CSV writing
            print(f"  Centroid candidates added: {n_centroid_candidates:,} "
                  f"(PLSS-complete, no dot position)")

    # Normalise columns, applying human-corrected dot positions where available
    norm_rows = [_normalise_row(r, review_decisions.get(r["pdf_stem"])) for r in all_rows]

    # Merge centroid normalised rows if any (they replace the ones just built above)
    if include_centroid and all_dot_done and n_centroid_candidates > 0:
        centroid_stems = {r["pdf_stem"] for r in centroid_rows}
        norm_rows = [nr for nr in norm_rows if nr["pdf_stem"] not in centroid_stems]
        norm_rows.extend(centroid_norm)
    human_corrections = sum(
        1 for r in norm_rows if r.get("quadrant_source") == "human_correction"
    )
    if human_corrections:
        print(f"  Human dot corrections applied: {human_corrections}")

    # Write a temp normalised CSV for the enricher
    # Build fieldnames as union of ALL row keys (preserves any row-specific fields
    # that only appear on some records due to optional stages).
    # Use output_root (D: drive) for the temp dir -- avoids C: space issues.
    tmp_dir = output_root / "_coord_enrich_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_csv = tmp_dir / "normalised_status.csv"
    if norm_rows:
        seen: dict[str, None] = {}
        for row in norm_rows:
            seen.update(dict.fromkeys(row.keys()))
        fieldnames = list(seen)
        with tmp_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(norm_rows)
    print(f"Normalised CSV written: {tmp_csv}")

    # Output CSV
    output_csv = output_root / "dot_coordinates.csv"

    print(f"\nRunning coordinate enrichment -> {output_csv}")
    print("(This queries the PLSS RDS database -- may take a few minutes)\n")

    resolved = enrich_with_coordinates(
        success_csv             = tmp_csv,
        output_csv              = output_csv,
        county_constraints_dir  = output_root,   # looks for county_constraints.json here
        status_csv              = status_csv,
    )

    # Clean up temp
    tmp_csv.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass  # dir not empty (other runs may have added files)

    # Summary
    print(f"\n{'='*60}")
    print(f"  Resolved: {resolved} records with lat/lon")
    print(f"  Output  : {output_csv}")
    failures = output_root / "coord_resolution_failures.csv"
    if failures.exists():
        with failures.open(encoding="utf-8") as f:
            nf = sum(1 for _ in csv.DictReader(f))
        print(f"  Failures: {nf} -> {failures}")
    print(f"  Log     : {output_root / 'coord_resolution_log.csv'}")
    print(f"{'='*60}\n")

    # Quick sample
    if output_csv.exists():
        rows = list(csv.DictReader(output_csv.open(encoding="utf-8")))
        resolved_rows = [r for r in rows if r.get("resolved_lat")]
        if resolved_rows:
            r = resolved_rows[0]
            print("Sample resolved record:")
            print(f"  {r['pdf_stem']}")
            print(f"  Sec={r.get('section')}  Twp={r.get('township')}  Rng={r.get('range')}")
            print(f"  dot_nw={r.get('dot_nw')}  ({r.get('dot_x_norm')}, {r.get('dot_y_norm')})")
            print(f"  -> lat={r.get('resolved_lat')}  lon={r.get('resolved_lon')}")
            print(f"     source={r.get('resolution_source')}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Resolve dot positions to lat/lon via PLSS RDS database"
    )
    ap.add_argument("--output",   default=_DEFAULT_OUTPUT,
                    help="Pipeline output root (default: D:\\project_outputs_local)")
    ap.add_argument("--year",     type=int, default=None,
                    help="Process a single year only (e.g. 1913)")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Test RDS connection only, do not process records")
    ap.add_argument("--all-dot-done", action="store_true",
                    help="Enrich ALL dot_done records, bypassing the manual-review "
                         "gate.  Human dot-position corrections are still applied "
                         "where available.  Use this to achieve full map coverage.")
    ap.add_argument("--include-centroid", action="store_true",
                    help="Also add PLSS-complete no-dot records as Pass-5 centroid "
                         "candidates (approximate section-centre lat/lon, ±½ mile). "
                         "Requires --all-dot-done.  Adds up to ~1,225 approximate "
                         "coordinates for records where dot detection found nothing.")
    args = ap.parse_args()

    print("\n-- PLSS Coordinate Enrichment ------------------------------")
    print(f"  Output root      : {args.output}")
    print(f"  All dot_done     : {args.all_dot_done}")
    print(f"  Include centroid : {args.include_centroid}")
    print(f"  Coord module     : {_D_PROJECT / 'coord'}")
    print(f"  .env             : {_ENV_PATH}")
    print("------------------------------------------------------------\n")

    run(Path(args.output),
        year_filter      = args.year,
        dry_run          = args.dry_run,
        all_dot_done     = args.all_dot_done,
        include_centroid = args.include_centroid)


if __name__ == "__main__":
    main()

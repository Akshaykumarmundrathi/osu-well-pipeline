"""
Oklahoma Well Records -- Extraction Pipeline

Usage:
    python main.py --scan --source D:\\ --output D:\\project_outputs
    python main.py --output D:\\project_outputs          # resume
    python main.py --flat ..\\pdfs --output D:\\project_outputs_test
    python main.py --stage grid --output D:\\project_outputs
    python main.py --pdf ..\\pdfs\\file.pdf --output D:\\project_outputs_test
    python main.py --status --output D:\\project_outputs
    python main.py --limit 10 --flat ..\\pdfs --output D:\\project_outputs_test
"""

import argparse
import atexit
import csv
import json
import multiprocessing as mp
import os
import sys
import time
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ---------------------------------------------------------------------------
# Load .env from project root for local dev.
# In Docker/Batch, env vars are already set by run_batch_job.py from
# Secrets Manager — this block is a safe no-op when the file is absent.
# ---------------------------------------------------------------------------
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())  # setdefault: never overwrite real env

from config import (
    ALL_STAGES, DATASET_INDEX_CSV, FAILED_RECORDS_CSV,
    LATLONG_MIN_COLLECTION_NUM,
    OUTPUT_ROOT, PROCESSING_STATUS_CSV,
    RESOLUTION_MULTIPLIER, SOURCE_ROOT,
    STAGE_COUNTY, STAGE_DOT, STAGE_GRID, STAGE_LATLONG, STAGE_LOCATION,
)
from pdf.pdf_manager import PDFDocumentManager
from scan_dataset import DatasetRecord, OutputPathBuilder, load_index, scan_flat_folder
from utils.insights import InsightsCollector
from utils.logging_utils import get_logger, get_pdf_logger
from utils.processing_status import DONE, FAILED, SKIPPED, ProcessingStatus
from utils.zip_reader import get_pdf_bytes

log = get_logger(__name__)

# -- Output CSV column lists ---------------------------------------------------

_SUMMARY_FIELDS = [
    "pdf_path", "pdf_stem", "collection", "year", "month",
    "zip_path", "model_tier", "decade",
    "latlong_found", "lat", "lon", "latlong_confidence", "latlong_page",
    "latlong_method", "latlong_form_type",
    "header_county", "header_section", "header_township", "header_range",
    "header_quad_raw", "header_quad_db", "header_feet",
    "well_name", "well_type",
    "grid_found", "grid_page", "grid_method", "grid_confidence", "grid_image_path",
    "location_found", "section", "township", "range", "location_confidence",
    "location_quadrant_pdf", "location_quadrant_db",
    "location_quadrant_row", "location_quadrant_col", "location_quadrant_confidence",
    "county_found", "county_name", "county_score", "county_confidence",
    "dot_found", "dot_row", "dot_col", "dot_nw", "dot_confidence",
    "dot_x_norm", "dot_y_norm",
    "final_status",      # 'success' | 'review' | 'failed'
    "needs_review",      # True if any field below review threshold
    "review_reasons",    # semicolon-joined list of weak signals
    "latlong_status", "grid_status", "location_status", "county_status", "dot_status",
    "latlong_error_type", "grid_error_type",
    "location_error_type", "county_error_type", "dot_error_type",
    # End-to-end coordinate derivation audit
    "coord_derivation", "coord_latlong_source",
    "coord_section_source", "coord_township_source", "coord_range_source",
    "coord_county_used", "coord_dot_source",
]

_DOT_FIELDS = [
    "pdf_path", "pdf_stem", "collection", "year", "month",
    "well_name",
    "dot_row", "dot_col", "dot_nw", "dot_confidence",
    "section", "township", "range",
    "county_name",
    "grid_image_path",
]

_LATLONG_FIELDS = [
    "pdf_path", "pdf_stem", "collection", "year", "month",
    "well_name", "well_type",
    "lat", "lon", "latlong_confidence", "latlong_page",
    "county_name", "county_score",
]


# -- Clean console output helpers ----------------------------------------------

_STAGE_LABEL = {
    STAGE_LATLONG:  "Lat / Lon",
    STAGE_GRID:     "Grid",
    STAGE_LOCATION: "Location",
    STAGE_COUNTY:   "County",
    STAGE_DOT:      "Dot",
}
_COL = 14   # label column width


def _p(msg: str = ""):
    """Flushing print — guarantees the line lands in real time."""
    print(msg, flush=True)


def _banner(msg: str):
    """Top/bottom '=' bordered block for major pipeline boundaries."""
    _p()
    _p("=" * 68)
    _p(f"  {msg}")
    _p("=" * 68)


def _section_header(collection: str, year: str, month: str, count: int):
    """Light divider introducing a new (collection / year / month) group."""
    label = f"{collection or 'cli'} / {year or '-'} / {month or '-'}"
    _p(f"\n  {'─'*60}")
    _p(f"  {label}   [{count:,} records]")
    _p(f"  {'─'*60}")


def _stage_line(label: str, text: str):
    """Print a complete stage line: '  {label}{text}'."""
    _p(f"  {label:<{_COL}}{text}")


def _stage_start(label: str):
    """Print the stage label and leave the cursor mid-line (no newline)."""
    print(f"  {label:<{_COL}}", end="", flush=True)


def _record_header(num: int, total: int, well_name: str,
                   collection: str, year: str, month: str, pages: int):
    """Two-line record intro: '[i/total]  WELL' + 'col | year | month  (Np)'."""
    _p()
    _p(f"  [{num:>7,} / {total:,}]  {well_name}")
    _p(f"  {'':>{_COL}}{collection or ''} | {year or ''} | {month or ''}  "
       f"({pages} page{'s' if pages != 1 else ''})")


def _format_stage_result(stage: str, r: dict, elapsed: float) -> str:
    """Compact one-line summary of a stage's result dict, suffixed with elapsed."""
    sec = f"  ({elapsed:.0f}s)"

    if r.get("error") and not r.get("detected"):
        err = str(r["error"])
        # Shorten verbose API errors to the key part
        if "503" in err or "UNAVAILABLE" in err.upper():
            return f"API error (503 connection failed){sec}"
        if "socket" in err.lower() or "handshaker" in err.lower():
            return f"API error (connection dropped){sec}"
        return f"error: {err[:70]}{sec}"

    if stage == STAGE_LATLONG:
        if r.get("detected"):
            wt  = r.get("well_type") or "type unknown"
            return (f"FOUND   lat={r.get('lat')}  lon={r.get('lon')}"
                    f"   ({r.get('confidence')}% confidence)   [{wt}]{sec}")
        return f"not found{sec}"

    if stage == STAGE_GRID:
        if r.get("detected"):
            return (f"found   page {r.get('page')}   "
                    f"{r.get('method','').replace('extract_grid_region_','')}"
                    f"   ({r.get('confidence')}%){sec}")
        return f"not detected{sec}"

    if stage == STAGE_LOCATION:
        if r.get("detected"):
            # '?' = field wasn't extracted (regex miss / OCR gap).
            sec_ = r.get("section") or "?"
            twp  = r.get("township") or "?"
            rng  = r.get("range") or "?"
            conf = r.get("confidence", 0)
            return f"sec={sec_}  twp={twp}  rng={rng}   ({conf}%){sec}"
        return f"not found{sec}"

    if stage == STAGE_COUNTY:
        if r.get("detected"):
            return (f"{r.get('name','')}   "
                    f"({r.get('fuzzy_score', r.get('confidence', 0))}% match){sec}")
        return f"not matched{sec}"

    if stage == STAGE_DOT:
        if r.get("detected"):
            return (f"row={r.get('row')}  col={r.get('col')}  "
                    f"nw={r.get('nw','')}   ({r.get('confidence')}%){sec}")
        return f"not detected{sec}"

    return f"done{sec}"


def _record_status_line(stages_run: dict, stages: tuple):
    """Print a one-line OK / PARTIAL / FAILED summary after a record.

    Latlong is *optional*: only ~1% of docs have decimal coords, so
    its absence is the normal case — never counted as PARTIAL/missing.
    """
    failed   = [s for s in stages if isinstance(stages_run.get(s), dict)
                and stages_run[s].get("error")
                and not stages_run[s].get("detected")]
    skipped  = [s for s in stages if stages_run.get(s) == SKIPPED]
    core     = [s for s in stages
                if s != STAGE_LATLONG and s not in skipped]
    missing  = [s for s in core
                if not (isinstance(stages_run.get(s), dict)
                        and stages_run[s].get("detected"))]

    if failed:
        tag = f"FAILED  ({', '.join(failed)})"
    elif not missing:
        tag = "OK"
    else:
        tag = f"PARTIAL  (not found: {', '.join(missing)})"

    _p(f"  {'':>{_COL}}{tag}")


def _totals_line(done: int, failed: int, skipped: int, total: int):
    """Running progress: 'Progress: N / T (P%)   done=.. failed=.. skipped=..'."""
    pct = int(100 * (done + failed + skipped) / total) if total else 0
    _p(f"\n  Progress: {done + failed + skipped:,} / {total:,} ({pct}%)"
       f"   done={done:,}  failed={failed:,}  skipped={skipped:,}")


# -- Utility functions ---------------------------------------------------------

def _now() -> str:
    """UTC timestamp string in 'YYYY-MM-DDTHH:MM:SS' format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _well_name_from_stem(pdf_stem: str) -> str:
    """
    Stem format: '{api_or_prefix}_{WELL NAME}_{record_id}'.
    Returns the middle slice, falling back to the full stem if the
    underscore layout is unexpected.
    """
    first = pdf_stem.find("_")
    last  = pdf_stem.rfind("_")
    if first != -1 and last != first:
        return pdf_stem[first + 1: last]
    return pdf_stem


# -- PDF source resolution -----------------------------------------------------

def _make_manager(record: DatasetRecord) -> PDFDocumentManager:
    """
    Build a PDFDocumentManager for the record's source.

    Routes the lookup based on `record.zip_path`:
      - 's3://bucket/key' -> stream the ZIP from S3, extract the PDF
      - any other non-empty path -> local ZIP file
      - empty -> read the standalone PDF at `record.pdf_path`
    """
    zp = record.zip_path or ""
    if zp.startswith("s3://"):
        from utils.s3_reader import get_pdf_bytes_s3
        pdf_bytes = get_pdf_bytes_s3(zp, record.internal_path)
        return PDFDocumentManager(pdf_bytes=pdf_bytes,
                                  resolution_multiplier=RESOLUTION_MULTIPLIER)
    if zp:
        pdf_bytes = get_pdf_bytes(zp, record.internal_path)
        return PDFDocumentManager(pdf_bytes=pdf_bytes,
                                  resolution_multiplier=RESOLUTION_MULTIPLIER)
    # Flat S3 layout: no ZIP wrapper, direct s3:// PDF URI
    pp = record.pdf_path or ""
    if pp.startswith("s3://"):
        from utils.s3_reader import get_pdf_bytes_s3_flat
        pdf_bytes = get_pdf_bytes_s3_flat(pp)
        return PDFDocumentManager(pdf_bytes=pdf_bytes,
                                  resolution_multiplier=RESOLUTION_MULTIPLIER)
    return PDFDocumentManager(record.pdf_path,
                              resolution_multiplier=RESOLUTION_MULTIPLIER)


# -- CSV writers ---------------------------------------------------------------

def write_metadata(record: DatasetRecord, results: dict, paths: OutputPathBuilder):
    """Write per-record metadata.json with source info + raw stage outputs."""
    meta_path = paths.metadata_path(record)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": {
            "pdf_stem":        record.pdf_stem,
            "pdf_path":        record.pdf_path,
            "collection":      record.collection,
            "year":            record.year,
            "month":           record.month,
            "file_size_bytes": record.file_size_bytes,
            "scan_timestamp":  record.scan_timestamp,
        },
        "processed_at": _now(),
        "stages":       results,
    }
    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return meta_path


def _append_failed(record: DatasetRecord, stage: str, error: str):
    """Append one row to manual_review/failed_records.csv (with header if new)."""
    FAILED_RECORDS_CSV.parent.mkdir(parents=True, exist_ok=True)
    exists = FAILED_RECORDS_CSV.exists()
    with FAILED_RECORDS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["pdf_stem", "pdf_path", "stage", "error", "timestamp"])
        w.writerow([record.pdf_stem, record.pdf_path, stage, error, _now()])


def _int_or_zero(v) -> int:
    """Best-effort int conversion for confidence/score fields stored as strings."""
    try:
        return int(float(v)) if v not in ("", None) else 0
    except (TypeError, ValueError):
        return 0


def _classify_record(row: dict) -> tuple[str, list[str]]:
    """
    Decide each record's terminal state. Returns (final_status, review_reasons).

    final_status:
      'success' -- county detected with high score AND (lat/lon OR grid+location
                   high confidence); no field below review threshold
      'review'  -- success criteria met but at least one field is weak
                   (low confidence / low score) and warrants a human check
      'failed'  -- success criteria NOT met (county missing, or neither
                   coords nor full grid+location)
    """
    from config import (
        COUNTY_REVIEW_BELOW, DOT_REVIEW_BELOW, GRID_REVIEW_BELOW,
        LOCATION_REVIEW_BELOW,
    )

    ll_found     = bool(row.get("latlong_lat")) and bool(row.get("latlong_lon"))
    grid_done    = row.get("grid_status")     == DONE
    loc_done     = row.get("location_status") == DONE
    county_done  = row.get("county_status")   == DONE
    dot_done     = row.get("dot_status")      == DONE

    county_score = _int_or_zero(row.get("county_score"))
    grid_conf    = _int_or_zero(row.get("grid_confidence"))
    loc_conf     = _int_or_zero(row.get("location_confidence"))
    ll_conf      = _int_or_zero(row.get("latlong_confidence"))
    dot_conf     = _int_or_zero(row.get("dot_confidence"))

    # Success requires county AND (coords OR (grid AND location)).
    has_geo = ll_found or (grid_done and loc_done)
    if not (county_done and has_geo):
        return "failed", []

    reasons: list[str] = []
    if county_score < COUNTY_REVIEW_BELOW:
        reasons.append(f"county_score={county_score}<{COUNTY_REVIEW_BELOW}")
    if grid_done and grid_conf < GRID_REVIEW_BELOW:
        reasons.append(f"grid_conf={grid_conf}<{GRID_REVIEW_BELOW}")
    if loc_done and loc_conf < LOCATION_REVIEW_BELOW:
        reasons.append(f"location_conf={loc_conf}<{LOCATION_REVIEW_BELOW}")
    if ll_found and ll_conf and ll_conf < 80:
        reasons.append(f"latlong_conf={ll_conf}<80")
    if dot_done and dot_conf < DOT_REVIEW_BELOW:
        reasons.append(f"dot_conf={dot_conf}<{DOT_REVIEW_BELOW}")

    return ("review" if reasons else "success"), reasons


def _row_to_summary_dict(row: dict, final_status: str,
                        reasons: list[str]) -> dict:
    """Flatten a status row to the summary CSV schema."""
    stem = row.get("pdf_stem", "")
    ll   = bool(row.get("latlong_lat")) and bool(row.get("latlong_lon"))
    return {
        "pdf_path":            row.get("pdf_path", ""),
        "pdf_stem":            stem,
        "collection":          row.get("collection", ""),
        "year":                row.get("year", ""),
        "month":               row.get("month", ""),
        "zip_path":            row.get("zip_path", ""),
        "model_tier":          row.get("model_tier", ""),
        "decade":              row.get("decade", ""),
        "latlong_found":       ll,
        "lat":                 row.get("latlong_lat", ""),
        "lon":                 row.get("latlong_lon", ""),
        "latlong_confidence":  row.get("latlong_confidence", ""),
        "latlong_page":        row.get("latlong_page", ""),
        "latlong_method":      row.get("latlong_method", ""),
        "latlong_form_type":   row.get("latlong_form_type", ""),
        "header_county":       row.get("header_county", ""),
        "header_section":      row.get("header_section", ""),
        "header_township":     row.get("header_township", ""),
        "header_range":        row.get("header_range", ""),
        "header_quad_raw":     row.get("header_quad_raw", ""),
        "header_quad_db":      row.get("header_quad_db", ""),
        "header_feet":         row.get("header_feet", ""),
        "well_name":           _well_name_from_stem(stem),
        "well_type":           row.get("latlong_well_type", ""),
        "grid_found":          row.get("grid_status")     == DONE,
        "grid_page":           row.get("grid_page", ""),
        "grid_method":         row.get("grid_method", ""),
        "grid_confidence":     row.get("grid_confidence", ""),
        "location_found":               row.get("location_status") == DONE,
        "section":                      row.get("location_section", ""),
        "township":                     row.get("location_township", ""),
        "range":                        row.get("location_range", ""),
        "location_confidence":          row.get("location_confidence", ""),
        "location_quadrant_pdf":        row.get("location_quadrant_pdf", ""),
        "location_quadrant_db":         row.get("location_quadrant_db", ""),
        "location_quadrant_row":        row.get("location_quadrant_row", ""),
        "location_quadrant_col":        row.get("location_quadrant_col", ""),
        "location_quadrant_confidence": row.get("location_quadrant_confidence", ""),
        "county_found":        row.get("county_status")   == DONE,
        "county_name":         row.get("county_name", ""),
        "county_score":        row.get("county_score", ""),
        "county_confidence":   row.get("county_confidence", ""),
        "grid_image_path":     row.get("grid_image_path", ""),
        "dot_found":           row.get("dot_status") == DONE,
        "dot_row":             row.get("dot_row", ""),
        "dot_col":             row.get("dot_col", ""),
        "dot_nw":              row.get("dot_nw", ""),
        "dot_confidence":      row.get("dot_confidence", ""),
        "dot_x_norm":          row.get("dot_x_norm", ""),
        "dot_y_norm":          row.get("dot_y_norm", ""),
        "final_status":        final_status,
        "needs_review":        final_status == "review",
        "review_reasons":      "; ".join(reasons),
        "latlong_status":      row.get("latlong_status", ""),
        "grid_status":         row.get("grid_status", ""),
        "location_status":     row.get("location_status", ""),
        "county_status":       row.get("county_status", ""),
        "dot_status":          row.get("dot_status", ""),
        "latlong_error_type":  row.get("latlong_error_type", ""),
        "grid_error_type":     row.get("grid_error_type", ""),
        "location_error_type": row.get("location_error_type", ""),
        "county_error_type":     row.get("county_error_type", ""),
        "dot_error_type":        row.get("dot_error_type", ""),
        # Coordinate derivation audit
        "coord_derivation":      row.get("coord_derivation", ""),
        "coord_latlong_source":  row.get("coord_latlong_source", ""),
        "coord_section_source":  row.get("coord_section_source", ""),
        "coord_township_source": row.get("coord_township_source", ""),
        "coord_range_source":    row.get("coord_range_source", ""),
        "coord_county_used":     row.get("coord_county_used", ""),
        "coord_dot_source":      row.get("coord_dot_source", ""),
    }


def write_summary_csvs(status: ProcessingStatus, output_root: Path):
    """
    Write the two terminal CSVs. Every record lands in EXACTLY ONE of them:
      - <output>/success.csv  (final_status in {'success', 'review'})
      - <output>/manual_review/failed.csv  (final_status == 'failed')

    'review' rows live in success.csv with `needs_review=True` and a
    `review_reasons` column listing what triggered the flag — so they
    aren't duplicated to the failed file.
    """
    from config import MANUAL_REVIEW_DIR

    success_path = output_root / "success.csv"
    failed_path  = MANUAL_REVIEW_DIR / "failed.csv"
    failed_path.parent.mkdir(parents=True, exist_ok=True)

    n_success = n_review = n_failed = 0
    with success_path.open("w", newline="", encoding="utf-8") as fs, \
         failed_path.open("w", newline="", encoding="utf-8") as ff:
        ws = csv.DictWriter(fs, fieldnames=_SUMMARY_FIELDS, extrasaction="ignore")
        wf = csv.DictWriter(ff, fieldnames=_SUMMARY_FIELDS, extrasaction="ignore")
        ws.writeheader()
        wf.writeheader()

        for row in status._rows.values():
            final_status, reasons = _classify_record(row)
            out = _row_to_summary_dict(row, final_status, reasons)
            if final_status == "failed":
                wf.writerow(out); n_failed += 1
            else:
                ws.writerow(out)
                if final_status == "review":
                    n_review += 1
                else:
                    n_success += 1

    _p(f"  success.csv  ({n_success:,} clean + {n_review:,} for review)  -> {success_path}")
    _p(f"  failed.csv   ({n_failed:,} records)                            -> {failed_path}")


def write_dot_locations_csv(status: ProcessingStatus, output_path: Path):
    """Write a CSV with every record where the dot stage succeeded."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_DOT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in status._rows.values():
            if row.get("dot_status") != DONE:
                continue
            stem = row.get("pdf_stem", "")
            writer.writerow({
                "pdf_path":       row.get("pdf_path", ""),
                "pdf_stem":       stem,
                "collection":     row.get("collection", ""),
                "year":           row.get("year", ""),
                "month":          row.get("month", ""),
                "well_name":      _well_name_from_stem(stem),
                "dot_row":        row.get("dot_row", ""),
                "dot_col":        row.get("dot_col", ""),
                "dot_nw":         row.get("dot_nw", ""),
                "dot_confidence": row.get("dot_confidence", ""),
                "section":        row.get("location_section", ""),
                "township":       row.get("location_township", ""),
                "range":          row.get("location_range", ""),
                "county_name":    row.get("county_name", ""),
                "grid_image_path": row.get("grid_image_path", ""),
            })
            count += 1
    _p(f"  Dot CSV written  ({count:,} records with dot detection)  ->  {output_path}")


def write_latlong_csv(status: ProcessingStatus, output_path: Path):
    """Write a separate CSV with only the records that had decimal lat/lon."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_LATLONG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in status._rows.values():
            if not (row.get("latlong_lat") and row.get("latlong_lon")):
                continue
            stem = row.get("pdf_stem", "")
            writer.writerow({
                "pdf_path":           row.get("pdf_path", ""),
                "pdf_stem":           stem,
                "collection":         row.get("collection", ""),
                "year":               row.get("year", ""),
                "month":              row.get("month", ""),
                "well_name":          _well_name_from_stem(stem),
                "well_type":          row.get("latlong_well_type", ""),
                "lat":                row.get("latlong_lat", ""),
                "lon":                row.get("latlong_lon", ""),
                "latlong_confidence": row.get("latlong_confidence", ""),
                "latlong_page":       row.get("latlong_page", ""),
                "county_name":        row.get("county_name", ""),
                "county_score":       row.get("county_score", ""),
            })
            count += 1
    _p(f"  Lat/Lon CSV written  ({count:,} records with coordinates)  ->  {output_path}")


# -- Per-record processing (parallel-safe worker) ------------------------------

def _record_status_text(stages_run: dict, stages: tuple) -> str:
    """Return the OK / PARTIAL / FAILED summary as a single string (no print)."""
    failed   = [s for s in stages if isinstance(stages_run.get(s), dict)
                and stages_run[s].get("error")
                and not stages_run[s].get("detected")]
    skipped  = [s for s in stages if stages_run.get(s) == SKIPPED]
    core     = [s for s in stages if s != STAGE_LATLONG and s not in skipped]
    missing  = [s for s in core
                if not (isinstance(stages_run.get(s), dict)
                        and stages_run[s].get("detected"))]
    if failed:
        tag = f"FAILED  ({', '.join(failed)})"
    elif not missing:
        tag = "OK"
    else:
        tag = f"PARTIAL  (not found: {', '.join(missing)})"
    return f"  {'':>{_COL}}{tag}"


def _process_record_worker(arg):
    """
    Multiprocessing-friendly worker. Runs the full stage pipeline for one
    record, returning  (pdf_stem, stage_results_dict, console_lines).

    All side effects that are unique-per-record (per-PDF log file, crop
    images, metadata.json) happen inside the worker — no contention.
    Shared state (status CSV, failed_records.csv, console) is left to the
    parent which applies the returned `results` via _apply_results().

    Argument tuple is positional so it's cheap to pickle:
      (record, stages, output_root_str, resume, prior_row, record_num, total)
    """
    record, stages, output_root_str, resume, prior_row, record_num, total = arg
    paths   = OutputPathBuilder(Path(output_root_str))
    pdf_log = get_pdf_logger(record.pdf_stem, paths.log_path(record))
    pdf_log.debug("=== START %s ===", record.pdf_stem)

    well_name = _well_name_from_stem(record.pdf_stem)
    lines: list[str] = []

    # -- Open the PDF -----------------------------------------------------------
    try:
        manager = _make_manager(record)
        pages   = manager.page_count()
        pdf_log.debug("pages: %d", pages)
    except Exception as exc:
        pdf_log.error("Cannot open PDF: %s", exc)
        lines.append("")
        lines.append(f"  [{record_num:>7,} / {total:,}]  {well_name}")
        lines.append(f"  {'ERROR':<{_COL}}Cannot open PDF -- {exc}")
        return (
            record.pdf_stem,
            {s: {"detected": False, "error": f"open_failed: {exc}"} for s in stages},
            lines,
        )

    lines.append("")
    lines.append(f"  [{record_num:>7,} / {total:,}]  {well_name}")
    lines.append(f"  {'':>{_COL}}{record.collection or ''} | {record.year or ''} "
                 f"| {record.month or ''}  "
                 f"({pages} page{'s' if pages != 1 else ''})")

    results: dict = {}
    stage_dirs = {
        STAGE_LATLONG:  paths.grids_dir(record),
        STAGE_GRID:     paths.grids_dir(record),
        STAGE_LOCATION: paths.locations_dir(record),
        STAGE_COUNTY:   paths.counties_dir(record),
        STAGE_DOT:      paths.dots_dir(record),
    }

    for stage in stages:
        label = _STAGE_LABEL.get(stage, stage)

        # Resume: already done or skipped in a prior run.
        prior_status = prior_row.get(f"{stage}_status")
        if resume and prior_status == DONE:
            lines.append(f"  {label:<{_COL}}already done")
            results[stage] = {"detected": True, "_was_done": True}
            continue
        if resume and prior_status == SKIPPED:
            lines.append(f"  {label:<{_COL}}already skipped")
            results[stage] = SKIPPED
            continue

        # Latlong collection gate (tier-aware).
        if stage == STAGE_LATLONG:
            from config import TIER_CONFIG, tier_for
            tier  = tier_for(record.collection_num)
            run_l = TIER_CONFIG.get(tier, {"run_latlong": False})["run_latlong"]
            if not run_l:
                lines.append(
                    f"  {label:<{_COL}}skipped  "
                    f"(tier '{tier}' has no lat/lon on form)"
                )
                results[stage] = SKIPPED
                continue

        # Skip grid + location when lat/lon was already found (this run or prior).
        if stage in (STAGE_GRID, STAGE_LOCATION):
            ll_r = results.get(STAGE_LATLONG, {})
            ll_found = (
                (isinstance(ll_r, dict) and ll_r.get("detected", False))
                or (bool(prior_row.get("latlong_lat"))
                    and bool(prior_row.get("latlong_lon")))
            )
            if ll_found:
                lines.append(f"  {label:<{_COL}}skipped  (lat/lon found in document)")
                results[stage] = SKIPPED
                continue

        # Skip dot detection when grid was not found (no image to run on).
        if stage == STAGE_DOT:
            grid_r = results.get(STAGE_GRID)
            grid_detected = (
                (isinstance(grid_r, dict) and grid_r.get("detected"))
                or (grid_r is None and prior_row.get("grid_status") == DONE)
            )
            if not grid_detected:
                lines.append(f"  {label:<{_COL}}skipped  (grid not detected)")
                results[stage] = SKIPPED
                continue

        t0 = time.monotonic()
        extra_kw: dict = {}
        if stage == STAGE_DOT:
            extra_kw["grid_dir"] = paths.grids_dir(record)
            extra_kw["output_root"] = paths.root
        try:
            r = _dispatch(stage, manager, stage_dirs[stage],
                          record.pdf_stem, pdf_log, record=record, **extra_kw)
        except Exception as exc:
            pdf_log.error("[%s] unhandled exception: %s", stage.upper(), exc,
                          exc_info=True)
            r = {"detected": False, "error": str(exc)}
        elapsed = time.monotonic() - t0
        pdf_log.debug("[%s] %.1fs detected=%s", stage.upper(), elapsed,
                      r.get("detected"))

        # Stamp elapsed so the parent's insights collector can aggregate it.
        if isinstance(r, dict):
            r["_elapsed"] = elapsed

        lines.append(f"  {label:<{_COL}}{_format_stage_result(stage, r, elapsed)}")
        results[stage] = r

    lines.append(_record_status_text(results, stages))

    # Write per-record metadata.json from real (non-skipped, non-was_done) results.
    real = {k: v for k, v in results.items()
            if isinstance(v, dict) and not v.get("_was_done")}
    if real:
        try:
            write_metadata(record, real, paths)
        except Exception as exc:
            pdf_log.error("metadata write failed: %s", exc)

    pdf_log.debug("=== END %s ===", record.pdf_stem)
    return record.pdf_stem, results, lines


def _apply_results(record: DatasetRecord, stages: tuple,
                   results: dict, status: ProcessingStatus):
    """
    Mirror the inline status mutations the original sequential worker did.
    Called from the parent process so the master ProcessingStatus and
    failed_records.csv are updated from a single writer.
    """
    for stage in stages:
        r = results.get(stage)
        if r == SKIPPED:
            status.mark_skipped(record.pdf_stem, stage)
            continue
        if isinstance(r, dict):
            if r.get("_was_done"):
                continue   # nothing to do; status already DONE from prior run
            if r.get("error") and not r.get("detected"):
                err = r["error"]
                status.mark_failed(record.pdf_stem, stage, err)
                _append_failed(record, stage, err)
            else:
                status.mark_done(record.pdf_stem, stage, r)


def run_one_record(
    record: DatasetRecord,
    stages: tuple,
    paths: OutputPathBuilder,
    status: ProcessingStatus,
    resume: bool = True,
    record_num: int = 0,
    total: int = 0,
) -> dict:
    """
    Sequential entry point. Calls the worker inline, prints its lines,
    applies its results to `status`. Kept for callers that already pass
    a status object (e.g. _retry_record).
    """
    prior = dict(status._rows.get(record.pdf_stem, {}))
    arg   = (record, stages, str(paths.root), resume, prior,
             record_num, total)
    _stem, results, lines = _process_record_worker(arg)
    for ln in lines:
        _p(ln)
    _apply_results(record, stages, results, status)
    return results


def _dispatch(stage: str, manager: PDFDocumentManager,
              out_dir: Path, pdf_stem: str, log,
              record: DatasetRecord | None = None,
              grid_dir: Path | None = None,
              output_root: Path | None = None,
              **kwargs) -> dict:
    """
    Route a stage name to its extractor entry point. Lazy-imports each
    sub-module so a stage that's never invoked never pays the import cost.

    Strategy selection is tier-aware: collections 9+ use the
    'Location:' keyword extractor by default, while early collections
    use the classic sec/twp/rge keyword pairing.
    """
    if stage == STAGE_LATLONG:
        from latlong.latlong_extractor import process_single_latlong
        return process_single_latlong(manager, pdf_stem, log)

    if stage == STAGE_GRID:
        from grid.scoring import process_single_grid
        return process_single_grid(manager, out_dir, pdf_stem, log)

    if stage == STAGE_LOCATION:
        from config import TIER_CONFIG, tier_for
        strategy = "str_keywords"
        if record is not None:
            strategy = TIER_CONFIG.get(
                tier_for(record.collection_num),
                {"location_strategy": "str_keywords"},
            )["location_strategy"]
        if strategy == "location_keyword":
            from location.location_keyword_extractor import (
                process_single_location_keyword,
            )
            return process_single_location_keyword(manager, out_dir,
                                                   pdf_stem, log)
        from location.location_extractor import process_single_location
        return process_single_location(manager, out_dir, pdf_stem, log)

    if stage == STAGE_COUNTY:
        from county.county_extractor import process_single_county
        return process_single_county(manager, out_dir, pdf_stem, log)

    if stage == STAGE_DOT:
        from dot.dot_extractor import process_single_dot
        from config import tier_for
        if grid_dir is None:
            raise ValueError("STAGE_DOT requires grid_dir kwarg")
        _tier = tier_for(getattr(record, "collection_num", None))
        return process_single_dot(
            grid_dir, out_dir, pdf_stem, log,
            tier=_tier,
            output_root=output_root,
        )

    raise ValueError(f"Unknown stage: {stage}")


# -- Retry helpers -------------------------------------------------------------

_RETRIED: set[str] = set()           # stems already retried this run


def _retry_one_stage(stage: str, error_type: str, manager, out_dir: Path,
                     pdf_stem: str, pdf_log):
    """
    Re-run a single failed stage with a strategy chosen from `error_type`.
    Returns the new result dict (or None when no strategy applies).

    Strategies:
      county / keyword_not_found  -> scan deeper pages
      county / no_match           -> wider crop, then full-page Pro fallback
      location / not_found        -> looser min_overlap pairing + per-keyword fallback
      grid / not_detected         -> relaxed size band, forward iteration
      latlong / *                 -> scan ALL pages
      *  / api_error              -> simple retry (transient)
    """
    from config import (
        COUNTY_RETRY_CROP_SCALE, LOCATION_MIN_OVERLAP_RETRY,
        MAX_COUNTY_PAGES_RETRY, MAX_LATLONG_PAGES_RETRY,
    )

    if stage == STAGE_COUNTY:
        from county.county_extractor import process_single_county
        if error_type == "keyword_not_found":
            return process_single_county(
                manager, out_dir, pdf_stem, pdf_log,
                max_pages=MAX_COUNTY_PAGES_RETRY,
            )
        if error_type in ("no_match", "invalid_crop", "exception", "unknown", ""):
            # Try wider crop first; if still no_match, go to full-page Pro.
            r = process_single_county(
                manager, out_dir, pdf_stem, pdf_log,
                crop_scale=COUNTY_RETRY_CROP_SCALE,
            )
            if r.get("detected"):
                return r
            return process_single_county(
                manager, out_dir, pdf_stem, pdf_log,
                full_page_gemini=True,
            )
        # api_error or other transient -> plain retry
        return process_single_county(manager, out_dir, pdf_stem, pdf_log)

    if stage == STAGE_LOCATION:
        from location.location_extractor import process_single_location
        return process_single_location(
            manager, out_dir, pdf_stem, pdf_log,
            min_overlap=LOCATION_MIN_OVERLAP_RETRY,
        )

    if stage == STAGE_GRID:
        from grid.scoring import process_single_grid
        return process_single_grid(
            manager, out_dir, pdf_stem, pdf_log, relaxed=True,
        )

    if stage == STAGE_LATLONG:
        from latlong.latlong_extractor import process_single_latlong
        # Temporarily override the page cap to scan every page.
        process_single_latlong._max_pages_override = MAX_LATLONG_PAGES_RETRY
        try:
            return process_single_latlong(manager, pdf_stem, pdf_log)
        finally:
            try:
                del process_single_latlong._max_pages_override
            except AttributeError:
                pass

    # STAGE_DOT: fully deterministic (no API calls) — nothing to retry.
    return None


def _retry_record(record: DatasetRecord, stages: tuple,
                  paths: OutputPathBuilder, status: ProcessingStatus,
                  num: int, total: int):
    """
    Re-run only the FAILED stages of a single record, using a strategy
    chosen from each stage's stored error_type. Prints one compact
    summary line per record (replaces the verbose retry block).
    """
    pdf_log = get_pdf_logger(record.pdf_stem, paths.log_path(record))
    well    = _well_name_from_stem(record.pdf_stem)

    try:
        manager = _make_manager(record)
    except Exception as exc:
        _p(f"  [Retry {num:>3}/{total}]  {well}  -- cannot open PDF: {exc}")
        return

    stage_dirs = {
        STAGE_LATLONG:  paths.grids_dir(record),
        STAGE_GRID:     paths.grids_dir(record),
        STAGE_LOCATION: paths.locations_dir(record),
        STAGE_COUNTY:   paths.counties_dir(record),
        STAGE_DOT:      paths.dots_dir(record),
    }

    parts: list[str] = []
    for stage in stages:
        if status.get_status(record.pdf_stem, stage) != FAILED:
            continue
        et = status.get_error_type(record.pdf_stem, stage) or "unknown"

        t0 = time.monotonic()
        try:
            r = _retry_one_stage(stage, et, manager,
                                 stage_dirs[stage], record.pdf_stem, pdf_log)
        except Exception as exc:
            pdf_log.error("[retry][%s] unhandled: %s", stage, exc, exc_info=True)
            r = {"detected": False, "error": str(exc)}
        elapsed = time.monotonic() - t0

        label = _STAGE_LABEL.get(stage, stage)
        if r and r.get("detected"):
            status.mark_done(record.pdf_stem, stage, r)
            parts.append(f"{label} {et}->OK ({elapsed:.0f}s)")
        else:
            new_err = (r.get("error") if r else "no_change") or "no_change"
            status.mark_failed(record.pdf_stem, stage, new_err)
            _append_failed(record, stage, f"retry({et}): {new_err}")
            parts.append(f"{label} {et}->{new_err[:20]} ({elapsed:.0f}s)")

    summary = " | ".join(parts) if parts else "no FAILED stages"
    _p(f"  [Retry {num:>3}/{total}]  {well}  -- {summary}")


def _retry_failed(
    records: list,
    stages: tuple,
    paths: OutputPathBuilder,
    status: ProcessingStatus,
    total: int,
    label: str = "",
):
    """
    Retry every record with a FAILED stage ONCE per run, using the
    failure-type-aware dispatcher. Skipped on second invocation for the
    same stem (so month-retry + year-retry don't double up).
    """
    stems   = [r.pdf_stem for r in records]
    failed  = status.failed_in(stems, stages)
    if not failed:
        return
    failed_set = set(failed)
    to_retry   = [
        r for r in records
        if r.pdf_stem in failed_set and r.pdf_stem not in _RETRIED
    ]
    if not to_retry:
        return
    _p(f"\n  Retrying {len(to_retry)} failed record(s)  [{label}]")
    for i, record in enumerate(to_retry, 1):
        _RETRIED.add(record.pdf_stem)
        _retry_record(record, stages, paths, status, i, len(to_retry))
    status.force_save()


# -- Pipeline runner -----------------------------------------------------------

def run_pipeline(args):
    """
    End-to-end pipeline. Loads/scans records, groups them by
    (collection, year, month), processes each group, retries failures at
    month and year boundaries, then writes the final summary CSVs.
    """
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)

    paths  = OutputPathBuilder(output_root)
    status = ProcessingStatus(output_root / "processing_status.csv")

    atexit.register(status.force_save)

    # Disk-backed Vision API cache (survives restarts and spot interruptions).
    from utils.api_cache import init_cache
    _api_cache = init_cache(output_root)
    _cache_stats_before = _api_cache.stats()

    # County → PLSS bounds cache: built once, reused across runs.
    # Improves direction-inference for partial PLSS records.
    _county_constraints_dir = output_root
    from coord.county_constraints import load as _load_cc, build_and_save as _build_cc
    if not (_county_constraints_dir / "county_constraints.json").exists():
        _p("  Building county constraints from RDS (one-time)...")
        try:
            import os
            _build_cc(
                _county_constraints_dir,
                host=os.environ.get("RDS_HOST", ""),
                port=int(os.environ.get("RDS_PORT", "5432")),
                dbname=os.environ.get("RDS_DBNAME",   ""),
                user=os.environ.get("RDS_USER",       ""),
                password=os.environ.get("RDS_PASSWORD", ""),
            )
            _p(f"  county_constraints.json saved -> {_county_constraints_dir}")
        except Exception as _cc_exc:
            _p(f"  county constraints build skipped: {_cc_exc}")

    # Graceful Docker shutdown: flush status before process is killed.
    import signal
    def _sigterm_handler(signum, frame):
        _p("\n  SIGTERM received — flushing status and exiting...")
        status.force_save()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sigterm_handler)

    _run_start = time.monotonic()

    # -- Source records --------------------------------------------------------
    if args.pdf:
        pdf = Path(args.pdf)
        records = [DatasetRecord(pdf_stem=pdf.stem, pdf_path=str(pdf),
                                 collection="cli", collection_safe="cli")]
    elif args.flat:
        records = scan_flat_folder(Path(args.flat))
    else:
        if args.scan:
            from scan_dataset import scan_collection_root, write_index
            _p("Scanning ZIP archives...")
            records = scan_collection_root(Path(args.source))
            write_index(records, output_root / "dataset_index.csv")
        else:
            records = load_index(Path(args.index))
            if not records:
                _p("ERROR: No records found. Run with --scan first.")
                sys.exit(1)

    if args.limit:
        records = records[: args.limit]

    stages = (args.stage,) if args.stage else ALL_STAGES
    stage_names = " -> ".join(_STAGE_LABEL.get(s, s) for s in stages)

    _banner(
        f"Oklahoma Well Records Pipeline\n"
        f"  Records : {len(records):,}\n"
        f"  Stages  : {stage_names}\n"
        f"  Resume  : {'ON  (completed stages are skipped)' if args.resume else 'OFF (all stages reprocessed)'}\n"
        f"  Output  : {output_root}\n"
        f"  API cache: {_cache_stats_before['entries']:,} entries "
        f"({_cache_stats_before['size_mb']} MB)"
    )

    # Init status — full traceability fields per record.
    from config import decade_for, tier_for
    for r in records:
        tier   = tier_for(r.collection_num)
        decade = decade_for(r.year)
        status.init_record(
            r.pdf_stem, r.pdf_path,
            r.collection, r.year, r.month,
            zip_path=r.zip_path,
            internal_path=r.internal_path,
            collection_num=r.collection_num,
            model_tier=tier,
            decade=decade,
        )
    status.force_save()

    # -- Group by (collection, year, month) ------------------------------------
    def _key(r: DatasetRecord):
        return (r.collection or "", r.year or "", r.month or "")

    sorted_records = sorted(records, key=_key)
    month_groups   = [(k, list(g)) for k, g in groupby(sorted_records, key=_key)]
    if not month_groups:
        month_groups = [(("", "", ""), records)]

    # -- Main loop -------------------------------------------------------------
    total_done = total_failed = total_skipped = 0
    record_num = 0
    year_group_records: list = []
    prev_year_key: tuple | None = None
    workers = max(1, int(getattr(args, "workers", 1) or 1))
    insights = InsightsCollector(output_root, workers=workers,
                                 total_records=len(records))

    for (collection, year, month), month_recs in month_groups:
        cur_year_key = (collection, year)

        if prev_year_key and cur_year_key != prev_year_key:
            _retry_failed(year_group_records, stages, paths, status,
                          total=len(records),
                          label=f"{prev_year_key[0]} / {prev_year_key[1]}")
            year_group_records = []

        _section_header(collection, year, month, len(month_recs))

        month_done = month_failed = month_skipped = 0

        # Pre-filter resume-skips and assign stable record numbers.
        work_items: list = []
        record_by_stem: dict = {}
        for record in month_recs:
            record_num += 1
            if args.resume and all(status.is_done_or_skipped(record.pdf_stem, s) for s in stages):
                month_skipped += 1
                total_skipped += 1
                continue
            prior = dict(status._rows.get(record.pdf_stem, {}))
            work_items.append((record, stages, str(paths.root), args.resume,
                               prior, record_num, len(records)))
            record_by_stem[record.pdf_stem] = record

        def _consume(result_iter):
            """Print + apply each worker result as it arrives."""
            nonlocal month_done, month_failed, total_done, total_failed
            for stem, results, lines in result_iter:
                for ln in lines:
                    _p(ln)
                rec = record_by_stem.get(stem)
                if rec is None:
                    continue
                try:
                    _apply_results(rec, stages, results, status)
                except Exception as exc:
                    _p(f"  apply_results failed for {stem}: {exc}")
                # Insights aggregation (single-writer, parent process).
                try:
                    insights.add(rec, results)
                except Exception as exc:
                    _p(f"  insights.add failed for {stem}: {exc}")
                any_failed = any(
                    isinstance(r, dict) and r.get("error") and not r.get("detected")
                    for r in results.values()
                )
                if any_failed:
                    month_failed += 1; total_failed += 1
                else:
                    month_done   += 1; total_done   += 1

        if not work_items:
            pass
        elif workers <= 1:
            # Sequential: still goes through the worker (single code path).
            _consume(_process_record_worker(w) for w in work_items)
        else:
            # Parallel: imap_unordered streams results back as soon as they finish.
            # chunksize=1 keeps memory low and lets fast records report quickly.
            with mp.Pool(processes=workers) as pool:
                _consume(pool.imap_unordered(_process_record_worker,
                                             work_items, chunksize=1))

        # Month summary
        _p(f"\n  Month complete: {month_done} done | {month_failed} failed | {month_skipped} skipped")

        status.force_save()
        _retry_failed(month_recs, stages, paths, status,
                      total=len(records),
                      label=f"{collection} / {year} / {month}")

        _totals_line(total_done, total_failed, total_skipped, len(records))

        year_group_records.extend(month_recs)
        prev_year_key = cur_year_key

    # Final year retry
    if year_group_records:
        _retry_failed(year_group_records, stages, paths, status,
                      total=len(records),
                      label=f"{prev_year_key[0]} / {prev_year_key[1]}")

    # -- Final summary ---------------------------------------------------------
    status.force_save()
    counts = status.counts()

    _banner(
        f"Run Complete\n"
        f"  Processed : {total_done + total_failed:,}   "
        f"done={total_done:,}  failed={total_failed:,}  skipped={total_skipped:,}"
    )
    for s in ALL_STAGES:
        c   = counts.get(s, {})
        lbl = _STAGE_LABEL.get(s, s)
        _p(f"  {lbl:<12}  done={c.get(DONE,0):<7,}  "
           f"failed={c.get(FAILED,0):<7,}  "
           f"skipped={c.get(SKIPPED,0):<7,}  "
           f"pending={c.get('pending',0):,}")

    _p()
    write_summary_csvs(status, output_root)
    write_latlong_csv(status, output_root / "latlong_records.csv")
    write_dot_locations_csv(status, output_root / "dot_locations.csv")

    try:
        md_path, json_path = insights.write()
        _p(f"  run_insights.md  -> {md_path}")
        _p(f"  run_insights.json -> {json_path}")
    except Exception as exc:
        _p(f"  insights.write failed: {exc}")

    # Failure analysis CSV (stage × error_type × tier breakdown).
    try:
        from utils.failure_analysis import append_run_history, generate_failure_analysis
        fa_path    = output_root / "failure_analysis.csv"
        fa_summary = generate_failure_analysis(
            output_root / "processing_status.csv", fa_path
        )
        if fa_summary:
            _p(f"  failure_analysis.csv  ({fa_summary['total_failures']:,} failures)  -> {fa_path}")
    except Exception as exc:
        _p(f"  failure_analysis failed: {exc}")
        fa_summary = {}

    # Append this run to history for evolutionary learning.
    try:
        cache_stats_after = _api_cache.stats()
        run_summary = {
            "elapsed_s": round(time.monotonic() - _run_start, 1),
            "counts": {s: dict(counts.get(s, {})) for s in ALL_STAGES},
            "cache_stats": {
                "entries":        cache_stats_after["entries"],
                "size_mb":        cache_stats_after["size_mb"],
                "hits_this_run":  cache_stats_after["entries"] - _cache_stats_before["entries"],
            },
            "failure_breakdown": fa_summary.get("breakdown", []) if fa_summary else [],
        }
        append_run_history(output_root, run_summary)
    except Exception as exc:
        _p(f"  append_run_history failed: {exc}")

    # Self-learning: emit parameter suggestions if trends are clear.
    try:
        from utils.evolutionary import learn_from_run
        suggestions = learn_from_run(output_root)
        if suggestions:
            _p(f"  {len(suggestions)} parameter suggestion(s) -> "
               f"{output_root / 'parameter_suggestions.json'}")
    except Exception as exc:
        _p(f"  learn_from_run failed: {exc}")

    # PLSS coordinate enrichment: resolve dot (row, col) → (lat, lon) via RDS.
    try:
        from coord.coord_enricher import enrich_with_coordinates
        enrich_with_coordinates(
            output_root / "success.csv",
            output_root / "dot_coordinates.csv",
            county_constraints_dir=_county_constraints_dir,
            status_csv=output_root / PROCESSING_STATUS_CSV,
            _print=_p,
        )
    except Exception as exc:
        _p(f"  coordinate enrichment skipped: {exc}")


def print_status(status_csv: Path):
    """Print per-stage done/failed/pending counts from a status CSV."""
    s = ProcessingStatus(status_csv)
    c = s.counts()
    _banner(f"Status -- {status_csv.name}   ({len(s._rows):,} records)")
    for stage in ALL_STAGES:
        sc  = c.get(stage, {})
        lbl = _STAGE_LABEL.get(stage, stage)
        _p(f"  {lbl:<12}  done={sc.get(DONE,0):<7,}  "
           f"failed={sc.get(FAILED,0):<7,}  "
           f"pending={sc.get('pending',0):,}")
    _p()


# -- CLI -----------------------------------------------------------------------

def main():
    """CLI entry point. Parses args and dispatches to run_pipeline or
    print_status. See module docstring for usage examples."""
    ap = argparse.ArgumentParser(description="Oklahoma well records pipeline")
    ap.add_argument("--stage",     choices=list(ALL_STAGES))
    ap.add_argument("--resume",    action="store_true", default=True)
    ap.add_argument("--no-resume", dest="resume", action="store_false")
    ap.add_argument("--limit",     type=int)
    ap.add_argument("--flat",      type=Path, help="Flat PDF folder (testing)")
    ap.add_argument("--pdf",       type=Path, help="Single PDF file")
    ap.add_argument("--scan",      action="store_true",
                    help="Re-scan source ZIPs before processing")
    ap.add_argument("--source",    type=Path, default=SOURCE_ROOT)
    ap.add_argument("--index",     type=Path, default=DATASET_INDEX_CSV)
    ap.add_argument("--output",    type=Path, default=OUTPUT_ROOT)
    ap.add_argument("--status",    action="store_true")
    ap.add_argument("--workers",   type=int, default=1,
                    help="Parallel worker processes (default 1 = sequential). "
                         "Set to N <= os.cpu_count() for ~Nx speedup.")
    ap.add_argument("--verbose",   action="store_true",
                    help="Show DEBUG output on console")
    args = ap.parse_args()

    if args.verbose:
        import logging
        logging.getLogger().setLevel(logging.DEBUG)

    if args.status:
        print_status(Path(args.output) / "processing_status.csv")
        return

    run_pipeline(args)


if __name__ == "__main__":
    main()

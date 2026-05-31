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
    ALL_STAGES, COUNTY_REVIEW_BELOW, DATASET_INDEX_CSV, DOT_REVIEW_BELOW,
    FAILED_RECORDS_CSV, GRID_REVIEW_BELOW, LATLONG_MIN_COLLECTION_NUM,
    LATLONG_REVIEW_BELOW, LOCATION_REVIEW_BELOW,
    OUTPUT_ROOT, PROCESSING_STATUS_CSV,
    RESOLUTION_MULTIPLIER, SOURCE_ROOT,
    STAGE_COUNTY, STAGE_DOT, STAGE_GRID, STAGE_LATLONG, STAGE_LOCATION,
)
from pdf.pdf_manager import PDFDocumentManager
from scan_dataset import DatasetRecord, OutputPathBuilder, load_index, scan_flat_folder
from utils.insights import InsightsCollector
from utils.logging_utils import get_logger, get_pdf_logger
from utils.processing_status import DONE, FAILED, SKIPPED, ProcessingStatus

log = get_logger(__name__)

# -- Output CSV column lists ---------------------------------------------------

_SUMMARY_FIELDS = [
    # Identity
    "pdf_path", "pdf_stem", "collection", "year", "month",
    "zip_path", "model_tier",
    # Lat/Lon
    "latlong_found", "lat", "lon", "latlong_confidence", "latlong_page",
    # Well identity
    "well_name",
    # Grid
    "grid_found", "grid_page", "grid_method", "grid_confidence", "grid_image_path",
    # Location (Section / Township / Range)
    "location_found", "section", "township", "range", "location_confidence",
    "location_quadrant_pdf", "location_quadrant_db",
    # County
    "county_found", "county_name", "county_score", "county_confidence",
    # Dot
    "dot_found", "dot_row", "dot_col", "dot_nw", "dot_confidence",
    "dot_x_norm", "dot_y_norm",
    # Review flags
    "final_status",   # 'success' | 'review' | 'failed'
    "needs_review",   # True if any field below review threshold
    "review_reasons", # semicolon-joined list of weak signals
    # Per-stage status + error type
    "latlong_status", "grid_status", "location_status", "county_status", "dot_status",
    "latlong_error_type", "grid_error_type",
    "location_error_type", "county_error_type", "dot_error_type",
    # Coordinate derivation summary
    "coord_derivation", "coord_latlong_source",
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
    _p(f"\n  {'-'*60}")
    _p(f"  {label}   [{count:,} records]")
    _p(f"  {'-'*60}")


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
    Build a PDFDocumentManager for the record's source PDF.

    PDFs are always flat — never inside a ZIP.
      - pdf_path starts with 's3://' → stream bytes from S3
      - otherwise                    → open local file path directly
    """
    pp = record.pdf_path or ""
    if pp.startswith("s3://"):
        from utils.s3_reader import get_pdf_bytes_s3_flat
        pdf_bytes = get_pdf_bytes_s3_flat(pp)
        return PDFDocumentManager(pdf_bytes=pdf_bytes,
                                  resolution_multiplier=RESOLUTION_MULTIPLIER)
    return PDFDocumentManager(record.pdf_path,
                              resolution_multiplier=RESOLUTION_MULTIPLIER)


# -- CSV writers ---------------------------------------------------------------

def _relativize_paths(obj, output_root: Path):
    """
    Walk a result dict and convert any absolute file-system paths that live
    under output_root into portable POSIX-relative strings.

    Motivation: grid/dot stages store image_path as '/tmp/output/grids/…'
    which is an ephemeral container path useless after the task exits.
    Storing the path relative to output_root lets the map site reconstruct
    the S3 URI as: s3://bucket/slice-prefix/<relative_path>.
    """
    root_str = str(output_root)
    if isinstance(obj, dict):
        return {k: _relativize_paths(v, output_root) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_relativize_paths(v, output_root) for v in obj]
    if isinstance(obj, str) and obj.startswith(root_str):
        try:
            return Path(obj).relative_to(output_root).as_posix()
        except ValueError:
            return obj
    return obj


def write_metadata(record: DatasetRecord, results: dict, paths: OutputPathBuilder):
    """
    Write per-record metadata.json with source info + raw stage outputs.

    Image paths are stored relative to output_root (not absolute container
    paths) so the map site can build S3 URIs portably after upload.
    """
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
        "processed_at":  _now(),
        "output_root":   str(paths.root),  # document root for S3 URI construction
        "stages":        _relativize_paths(results, paths.root),
    }
    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return meta_path


def _append_failed(record: DatasetRecord, stage: str, error: str,
                   output_root: Path | None = None):
    """Append one row to <output_root>/manual_review/failed_records.csv."""
    if output_root is None:
        # Fallback: use the config constant (correct for cloud runs where
        # OUTPUT_ROOT env var matches the actual output directory).
        csv_path = FAILED_RECORDS_CSV
    else:
        csv_path = output_root / "manual_review" / "failed_records.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    exists = csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["pdf_stem", "pdf_path", "stage", "error", "timestamp"])
        w.writerow([record.pdf_stem, record.pdf_path, stage, error, _now()])


_REVIEW_QUEUE_FIELDS = [
    "pdf_stem", "pdf_path", "collection", "year", "month",
    "stage", "confidence", "image_path", "reason", "timestamp",
]


def _append_review_queue(output_root: Path, record: DatasetRecord,
                         stage: str, confidence: int,
                         image_path: str, reason: str):
    """
    Append a low-confidence record to manual_review/review_queue.csv.

    The map site / operator downloads this CSV, inspects each image at
    `image_path` (relative to output_root, or the S3 URI after upload),
    and either approves or corrects the detection before the next run.
    """
    review_csv = output_root / "manual_review" / "review_queue.csv"
    review_csv.parent.mkdir(parents=True, exist_ok=True)
    exists = review_csv.exists()
    with review_csv.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_REVIEW_QUEUE_FIELDS,
                           extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow({
            "pdf_stem":   record.pdf_stem,
            "pdf_path":   record.pdf_path,
            "collection": record.collection,
            "year":       record.year,
            "month":      record.month,
            "stage":      stage,
            "confidence": confidence,
            "image_path": image_path,
            "reason":     reason,
            "timestamp":  _now(),
        })


def _review_queue_count_by_stage(output_root: Path) -> dict[str, int]:
    """Read review_queue.csv and return {stage: row_count}. Fast (no full parse)."""
    csv_path = output_root / "manual_review" / "review_queue.csv"
    if not csv_path.exists():
        return {}
    counts: dict[str, int] = {}
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                stage = row.get("stage", "?")
                counts[stage] = counts.get(stage, 0) + 1
    except Exception:
        pass
    return counts


def _print_review_summary(output_root: Path,
                          before: dict[str, int],
                          label: str = "") -> None:
    """
    Print new entries added to review_queue.csv since `before` snapshot.
    Call with before=_review_queue_count_by_stage(output_root) captured
    at the start of a batch group, then call again after the group finishes.
    """
    after  = _review_queue_count_by_stage(output_root)
    delta  = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    delta  = {k: v for k, v in delta.items() if v > 0}
    total  = sum(delta.values())
    if total == 0:
        return
    suffix = f"  [{label}]" if label else ""
    _p(f"\n  -- Review queue{suffix}: {total} record(s) flagged --")
    for stage, count in sorted(delta.items()):
        threshold = {
            STAGE_LATLONG:  f"conf<{LATLONG_REVIEW_BELOW}%",
            STAGE_GRID:     f"conf<{GRID_REVIEW_BELOW}%",
            STAGE_LOCATION: f"partial STR (<{LOCATION_REVIEW_BELOW}%)",
            STAGE_COUNTY:   f"score<{COUNTY_REVIEW_BELOW}%",
            STAGE_DOT:      f"conf<{DOT_REVIEW_BELOW}%",
        }.get(stage, "low confidence")
        _p(f"    {stage:<12}  {count:>4} record(s)  ({threshold})")
    _p(f"    -> manual_review/review_queue.csv")


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
        # Identity
        "pdf_path":           row.get("pdf_path", ""),
        "pdf_stem":           stem,
        "collection":         row.get("collection", ""),
        "year":               row.get("year", ""),
        "month":              row.get("month", ""),
        "zip_path":           row.get("zip_path", ""),
        "model_tier":         row.get("model_tier", ""),
        # Lat/Lon
        "latlong_found":      ll,
        "lat":                row.get("latlong_lat", ""),
        "lon":                row.get("latlong_lon", ""),
        "latlong_confidence": row.get("latlong_confidence", ""),
        "latlong_page":       row.get("latlong_page", ""),
        # Well identity
        "well_name":          _well_name_from_stem(stem),
        # Grid
        "grid_found":         row.get("grid_status") == DONE,
        "grid_page":          row.get("grid_page", ""),
        "grid_method":        row.get("grid_method", ""),
        "grid_confidence":    row.get("grid_confidence", ""),
        "grid_image_path":    row.get("grid_image_path", ""),
        # Location
        "location_found":        row.get("location_status") == DONE,
        "section":               row.get("location_section", ""),
        "township":              row.get("location_township", ""),
        "range":                 row.get("location_range", ""),
        "location_confidence":   row.get("location_confidence", ""),
        "location_quadrant_pdf": row.get("location_quadrant_pdf", ""),
        "location_quadrant_db":  row.get("location_quadrant_db", ""),
        # County
        "county_found":      row.get("county_status") == DONE,
        "county_name":       row.get("county_name", ""),
        "county_score":      row.get("county_score", ""),
        "county_confidence": row.get("county_confidence", ""),
        # Dot
        "dot_found":      row.get("dot_status") == DONE,
        "dot_row":        row.get("dot_row", ""),
        "dot_col":        row.get("dot_col", ""),
        "dot_nw":         row.get("dot_nw", ""),
        "dot_confidence": row.get("dot_confidence", ""),
        "dot_x_norm":     row.get("dot_x_norm", ""),
        "dot_y_norm":     row.get("dot_y_norm", ""),
        # Review
        "final_status":  final_status,
        "needs_review":  final_status == "review",
        "review_reasons": "; ".join(reasons),
        # Per-stage status + error
        "latlong_status":      row.get("latlong_status", ""),
        "grid_status":         row.get("grid_status", ""),
        "location_status":     row.get("location_status", ""),
        "county_status":       row.get("county_status", ""),
        "dot_status":          row.get("dot_status", ""),
        "latlong_error_type":  row.get("latlong_error_type", ""),
        "grid_error_type":     row.get("grid_error_type", ""),
        "location_error_type": row.get("location_error_type", ""),
        "county_error_type":   row.get("county_error_type", ""),
        "dot_error_type":      row.get("dot_error_type", ""),
        # Coord derivation
        "coord_derivation":     row.get("coord_derivation", ""),
        "coord_latlong_source": row.get("coord_latlong_source", ""),
    }


def _stage_quality_summary(status: "ProcessingStatus") -> None:
    """
    Print extraction quality metrics after all records are processed.

    Shows:
      • Full STR rate (section + township + range all present)
      • Lat/Lon coverage
      • Dot coverage
      • Top 10 counties by occurrence
      • Average confidence per stage (done records only)
    """
    rows = list(status._rows.values())
    n    = len(rows)
    if n == 0:
        return

    # STR completeness
    str_full  = sum(1 for r in rows if r.get("location_section")
                    and r.get("location_township") and r.get("location_range"))
    str_any   = sum(1 for r in rows if r.get("location_section")
                    or r.get("location_township") or r.get("location_range"))
    # Lat/Lon
    ll_found  = sum(1 for r in rows if r.get("latlong_lat") and r.get("latlong_lon"))
    # Dot
    dot_found = sum(1 for r in rows if r.get("dot_row") and r.get("dot_col"))

    # County frequency
    from collections import Counter
    county_ctr: Counter = Counter(
        r.get("county_name", "").strip()
        for r in rows if r.get("county_name", "").strip()
    )

    # Avg confidence per stage (done only)
    def _avg_conf(stage: str) -> str:
        vals = []
        for r in rows:
            if r.get(f"{stage}_status") == DONE:
                try:
                    vals.append(float(r.get(f"{stage}_confidence", 0) or 0))
                except (ValueError, TypeError):
                    pass
        return f"{sum(vals)/len(vals):.0f}" if vals else "—"

    _p()
    _p("  ── Extraction Quality ─────────────────────────────────────")
    _p(f"  {'Lat/Lon':<20} {ll_found:>6,} / {n:,}  "
       f"({100*ll_found//n if n else 0}%)")
    _p(f"  {'Full STR':<20} {str_full:>6,} / {n:,}  "
       f"({100*str_full//n if n else 0}%)")
    _p(f"  {'Any STR field':<20} {str_any:>6,} / {n:,}  "
       f"({100*str_any//n if n else 0}%)")
    _p(f"  {'Dot detected':<20} {dot_found:>6,} / {n:,}  "
       f"({100*dot_found//n if n else 0}%)")
    _p()
    _p("  ── Avg Confidence (done records) ──────────────────────────")
    for s in ALL_STAGES:
        lbl = _STAGE_LABEL.get(s, s)
        _p(f"  {lbl:<20} {_avg_conf(s):>4}")
    if county_ctr:
        _p()
        _p("  ── Top Counties ───────────────────────────────────────────")
        for cname, cnt in county_ctr.most_common(10):
            _p(f"  {cname:<25} {cnt:>6,}")
    _p("  ───────────────────────────────────────────────────────────")


def write_summary_csvs(status: ProcessingStatus, output_root: Path):
    """
    Write the two terminal CSVs. Every record lands in EXACTLY ONE of them:
      - <output>/success.csv  (final_status in {'success', 'review'})
      - <output>/manual_review/failed.csv  (final_status == 'failed')

    'review' rows live in success.csv with `needs_review=True` and a
    `review_reasons` column listing what triggered the flag — so they
    aren't duplicated to the failed file.
    """
    success_path = output_root / "success.csv"
    failed_path  = output_root / "manual_review" / "failed.csv"
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
                "well_type":          "",
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

    # Track whether a stage threw an unhandled exception.  PyMuPDF (fitz) can
    # leave internal page caches in an undefined state after a mid-render error,
    # causing cascade failures in subsequent stages.  Recreating the manager
    # before the next stage gives each stage a clean document handle.
    manager_dirty = False

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

        # Skip location for early/transition tiers — handwritten STR is unreadable
        # by Tesseract (50-150s wasted per record, always returns not_found).
        # Guard: only skip when collection_num is known (>0); unknown collections
        # (ad-hoc --flat test runs) keep location enabled.
        if stage == STAGE_LOCATION:
            _cnum_loc = getattr(record, "collection_num", None) or 0
            if _cnum_loc:
                from config import TIER_CONFIG, tier_for as _tf_loc
                _loc_tier = _tf_loc(_cnum_loc)
                if not TIER_CONFIG.get(_loc_tier, {}).get("run_location", True):
                    lines.append(
                        f"  {label:<{_COL}}skipped  "
                        f"(tier '{_loc_tier}' STR is handwritten, unreadable)"
                    )
                    results[stage] = SKIPPED
                    continue

        # Skip grid + location when lat/lon was already found (this run or prior).
        # Only apply the "prior run" branch when resume=True — with --no-resume the
        # prior row's latlong_lat/lon must not influence whether we run these stages.
        if stage in (STAGE_GRID, STAGE_LOCATION):
            ll_r = results.get(STAGE_LATLONG, {})
            ll_found_this_run = isinstance(ll_r, dict) and ll_r.get("detected", False)
            ll_found_prior    = (
                resume
                and bool(prior_row.get("latlong_lat"))
                and bool(prior_row.get("latlong_lon"))
            )
            if ll_found_this_run or ll_found_prior:
                reason = "lat/lon found this run" if ll_found_this_run else "lat/lon found in prior run"
                lines.append(f"  {label:<{_COL}}skipped  ({reason})")
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

        # If a prior stage left PyMuPDF in an undefined state, recreate the
        # manager before attempting the next stage so each stage sees a clean
        # document handle and failures don't cascade through all remaining stages.
        if manager_dirty:
            try:
                manager = _make_manager(record)
                manager_dirty = False
                pdf_log.debug("manager recreated after prior stage exception")
            except Exception as exc:
                pdf_log.error("Cannot recreate PDF manager: %s", exc)
                r = {"detected": False, "error": f"manager_recreate_failed: {exc}"}
                if isinstance(r, dict):
                    r["_elapsed"] = 0.0
                lines.append(f"  {label:<{_COL}}{_format_stage_result(stage, r, 0.0)}")
                results[stage] = r
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
            # Flag the manager as potentially corrupted so the next stage gets
            # a fresh document handle rather than inheriting broken PyMuPDF state.
            manager_dirty = True
        elapsed = time.monotonic() - t0
        pdf_log.debug("[%s] %.1fs detected=%s", stage.upper(), elapsed,
                      r.get("detected"))

        # Stamp elapsed so the parent's insights collector can aggregate it.
        if isinstance(r, dict):
            r["_elapsed"] = elapsed

        lines.append(f"  {label:<{_COL}}{_format_stage_result(stage, r, elapsed)}")
        results[stage] = r

        # ---- Post-stage side-effects ----------------------------------------

        if isinstance(r, dict) and r.get("detected"):
            conf       = int(r.get("confidence", 0))
            img_path   = r.get("image_path", "")
            rel_img    = ""
            if img_path:
                try:
                    rel_img = Path(img_path).relative_to(Path(output_root_str)).as_posix()
                except ValueError:
                    rel_img = img_path

            # Annotated full-page view is more useful for human review than the
            # tight crop; use it when present, fall back to crop path.
            ann_path = r.get("annotated_path", "")
            rel_ann  = ""
            if ann_path:
                try:
                    rel_ann = Path(ann_path).relative_to(Path(output_root_str)).as_posix()
                except ValueError:
                    rel_ann = ann_path
            review_img = rel_ann or rel_img

            # Flag low-confidence lat/lon for manual review.
            if stage == STAGE_LATLONG:
                if conf < LATLONG_REVIEW_BELOW:
                    reason = f"latlong_conf={conf}<{LATLONG_REVIEW_BELOW}"
                    pdf_log.info("Low-confidence latlong (%d%%) → review queue", conf)
                    try:
                        _append_review_queue(
                            Path(output_root_str), record,
                            STAGE_LATLONG, conf, rel_img, reason,
                        )
                    except Exception as _rq_exc:
                        pdf_log.debug("review_queue write failed: %s", _rq_exc)

            # Flag low-confidence grid for manual review.
            if stage == STAGE_GRID:
                if conf < GRID_REVIEW_BELOW:
                    reason = f"grid_conf={conf}<{GRID_REVIEW_BELOW}"
                    pdf_log.info("Low-confidence grid (%d%%) → review queue", conf)
                    try:
                        _append_review_queue(
                            Path(output_root_str), record,
                            STAGE_GRID, conf, rel_img, reason,
                        )
                    except Exception as _rq_exc:
                        pdf_log.debug("review_queue write failed: %s", _rq_exc)

            # Flag partial location (any STR field missing) for manual review.
            if stage == STAGE_LOCATION:
                if conf < LOCATION_REVIEW_BELOW:
                    missing = [
                        f for f, k in [("sec", "section"), ("twp", "township"), ("rng", "range")]
                        if not r.get(k)
                    ]
                    reason = (
                        f"partial STR ({conf}%): missing "
                        f"{', '.join(missing) if missing else 'unknown'}"
                    )
                    pdf_log.info("Partial location STR (%d%%) → review queue", conf)
                    try:
                        _append_review_queue(
                            Path(output_root_str), record,
                            STAGE_LOCATION, conf, review_img, reason,
                        )
                    except Exception as _rq_exc:
                        pdf_log.debug("review_queue write failed: %s", _rq_exc)

            # Flag low-confidence county match for manual review.
            if stage == STAGE_COUNTY:
                score = int(r.get("fuzzy_score", conf))
                if score < COUNTY_REVIEW_BELOW:
                    reason = (
                        f"county_score={score}<{COUNTY_REVIEW_BELOW}"
                        f" ({r.get('name', '')})"
                    )
                    pdf_log.info("Low-confidence county (%d%%) → review queue", score)
                    try:
                        _append_review_queue(
                            Path(output_root_str), record,
                            STAGE_COUNTY, score, review_img, reason,
                        )
                    except Exception as _rq_exc:
                        pdf_log.debug("review_queue write failed: %s", _rq_exc)

            # Flag low-confidence dot for manual review.
            if stage == STAGE_DOT:
                if conf < DOT_REVIEW_BELOW:
                    reason = f"dot_conf={conf}<{DOT_REVIEW_BELOW}"
                    pdf_log.info("Low-confidence dot (%d%%) → review queue", conf)
                    try:
                        _append_review_queue(
                            Path(output_root_str), record,
                            STAGE_DOT, conf, rel_img, reason,
                        )
                    except Exception as _rq_exc:
                        pdf_log.debug("review_queue write failed: %s", _rq_exc)

        # After dot stage (detected or not), the grid PNG is no longer needed
        # locally — it has been uploaded to S3 by the checkpoint thread.
        # Delete it now to reclaim disk space immediately.
        if stage == STAGE_DOT:
            grid_dir = paths.grids_dir(record)
            for _gp in grid_dir.glob(f"{record.pdf_stem}_page_*_grid.png"):
                try:
                    _gp.unlink(missing_ok=True)
                    pdf_log.debug("Deleted grid PNG after dot stage: %s", _gp.name)
                except Exception:
                    pass

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
                   results: dict, status: ProcessingStatus,
                   output_root: Path | None = None):
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
                _append_failed(record, stage, err, output_root=output_root)
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
    _apply_results(record, stages, results, status, output_root=paths.root)
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
        from config import TIER_EARLY, TIER_TRANSITION, tier_for
        _cnum        = getattr(record, "collection_num", None) or 0
        _tier        = tier_for(_cnum)
        # Only skip the Tesseract anchor when we *know* this is an early/transition
        # collection (handwritten docs with no printed anchor phrase). Unknown
        # collection_num (0 / None) keeps anchor enabled so ad-hoc --flat runs
        # still benefit from the anchor strategy.
        _skip_anchor = bool(_cnum) and _tier in (TIER_EARLY, TIER_TRANSITION)
        return process_single_grid(manager, out_dir, pdf_stem, log,
                                   skip_anchor=_skip_anchor)

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
                     pdf_stem: str, pdf_log, skip_anchor: bool = False):
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
            skip_anchor=skip_anchor,
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

    from config import TIER_EARLY, TIER_TRANSITION, tier_for as _tier_for
    _cnum        = getattr(record, "collection_num", None) or 0
    _tier        = _tier_for(_cnum)
    _skip_anchor = bool(_cnum) and _tier in (TIER_EARLY, TIER_TRANSITION)

    parts: list[str] = []
    for stage in stages:
        if status.get_status(record.pdf_stem, stage) != FAILED:
            continue
        et = status.get_error_type(record.pdf_stem, stage) or "unknown"

        t0 = time.monotonic()
        try:
            r = _retry_one_stage(stage, et, manager,
                                 stage_dirs[stage], record.pdf_stem, pdf_log,
                                 skip_anchor=_skip_anchor)
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
            _append_failed(record, stage, f"retry({et}): {new_err}",
                           output_root=paths.root)
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
    # Exit 130 (128 + SIGTERM=15) signals preemption to the parent wrapper
    # (run_batch_job.py) so it does NOT mark the slice as successfully complete.
    import signal
    def _sigterm_handler(signum, frame):
        _p("\n  SIGTERM received — flushing status and exiting (preempted)...")
        # Wrap force_save in try/except: the .tmp rename can fail if OUTPUT_ROOT
        # isn't yet writable (e.g. SIGTERM arrives before the first status write).
        try:
            status.force_save()
        except Exception as _se:
            _p(f"  force_save error during SIGTERM (non-fatal): {_se}")
        sys.exit(130)
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

    # Batch slice partitioning: each container handles 1/N of the records.
    # Records are sorted by pdf_stem for deterministic, reproducible slices.
    if args.slice_index is not None and args.total_slices:
        records.sort(key=lambda r: r.pdf_stem)
        n = args.total_slices
        i = args.slice_index
        records = [r for idx, r in enumerate(records) if idx % n == i]
        _p(f"  Batch slice {i}/{n}: {len(records):,} records assigned to this job")

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

    # Init status — traceability fields per record.
    from config import tier_for
    for r in records:
        tier = tier_for(r.collection_num)
        status.init_record(
            r.pdf_stem, r.pdf_path,
            r.collection, r.year, r.month,
            zip_path=r.zip_path,
            collection_num=r.collection_num,
            model_tier=tier,
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

    _before_run = _review_queue_count_by_stage(output_root)

    for (collection, year, month), month_recs in month_groups:
        cur_year_key = (collection, year)

        if prev_year_key and cur_year_key != prev_year_key:
            _retry_failed(year_group_records, stages, paths, status,
                          total=len(records),
                          label=f"{prev_year_key[0]} / {prev_year_key[1]}")
            year_group_records = []

        _before_month = _review_queue_count_by_stage(output_root)
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
                    _apply_results(rec, stages, results, status,
                                   output_root=output_root)
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

        _print_review_summary(output_root, _before_month,
                              f"{collection}/{year}/{month}")
        _totals_line(total_done, total_failed, total_skipped, len(records))

        year_group_records.extend(month_recs)
        prev_year_key = cur_year_key

    # Final year retry
    if year_group_records:
        _retry_failed(year_group_records, stages, paths, status,
                      total=len(records),
                      label=f"{prev_year_key[0]} / {prev_year_key[1]}")

    _print_review_summary(output_root, _before_run, "full run")

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

    _stage_quality_summary(status)
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
    # AWS Batch slice args: partition the dataset_index into N equal slices
    # and process only slice number I (0-based).  run_batch_job.py sets these
    # via env vars BATCH_SLICE_INDEX / BATCH_TOTAL_SLICES.
    ap.add_argument("--slice-index",  type=int, default=None,
                    help="(Batch) 0-based index of this job's slice")
    ap.add_argument("--total-slices", type=int, default=None,
                    help="(Batch) total number of slices")
    args = ap.parse_args()

    # Env-var fallback for Batch (container sets BATCH_SLICE_INDEX etc.)
    if args.slice_index  is None and os.environ.get("BATCH_SLICE_INDEX"):
        args.slice_index  = int(os.environ["BATCH_SLICE_INDEX"])
    if args.total_slices is None and os.environ.get("BATCH_TOTAL_SLICES"):
        args.total_slices = int(os.environ["BATCH_TOTAL_SLICES"])

    if args.verbose:
        import logging
        logging.getLogger().setLevel(logging.DEBUG)

    if args.status:
        print_status(Path(args.output) / "processing_status.csv")
        return

    run_pipeline(args)


if __name__ == "__main__":
    main()

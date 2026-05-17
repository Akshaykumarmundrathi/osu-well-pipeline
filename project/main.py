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
import sys
import time
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    ALL_STAGES, DATASET_INDEX_CSV, FAILED_RECORDS_CSV,
    OUTPUT_ROOT, PROCESSING_STATUS_CSV,
    RESOLUTION_MULTIPLIER, SOURCE_ROOT,
    STAGE_COUNTY, STAGE_GRID, STAGE_LATLONG, STAGE_LOCATION,
)
from pdf.pdf_manager import PDFDocumentManager
from scan_dataset import DatasetRecord, OutputPathBuilder, load_index, scan_flat_folder
from utils.logging_utils import get_logger, get_pdf_logger
from utils.processing_status import DONE, FAILED, SKIPPED, ProcessingStatus
from utils.zip_reader import get_pdf_bytes

log = get_logger(__name__)

# -- Output CSV column lists ---------------------------------------------------

_SUMMARY_FIELDS = [
    "pdf_path", "pdf_stem", "collection", "year", "month",
    "latlong_found", "lat", "lon", "latlong_confidence", "latlong_page",
    "well_name", "well_type",
    "grid_found", "grid_page", "grid_method", "grid_confidence",
    "location_found", "section", "township", "range", "location_confidence",
    "county_found", "county_name", "county_score", "county_confidence",
    "all_success",
    "latlong_status", "grid_status", "location_status", "county_status",
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
    Build a PDFDocumentManager for the record's source — either raw bytes
    extracted from a ZIP entry, or a direct file path on disk.
    """
    if record.zip_path:
        pdf_bytes = get_pdf_bytes(record.zip_path, record.internal_path)
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


def write_summary_csv(status: ProcessingStatus, output_path: Path):
    """
    Materialize the full per-record summary CSV from the in-memory status
    rows. `all_success` is True when the record has either lat/lon OR
    (grid AND location), plus a county match.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_SUMMARY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in status._rows.values():
            stem = row.get("pdf_stem", "")
            ll   = bool(row.get("latlong_lat")) and bool(row.get("latlong_lon"))
            g    = row.get("grid_status")     == DONE
            l    = row.get("location_status") == DONE
            c    = row.get("county_status")   == DONE
            writer.writerow({
                "pdf_path":            row.get("pdf_path", ""),
                "pdf_stem":            stem,
                "collection":          row.get("collection", ""),
                "year":                row.get("year", ""),
                "month":               row.get("month", ""),
                "latlong_found":       ll,
                "lat":                 row.get("latlong_lat", ""),
                "lon":                 row.get("latlong_lon", ""),
                "latlong_confidence":  row.get("latlong_confidence", ""),
                "latlong_page":        row.get("latlong_page", ""),
                "well_name":           _well_name_from_stem(stem),
                "well_type":           row.get("latlong_well_type", ""),
                "grid_found":          g,
                "grid_page":           row.get("grid_page", ""),
                "grid_method":         row.get("grid_method", ""),
                "grid_confidence":     row.get("grid_confidence", ""),
                "location_found":      l,
                "section":             row.get("location_section", ""),
                "township":            row.get("location_township", ""),
                "range":               row.get("location_range", ""),
                "location_confidence": row.get("location_confidence", ""),
                "county_found":        c,
                "county_name":         row.get("county_name", ""),
                "county_score":        row.get("county_score", ""),
                "county_confidence":   row.get("county_confidence", ""),
                "all_success":         (ll or (g and l)) and c,
                "latlong_status":      row.get("latlong_status", ""),
                "grid_status":         row.get("grid_status", ""),
                "location_status":     row.get("location_status", ""),
                "county_status":       row.get("county_status", ""),
            })
            count += 1
    _p(f"  Summary CSV written  ({count:,} rows)  ->  {output_path}")


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


# -- Per-record processing -----------------------------------------------------

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
    Process a single PDF through the given stages.
    - Opens the PDF (failure marks ALL stages failed and returns {})
    - For each stage: skip if already done (resume); skip grid/location
      when lat/lon was found; otherwise dispatch and record result.
    - Writes per-stage status + a metadata.json for stages that actually ran.
    Returns the per-stage results dict.
    """
    pdf_log = get_pdf_logger(record.pdf_stem, paths.log_path(record))
    pdf_log.debug("=== START %s ===", record.pdf_stem)

    well_name = _well_name_from_stem(record.pdf_stem)

    # Open PDF
    try:
        manager = _make_manager(record)
        pages   = manager.page_count()
        pdf_log.debug("pages: %d", pages)
    except Exception as exc:
        pdf_log.error("Cannot open PDF: %s", exc)
        _record_header(record_num, total, well_name,
                       record.collection, record.year, record.month, 0)
        _p(f"  {'ERROR':<{_COL}}Cannot open PDF -- {exc}")
        for stage in stages:
            status.mark_failed(record.pdf_stem, stage, f"open_failed: {exc}")
            _append_failed(record, stage, str(exc))
        return {}

    _record_header(record_num, total, well_name,
                   record.collection, record.year, record.month, pages)

    results: dict = {}   # stage -> result dict or SKIPPED sentinel
    stage_dirs = {
        STAGE_LATLONG:  paths.grids_dir(record),  # latlong writes no images
        STAGE_GRID:     paths.grids_dir(record),
        STAGE_LOCATION: paths.locations_dir(record),
        STAGE_COUNTY:   paths.counties_dir(record),
    }

    for stage in stages:
        label = _STAGE_LABEL.get(stage, stage)

        if resume and status.is_done(record.pdf_stem, stage):
            _stage_line(label, "already done")
            results[stage] = {"detected": True, "_was_done": True}
            continue

        # Skip grid + location when lat/lon already found
        if stage in (STAGE_GRID, STAGE_LOCATION):
            ll_found = (
                results.get(STAGE_LATLONG, {}).get("detected", False)
                or status.latlong_detected(record.pdf_stem)
            )
            if ll_found:
                _stage_line(label, "skipped  (lat/lon found in document)")
                status.mark_skipped(record.pdf_stem, stage)
                results[stage] = SKIPPED
                continue

        _stage_start(label)
        t0 = time.monotonic()

        try:
            r = _dispatch(stage, manager, stage_dirs[stage],
                          record.pdf_stem, pdf_log)
        except Exception as exc:
            pdf_log.error("[%s] unhandled exception: %s", stage.upper(), exc,
                          exc_info=True)
            r = {"detected": False, "error": str(exc)}

        elapsed = time.monotonic() - t0
        pdf_log.debug("[%s] %.1fs detected=%s", stage.upper(), elapsed,
                      r.get("detected"))

        # Print result on same line as label
        print(_format_stage_result(stage, r, elapsed), flush=True)
        results[stage] = r

        if r.get("error") and not r.get("detected"):
            err = r["error"]
            status.mark_failed(record.pdf_stem, stage, err)
            _append_failed(record, stage, err)
        else:
            status.mark_done(record.pdf_stem, stage, r)

    # Status summary line
    _record_status_line(results, stages)

    if any(v not in (SKIPPED,) and not isinstance(v, dict)
           or isinstance(v, dict) and not v.get("_was_done")
           for v in results.values()):
        real = {k: v for k, v in results.items()
                if isinstance(v, dict) and not v.get("_was_done")}
        if real:
            mp = write_metadata(record, real, paths)
            pdf_log.debug("metadata -> %s", mp)

    pdf_log.debug("=== END %s ===", record.pdf_stem)
    return results


def _dispatch(stage: str, manager: PDFDocumentManager,
              out_dir: Path, pdf_stem: str, log) -> dict:
    """Route a stage name to its extractor entry point. Lazy-imports each
    sub-module so a stage that's never invoked never pays the import cost."""
    if stage == STAGE_LATLONG:
        from latlong.latlong_extractor import process_single_latlong
        return process_single_latlong(manager, pdf_stem, log)

    if stage == STAGE_GRID:
        from grid.scoring import process_single_grid
        return process_single_grid(manager, out_dir, pdf_stem, log)

    if stage == STAGE_LOCATION:
        from location.location_extractor import process_single_location
        return process_single_location(manager, out_dir, pdf_stem, log)

    if stage == STAGE_COUNTY:
        from county.county_extractor import process_single_county
        return process_single_county(manager, out_dir, pdf_stem, log)

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
        f"  Output  : {output_root}"
    )

    # Init status
    for r in records:
        status.init_record(r.pdf_stem, r.pdf_path,
                           r.collection, r.year, r.month)
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

    for (collection, year, month), month_recs in month_groups:
        cur_year_key = (collection, year)

        if prev_year_key and cur_year_key != prev_year_key:
            _retry_failed(year_group_records, stages, paths, status,
                          total=len(records),
                          label=f"{prev_year_key[0]} / {prev_year_key[1]}")
            year_group_records = []

        _section_header(collection, year, month, len(month_recs))

        month_done = month_failed = month_skipped = 0

        for record in month_recs:
            record_num += 1

            if args.resume and all(status.is_done(record.pdf_stem, s) for s in stages):
                month_skipped += 1
                total_skipped += 1
                continue

            try:
                results = run_one_record(
                    record, stages, paths, status,
                    resume=args.resume,
                    record_num=record_num, total=len(records),
                )
                any_failed = any(
                    isinstance(r, dict) and r.get("error") and not r.get("detected")
                    for r in results.values()
                )
                if any_failed:
                    month_failed += 1
                    total_failed += 1
                else:
                    month_done   += 1
                    total_done   += 1
            except Exception as exc:
                _p(f"  FATAL error on {record.pdf_stem}: {exc}")
                for s in stages:
                    status.mark_failed(record.pdf_stem, s, str(exc))
                _append_failed(record, "pipeline", str(exc))
                month_failed += 1
                total_failed += 1

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
    write_summary_csv(status, output_root / "summary.csv")
    write_latlong_csv(status, output_root / "latlong_records.csv")


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

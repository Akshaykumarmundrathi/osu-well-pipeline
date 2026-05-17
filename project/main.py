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

Processing order:
  Records are grouped by (collection, year, month).
  After each month completes, failed records in that month are retried once.
  After each year completes, remaining failures in that year are retried once.
  A final summary.csv with all fields is written at the end.
"""

import argparse
import atexit
import csv
import json
import logging
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
from utils.processing_status import DONE, FAILED, ProcessingStatus
from utils.zip_reader import get_pdf_bytes

log = get_logger(__name__)

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


def _well_name_from_stem(pdf_stem: str) -> str:
    first = pdf_stem.find("_")
    last  = pdf_stem.rfind("_")
    if first != -1 and last != first:
        return pdf_stem[first + 1: last]
    return pdf_stem


# -- PDF source resolution -----------------------------------------------------

def _make_manager(record: DatasetRecord) -> PDFDocumentManager:
    if record.zip_path:
        pdf_bytes = get_pdf_bytes(record.zip_path, record.internal_path)
        return PDFDocumentManager(pdf_bytes=pdf_bytes,
                                  resolution_multiplier=RESOLUTION_MULTIPLIER)
    return PDFDocumentManager(record.pdf_path,
                              resolution_multiplier=RESOLUTION_MULTIPLIER)


# -- Writers -------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def write_metadata(record: DatasetRecord, results: dict, paths: OutputPathBuilder):
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
    FAILED_RECORDS_CSV.parent.mkdir(parents=True, exist_ok=True)
    exists = FAILED_RECORDS_CSV.exists()
    with FAILED_RECORDS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["pdf_stem", "pdf_path", "stage", "error", "timestamp"])
        w.writerow([record.pdf_stem, record.pdf_path, stage, error, _now()])


def write_summary_csv(status: ProcessingStatus, output_path: Path):
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
    log.info("Summary CSV -> %s  (%d rows)", output_path, count)


def write_latlong_csv(status: ProcessingStatus, output_path: Path):
    """Write a focused CSV containing only records where lat/lon was found."""
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
    log.info("Lat/Lon CSV -> %s  (%d records with coordinates)", output_path, count)


# -- Per-record processing -----------------------------------------------------

def run_one_record(
    record: DatasetRecord,
    stages: tuple,
    paths: OutputPathBuilder,
    status: ProcessingStatus,
    resume: bool = True,
) -> dict:
    pdf_log = get_pdf_logger(record.pdf_stem, paths.log_path(record))
    pdf_log.info("=== START %s ===", record.pdf_stem)
    pdf_log.info("source: %s", record.pdf_path)

    try:
        manager = _make_manager(record)
        pages   = manager.page_count()
        pdf_log.info("pages: %d", pages)
    except Exception as exc:
        pdf_log.error("Cannot open PDF: %s", exc)
        for stage in stages:
            status.mark_failed(record.pdf_stem, stage, f"open_failed: {exc}")
            _append_failed(record, stage, str(exc))
        return {}

    results: dict = {}
    stage_dirs = {
        STAGE_GRID:     paths.grids_dir(record),
        STAGE_LOCATION: paths.locations_dir(record),
        STAGE_COUNTY:   paths.counties_dir(record),
    }

    for stage in stages:
        if resume and status.is_done(record.pdf_stem, stage):
            pdf_log.info("[%s] already done -- skip", stage.upper())
            continue

        # Skip grid + location if lat/lon was found (this run or a previous run)
        if stage in (STAGE_GRID, STAGE_LOCATION):
            ll_found = (
                results.get(STAGE_LATLONG, {}).get("detected", False)
                or status.latlong_detected(record.pdf_stem)
            )
            if ll_found:
                status.mark_skipped(record.pdf_stem, stage)
                pdf_log.info("[%s] skipped -- lat/lon found in document",
                             stage.upper())
                continue

        pdf_log.info("[%s] starting", stage.upper())
        t0 = time.monotonic()

        try:
            r = _dispatch(stage, manager, stage_dirs[stage],
                          record.pdf_stem, pdf_log)
        except Exception as exc:
            pdf_log.error("[%s] unhandled: %s", stage.upper(), exc, exc_info=True)
            r = {"detected": False, "error": str(exc)}

        elapsed = time.monotonic() - t0
        pdf_log.info("[%s] %.1fs  detected=%s", stage.upper(), elapsed,
                     r.get("detected"))
        results[stage] = r

        if r.get("error") and not r.get("detected"):
            status.mark_failed(record.pdf_stem, stage, r["error"])
            _append_failed(record, stage, r["error"])
        else:
            status.mark_done(record.pdf_stem, stage, r)

    if results:
        mp = write_metadata(record, results, paths)
        pdf_log.info("metadata -> %s", mp)

    pdf_log.info("=== END %s ===\n", record.pdf_stem)
    return results


def _dispatch(stage: str, manager: PDFDocumentManager,
              out_dir: Path, pdf_stem: str, log) -> dict:
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

def _retry_failed(
    records: list,
    stages: tuple,
    paths: OutputPathBuilder,
    status: ProcessingStatus,
    label: str = "",
):
    stems    = [r.pdf_stem for r in records]
    failed   = status.failed_in(stems, stages)
    if not failed:
        return
    failed_set = set(failed)
    to_retry   = [r for r in records if r.pdf_stem in failed_set]
    log.info("Retry%s: %d failed records",
             f" [{label}]" if label else "", len(to_retry))
    for record in to_retry:
        log.info("  retrying %s", record.pdf_stem)
        run_one_record(record, stages, paths, status, resume=True)
    status.force_save()


# -- Pipeline runner -----------------------------------------------------------

def run_pipeline(args):
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)

    paths  = OutputPathBuilder(output_root)
    status = ProcessingStatus(output_root / "processing_status.csv")

    # Always flush on exit (handles Ctrl-C and crashes)
    atexit.register(status.force_save)

    # -- Source records --------------------------------------------------------
    if args.pdf:
        pdf = Path(args.pdf)
        records = [DatasetRecord(pdf_stem=pdf.stem, pdf_path=str(pdf),
                                 collection="cli", collection_safe="cli")]
    elif args.flat:
        records = scan_flat_folder(Path(args.flat))
        log.info("Flat scan: %d PDFs", len(records))
    else:
        if args.scan:
            from scan_dataset import scan_collection_root, write_index
            records = scan_collection_root(Path(args.source))
            write_index(records, output_root / "dataset_index.csv")
        else:
            records = load_index(Path(args.index))
            if not records:
                log.error("No records -- run with --scan first or --index at CSV")
                sys.exit(1)
        log.info("Loaded %d records", len(records))

    if args.limit:
        records = records[: args.limit]

    stages = (args.stage,) if args.stage else ALL_STAGES
    log.info("stages=%s  resume=%s  records=%d", stages, args.resume, len(records))

    # Init status rows (no-op for already-known records)
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
                          label=f"{prev_year_key[0]}/{prev_year_key[1]}")
            year_group_records = []

        log.info("--- %s / %s / %s  (%d records) ---",
                 collection or "cli", year or "-", month or "-", len(month_recs))

        month_done = month_failed = month_skipped = 0

        for record in month_recs:
            record_num += 1

            if args.resume and all(status.is_done(record.pdf_stem, s) for s in stages):
                month_skipped += 1
                total_skipped += 1
                continue

            log.info("[%d/%d] %s", record_num, len(records), record.pdf_stem)

            try:
                results = run_one_record(record, stages, paths, status,
                                         resume=args.resume)
                any_failed = any(
                    r.get("error") and not r.get("detected")
                    for r in results.values()
                )
                if any_failed:
                    month_failed += 1
                    total_failed += 1
                else:
                    month_done   += 1
                    total_done   += 1
            except Exception as exc:
                log.error("Fatal on %s: %s", record.pdf_stem, exc, exc_info=True)
                for s in stages:
                    status.mark_failed(record.pdf_stem, s, str(exc))
                _append_failed(record, "pipeline", str(exc))
                month_failed += 1
                total_failed += 1

        log.info("Month done=%d  failed=%d  skipped=%d",
                 month_done, month_failed, month_skipped)

        status.force_save()
        _retry_failed(month_recs, stages, paths, status,
                      label=f"{collection}/{year}/{month}")

        year_group_records.extend(month_recs)
        prev_year_key = cur_year_key

    # Final year retry
    if year_group_records:
        _retry_failed(year_group_records, stages, paths, status,
                      label=f"{prev_year_key[0]}/{prev_year_key[1]}")

    # -- Final flush + summary -------------------------------------------------
    status.force_save()

    counts = status.counts()
    log.info("-" * 55)
    log.info("done=%d  failed=%d  skipped=%d", total_done, total_failed, total_skipped)
    for s in ALL_STAGES:
        c = counts.get(s, {})
        log.info("  %-10s done=%-5d failed=%-5d pending=%d",
                 s, c.get(DONE, 0), c.get(FAILED, 0), c.get("pending", 0))

    write_summary_csv(status, output_root / "summary.csv")
    write_latlong_csv(status, output_root / "latlong_records.csv")


def print_status(status_csv: Path):
    s = ProcessingStatus(status_csv)
    c = s.counts()
    print(f"\nStatus -- {status_csv}  ({len(s._rows)} records)\n")
    for stage in ALL_STAGES:
        sc = c.get(stage, {})
        print(f"  {stage:<10}  done={sc.get(DONE,0):<6} "
              f"failed={sc.get(FAILED,0):<6} pending={sc.get('pending',0)}")
    print()


# -- CLI -----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Oklahoma well records pipeline")
    ap.add_argument("--stage",     choices=list(ALL_STAGES))
    ap.add_argument("--resume",    action="store_true", default=True)
    ap.add_argument("--no-resume", dest="resume", action="store_false")
    ap.add_argument("--limit",     type=int)
    ap.add_argument("--flat",      type=Path, help="Flat PDF folder (testing)")
    ap.add_argument("--pdf",       type=Path, help="Single PDF file")
    ap.add_argument("--scan",      action="store_true",
                    help="Re-scan source ZIPs before processing")
    ap.add_argument("--source",    type=Path, default=SOURCE_ROOT,
                    help="Root folder containing ExportedFolderContents ZIPs")
    ap.add_argument("--index",     type=Path, default=DATASET_INDEX_CSV)
    ap.add_argument("--output",    type=Path, default=OUTPUT_ROOT)
    ap.add_argument("--status",    action="store_true")
    ap.add_argument("--verbose",   action="store_true")
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.status:
        print_status(Path(args.output) / "processing_status.csv")
        return

    run_pipeline(args)


if __name__ == "__main__":
    main()

"""
Oklahoma Well Records — Extraction Pipeline

Usage:
    python main.py                         # full run from dataset_index.csv
    python main.py --flat ..\pdfs          # flat folder (testing)
    python main.py --pdf ..\pdfs\file.pdf  # single file
    python main.py --stage grid            # one stage only
    python main.py --resume                # skip already-done records (default on)
    python main.py --limit 10              # first N records
    python main.py --status                # print progress and exit
    python main.py --scan                  # (re-)scan source ZIPs first, then run

Output mirrors input hierarchy:
    D:\\project_outputs\\
    ├-- processing_status.csv
    ├-- grids\\ExportedFolderContents_1\\{year}\\{month}\\{stem}\\
    ├-- locations\\...
    ├-- counties\\...
    ├-- metadata\\...\\metadata.json
    └-- logs\\...\\{stem}.log
"""

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    ALL_STAGES, DATASET_INDEX_CSV, FAILED_RECORDS_CSV,
    OUTPUT_ROOT, PROCESSING_STATUS_CSV,
    RESOLUTION_MULTIPLIER, SOURCE_ROOT,
    STAGE_COUNTY, STAGE_GRID, STAGE_LOCATION,
)
from pdf.pdf_manager import PDFDocumentManager
from scan_dataset import DatasetRecord, OutputPathBuilder, load_index, scan_flat_folder
from utils.logging_utils import get_logger, get_pdf_logger
from utils.processing_status import DONE, FAILED, ProcessingStatus
from utils.zip_reader import get_pdf_bytes

log = get_logger(__name__)


# -- PDF source resolution -----------------------------------------------------

def _make_manager(record: DatasetRecord) -> PDFDocumentManager:
    """
    Returns a PDFDocumentManager for the record.
    Reads from ZIP bytes if record.zip_path is set; otherwise uses file path.
    """
    if record.zip_path:
        pdf_bytes = get_pdf_bytes(record.zip_path, record.internal_path)
        return PDFDocumentManager(pdf_bytes=pdf_bytes,
                                  resolution_multiplier=RESOLUTION_MULTIPLIER)
    return PDFDocumentManager(record.pdf_path,
                              resolution_multiplier=RESOLUTION_MULTIPLIER)


# -- Metadata writer -----------------------------------------------------------

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

    # Load PDF once — shared across all stages
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
            pdf_log.info("[%s] already done — skip", stage.upper())
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
            detail = r.get("name") or f"page={r.get('page')}"
            status.mark_done(record.pdf_stem, stage,
                             r.get("confidence", 0), str(detail))

    if results:
        mp = write_metadata(record, results, paths)
        pdf_log.info("metadata -> %s", mp)

    pdf_log.info("=== END %s ===\n", record.pdf_stem)
    return results


def _dispatch(stage: str, manager: PDFDocumentManager,
              out_dir: Path, pdf_stem: str, log) -> dict:
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


# -- Pipeline runner -----------------------------------------------------------

def run_pipeline(args):
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)

    paths  = OutputPathBuilder(output_root)
    status = ProcessingStatus(output_root / "processing_status.csv")

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
                log.error("No records — run with --scan first or point --index at CSV")
                sys.exit(1)
        log.info("Loaded %d records", len(records))

    if args.limit:
        records = records[: args.limit]

    stages = (args.stage,) if args.stage else ALL_STAGES
    log.info("stages=%s  resume=%s  records=%d", stages, args.resume, len(records))

    # -- Init status -----------------------------------------------------------
    for r in records:
        status.init_record(r.pdf_stem, r.pdf_path,
                           r.collection, r.year, r.month)
    status.save()

    # -- Main loop -------------------------------------------------------------
    done = failed = skipped = 0

    for i, record in enumerate(records, 1):
        if args.resume and all(status.is_done(record.pdf_stem, s) for s in stages):
            skipped += 1
            continue

        log.info("[%d/%d] %s", i, len(records), record.pdf_stem)

        try:
            results = run_one_record(record, stages, paths, status,
                                     resume=args.resume)
            any_failed = any(
                r.get("error") and not r.get("detected")
                for r in results.values()
            )
            failed += int(any_failed)
            done   += int(not any_failed)
        except Exception as exc:
            log.error("Fatal on %s: %s", record.pdf_stem, exc, exc_info=True)
            for s in stages:
                status.mark_failed(record.pdf_stem, s, str(exc))
            _append_failed(record, "pipeline", str(exc))
            failed += 1

    # -- Summary ---------------------------------------------------------------
    counts = status.counts()
    log.info("-" * 55)
    log.info("done=%d  failed=%d  skipped=%d", done, failed, skipped)
    for s in ALL_STAGES:
        c = counts.get(s, {})
        log.info("  %-10s done=%-5d failed=%-5d pending=%d",
                 s, c.get(DONE, 0), c.get(FAILED, 0), c.get("pending", 0))


def print_status(status_csv: Path):
    s = ProcessingStatus(status_csv)
    c = s.counts()
    print(f"\nStatus — {status_csv}  ({len(s._rows)} records)\n")
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

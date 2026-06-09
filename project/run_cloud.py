"""
run_cloud.py -- Full 570K-record cloud-native pipeline orchestrator
====================================================================

Pre-flight checks → dataset scan → full pipeline run with S3 checkpointing,
stop/pause support, and per-collection progress reports.

Intended use cases:
  1. Local Windows run against all 13 ExportedFolderContents flat dirs
  2. AWS EC2/Batch run with S3 checkpoint (set S3_CHECKPOINT_PREFIX in .env)
  3. Staged collection-by-collection run (--collection N)

Stop / resume
-------------
The pipeline saves state after every monthly batch.  To stop gracefully:
    python run_cloud.py --stop --output D:\\project_outputs
Then resume later with:
    python run_cloud.py --output D:\\project_outputs

On crash / network loss / spot-kill: re-run the SAME command.  Already-done
records are skipped automatically via processing_status.csv.

Usage
-----
    # Full run (all collections, sequential, resumes from last state)
    python run_cloud.py --output D:\\project_outputs --workers 4

    # First-time run: scan all collection dirs then process
    python run_cloud.py --scan --output D:\\project_outputs --workers 4

    # Single collection (e.g. to test C13 modern forms)
    python run_cloud.py --collection 13 --output D:\\project_outputs --workers 2

    # Single stage across all pending records
    python run_cloud.py --stage grid --output D:\\project_outputs

    # Pre-flight checks only (credentials, disk, S3)
    python run_cloud.py --preflight --output D:\\project_outputs

    # Progress dashboard
    python run_cloud.py --status --output D:\\project_outputs

    # Graceful stop (after current month-batch)
    python run_cloud.py --stop --output D:\\project_outputs

    # Resume after a stop
    python run_cloud.py --unstop --output D:\\project_outputs

    # Export summary CSVs (success.csv, dot_locations.csv, etc.)
    python run_cloud.py --export --output D:\\project_outputs

S3 checkpoint
-------------
Set S3_CHECKPOINT_PREFIX in .env or as an environment variable:
    S3_CHECKPOINT_PREFIX=s3://osu-pipeline-results-mano/checkpoints/full_run

Every time processing_status.csv is saved it is also uploaded to S3.
On any subsequent run the checkpoint is verified (but local file is
authoritative — only downloaded if local file is missing).

Environment variables (see .env.example)
-----------------------------------------
  OUTPUT_ROOT          Pipeline output dir (default: D:\\project_outputs)
  SOURCE_ROOT          Parent dir of ExportedFolderContents (N) dirs (default: D:\\)
  S3_CHECKPOINT_PREFIX S3 prefix for live status CSV backup (optional)
  GOOGLE_APPLICATION_CREDENTIALS  GCP Vision API service account JSON
  GOOGLE_API_KEY       Gemini API key(s), comma-separated for rotation
  RDS_HOST / RDS_USER / RDS_PASSWORD  PostgreSQL PLSS database
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(_HERE))

_env_file = _HERE.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# API cost estimates (per 1,000 records) — updated from test100 analysis
# ---------------------------------------------------------------------------
_COST_PER_1K = {
    "vision_ocr":  2.768,    # $2.768 / 1K records (avg 1.6 Vision calls/record)
    "gemini_flash": 0.056,   # $0.056 / 1K records (1 Gemini call/record, flash-lite)
}
_TOTAL_RECORDS = 570_879    # approximate; actual count from dataset scan


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def _check_preflight(output_root: Path, verbose: bool = False) -> list[str]:
    """
    Run pre-flight checks.  Returns a list of warning strings (empty = all OK).
    Raises SystemExit on fatal errors.
    """
    warnings: list[str] = []

    # 1. Google Cloud Vision credentials
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not gac:
        warnings.append("GOOGLE_APPLICATION_CREDENTIALS not set -- Vision API will fail")
    elif not Path(gac).exists():
        warnings.append(f"GOOGLE_APPLICATION_CREDENTIALS points to missing file: {gac}")
    else:
        if verbose:
            print(f"  [ok] Vision credentials: {gac}", flush=True)

    # 2. Gemini API key
    gemini_key = os.environ.get("GOOGLE_API_KEY", "")
    if not gemini_key:
        warnings.append("GOOGLE_API_KEY not set -- county Gemini step will be skipped")
    else:
        n_keys = len([k for k in gemini_key.split(",") if k.strip()])
        if verbose:
            print(f"  [ok] Gemini API keys: {n_keys} key(s) configured", flush=True)

    # 3. Output dir writeable
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        test_file = output_root / ".preflight_write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        if verbose:
            print(f"  [ok] Output dir writable: {output_root}", flush=True)
    except Exception as exc:
        print(f"  [FATAL] Output dir not writable: {output_root}: {exc}", flush=True)
        sys.exit(1)

    # 4. S3 checkpoint accessible (if configured)
    s3_prefix = os.environ.get("S3_CHECKPOINT_PREFIX", "").rstrip("/")
    if s3_prefix:
        try:
            import boto3
            parts  = s3_prefix.split("/")
            bucket = parts[2]
            boto3.client("s3").head_bucket(Bucket=bucket)
            if verbose:
                print(f"  [ok] S3 checkpoint bucket accessible: {bucket}", flush=True)
        except Exception as exc:
            warnings.append(f"S3 checkpoint not accessible ({s3_prefix}): {exc}")

    # 5. Disk space (warn if < 10 GB on the output drive)
    try:
        import shutil
        free_gb = shutil.disk_usage(output_root).free / 1e9
        if free_gb < 10:
            warnings.append(f"Low disk space: {free_gb:.1f} GB free on output drive")
        elif verbose:
            print(f"  [ok] Disk space: {free_gb:.0f} GB free", flush=True)
    except Exception:
        pass

    # 6. Collection directories accessible
    source_root_str = os.environ.get("SOURCE_ROOT",
        r"D:" if sys.platform == "win32" else str(Path.home()))
    source_root = Path(source_root_str)
    found_cols: list[int] = []
    import re
    dir_pat = re.compile(r"^ExportedFolderContents\s*\((\d+)\)$", re.IGNORECASE)
    if source_root.is_dir():
        for entry in source_root.iterdir():
            if not entry.is_dir():
                continue
            m = dir_pat.match(entry.name)
            if m:
                found_cols.append(int(m.group(1)))
    if not found_cols:
        warnings.append(
            f"No ExportedFolderContents (N) dirs found under {source_root}. "
            "Set SOURCE_ROOT or pass --source."
        )
    elif verbose:
        print(f"  [ok] Found collections: {sorted(found_cols)}", flush=True)

    return warnings


# ---------------------------------------------------------------------------
# Status / progress display
# ---------------------------------------------------------------------------

import re as _re
_COL_NUM_RE = _re.compile(r"\((\d+)\)")


def _cnum_from_row(row: dict) -> str:
    """
    Extract collection number as a string for grouping.
    Tries collection_num first; falls back to parsing the collection field
    (handles old CSVs where collection_num was not yet populated).
    """
    n = (row.get("collection_num") or "").strip()
    if n and n.isdigit():
        return n
    col = (row.get("collection") or "").strip()
    m = _COL_NUM_RE.search(col)
    if m:
        return m.group(1)
    return "?"


def _print_status(status_csv: Path) -> None:
    """Print per-collection and per-stage progress table."""
    if not status_csv.exists():
        print("  No processing_status.csv found -- pipeline has not run yet.", flush=True)
        return

    stage_cols = ["latlong", "grid", "location", "county", "dot"]
    col_counts: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: {s: defaultdict(int) for s in stage_cols}
    )
    total_rows = 0

    with status_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total_rows += 1
            cnum = _cnum_from_row(row)
            for stage in stage_cols:
                st = row.get(f"{stage}_status", "pending") or "pending"
                col_counts[cnum][stage][st] += 1

    print(f"\n  Records total : {total_rows:>9,}", flush=True)

    # Per-collection grid summary (most diagnostic stage)
    print(f"\n  Per-collection grid progress:", flush=True)
    print(f"  {'Coll':<5} {'done':>7} {'failed':>7} {'pending':>8} {'done%':>6}", flush=True)
    print(f"  {'-'*37}", flush=True)
    grand_done = grand_failed = grand_pending = 0
    for cnum in sorted(col_counts, key=lambda x: (len(x), x)):
        d = col_counts[cnum]["grid"]
        done    = d.get("done",    0)
        failed  = d.get("failed",  0)
        pending = d.get("pending", 0) + d.get("skipped", 0)
        total   = done + failed + pending
        pct     = f"{done/total*100:.0f}%" if total else "  -"
        print(f"  C{cnum:<4} {done:>7,} {failed:>7,} {pending:>8,} {pct:>6}",
              flush=True)
        grand_done    += done
        grand_failed  += failed
        grand_pending += pending
    grand_total = grand_done + grand_failed + grand_pending
    grand_pct   = f"{grand_done/grand_total*100:.1f}%" if grand_total else "-"
    print(f"  {'TOTAL':<5} {grand_done:>7,} {grand_failed:>7,} {grand_pending:>8,} {grand_pct:>6}",
          flush=True)

    # Cross-stage summary
    print(f"\n  All-stage summary:", flush=True)
    print(f"  {'Stage':<10} {'done':>8} {'failed':>8} {'skipped':>8} {'pending':>8}",
          flush=True)
    print(f"  {'-'*48}", flush=True)
    for stage in stage_cols:
        d = f = sk = pe = 0
        for cnum_d in col_counts.values():
            sd = cnum_d[stage]
            d  += sd.get("done",    0)
            f  += sd.get("failed",  0)
            sk += sd.get("skipped", 0)
            pe += sd.get("pending", 0)
        print(f"  {stage:<10} {d:>8,} {f:>8,} {sk:>8,} {pe:>8,}", flush=True)

    # API cost estimate
    remaining = grand_pending
    if remaining > 0:
        vision_est = remaining * _COST_PER_1K["vision_ocr"]  / 1000
        gemini_est = remaining * _COST_PER_1K["gemini_flash"] / 1000
        print(f"\n  Remaining API cost estimate ({remaining:,} records):", flush=True)
        print(f"    Vision OCR  : ${vision_est:>8,.2f}", flush=True)
        print(f"    Gemini Flash: ${gemini_est:>8,.2f}", flush=True)
        print(f"    Total       : ${vision_est + gemini_est:>8,.2f}", flush=True)


# ---------------------------------------------------------------------------
# Build / verify dataset index
# ---------------------------------------------------------------------------

def _ensure_index(
    output_root: Path,
    source_root: Path,
    force_scan: bool,
) -> Path:
    """
    Return the path to dataset_index.csv.  Builds it if needed.

    Uses scan_collection_root() from scan_dataset.py — same logic as
    `python main.py --scan`.
    """
    from scan_dataset import scan_collection_root, write_index

    index_path = output_root / "dataset_index.csv"

    if index_path.exists() and not force_scan:
        try:
            with index_path.open(newline="", encoding="utf-8") as f:
                n = sum(1 for _ in csv.DictReader(f))
            print(f"  [index] Using existing index: {n:,} records ({index_path})",
                  flush=True)
            print("  [index] Pass --scan to rebuild.", flush=True)
        except Exception:
            pass
        return index_path

    print(f"  [scan] Scanning collection dirs under {source_root} ...", flush=True)
    t0 = time.time()
    records = scan_collection_root(source_root)
    elapsed = time.time() - t0

    if not records:
        print(f"  [FATAL] No records found under {source_root}. "
              "Check SOURCE_ROOT / --source.", flush=True)
        sys.exit(1)

    write_index(records, index_path)
    print(f"  [scan] {len(records):,} records indexed in {elapsed:.1f}s -> {index_path}",
          flush=True)
    return index_path


# ---------------------------------------------------------------------------
# Launch main.py
# ---------------------------------------------------------------------------

def _launch(
    index_path: Path,
    output_root: Path,
    workers: int,
    stage: str | None,
    collection: int | None,
    verbose: bool,
    export: bool,
) -> int:
    """Invoke main.py as a subprocess.  Returns exit code."""
    main_py = _HERE / "main.py"
    cmd = [
        sys.executable, str(main_py),
        "--index",   str(index_path),
        "--output",  str(output_root),
        "--workers", str(workers),
        # Resume by default: processing_status.csv gates already-done records
    ]
    if stage:
        cmd += ["--stage", stage]
    if collection is not None:
        cmd += ["--collection", str(collection)]
    if verbose:
        cmd.append("--verbose")
    if export:
        cmd.append("--export")

    print("\n" + "-" * 72, flush=True)
    print("  Launching pipeline:", flush=True)
    print("  " + " ".join(cmd), flush=True)
    print("-" * 72 + "\n", flush=True)

    t0  = time.time()
    ret = subprocess.call(cmd)
    elapsed = time.time() - t0
    h, rem = divmod(int(elapsed), 3600)
    m, s   = divmod(rem, 60)
    print(f"\n  Pipeline finished in {h:02d}:{m:02d}:{s:02d}  (exit code: {ret})",
          flush=True)
    return ret


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    _out_default = os.environ.get(
        "OUTPUT_ROOT",
        r"D:\project_outputs" if sys.platform == "win32" else "/tmp/project_outputs",
    )
    _src_default = os.environ.get(
        "SOURCE_ROOT",
        r"D:" if sys.platform == "win32" else str(Path.home()),
    )

    ap = argparse.ArgumentParser(
        description="Full 570K-record cloud-native pipeline orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--output", type=Path, default=Path(_out_default),
        help=f"Pipeline output dir (default: {_out_default})",
    )
    ap.add_argument(
        "--source", type=Path, default=Path(_src_default),
        help=f"Parent dir containing ExportedFolderContents (N) dirs "
             f"(default: {_src_default})",
    )
    ap.add_argument(
        "--scan", action="store_true",
        help="Re-scan all collection dirs and rebuild dataset_index.csv",
    )
    ap.add_argument(
        "--collection", type=int, default=None,
        help="Run only this collection number (1-13)",
    )
    ap.add_argument(
        "--stage", default=None,
        choices=["latlong", "grid", "location", "county", "dot"],
        help="Run only this stage (default: all stages)",
    )
    ap.add_argument(
        "--workers", type=int, default=1,
        help="Parallel pipeline workers (default: 1)",
    )
    ap.add_argument(
        "--preflight", action="store_true",
        help="Run pre-flight checks and exit",
    )
    ap.add_argument(
        "--status", action="store_true",
        help="Print progress dashboard and exit",
    )
    ap.add_argument(
        "--stop", action="store_true",
        help="Create STOP file -- pipeline exits after current month-batch",
    )
    ap.add_argument(
        "--unstop", action="store_true",
        help="Remove STOP/PAUSE file to allow re-run to continue",
    )
    ap.add_argument(
        "--export", action="store_true",
        help="After processing, write success.csv / dot_locations.csv / "
             "latlong_records.csv",
    )
    ap.add_argument(
        "--verbose", action="store_true",
        help="Pass --verbose to main.py (DEBUG output)",
    )
    return ap.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args   = _parse_args()
    out    = Path(args.output)
    source = Path(args.source)

    out.mkdir(parents=True, exist_ok=True)

    status_csv = out / "processing_status.csv"

    # ── Stop / unstop ────────────────────────────────────────────────────────
    if args.stop:
        stop_f = out / "STOP"
        stop_f.write_text("stop requested\n", encoding="utf-8")
        print(f"  STOP file created: {stop_f}", flush=True)
        print("  Pipeline will exit gracefully after the current monthly batch.",
              flush=True)
        return

    if args.unstop:
        removed = []
        for fname in ["STOP", "PAUSE"]:
            f = out / fname
            if f.exists():
                f.unlink()
                removed.append(fname)
        if removed:
            print(f"  Removed: {', '.join(removed)}", flush=True)
        else:
            print("  No STOP/PAUSE file found.", flush=True)
        print("  Re-run to continue processing.", flush=True)
        return

    # ── Status only ──────────────────────────────────────────────────────────
    if args.status:
        _print_status(status_csv)
        return

    # ── Pre-flight checks ────────────────────────────────────────────────────
    print("\n" + "=" * 72, flush=True)
    print("  run_cloud.py - Oklahoma Well Records Pipeline", flush=True)
    print("=" * 72, flush=True)
    print(f"  Output     : {out}", flush=True)
    print(f"  Source     : {source}", flush=True)
    print(f"  Collection : {args.collection or 'all'}", flush=True)
    print(f"  Stage      : {args.stage or 'all'}", flush=True)
    print(f"  Workers    : {args.workers}", flush=True)
    s3_prefix = os.environ.get("S3_CHECKPOINT_PREFIX", "")
    print(f"  S3 checkpoint: {s3_prefix or '(not configured)'}", flush=True)
    print()

    print("  Pre-flight checks...", flush=True)
    warnings = _check_preflight(out, verbose=args.verbose or args.preflight)

    if warnings:
        for w in warnings:
            print(f"  [WARN] {w}", flush=True)
    else:
        print("  [ok] All pre-flight checks passed.", flush=True)

    if args.preflight:
        if warnings:
            print(f"\n  {len(warnings)} warning(s) found. Review before running.", flush=True)
        else:
            print("\n  Ready to run.", flush=True)
        return

    print()

    # ── Build / verify dataset index ─────────────────────────────────────────
    index_path = _ensure_index(out, source, force_scan=args.scan)

    # ── Show current state before starting ───────────────────────────────────
    if status_csv.exists():
        print("\n  Current run state:", flush=True)
        _print_status(status_csv)
        print()

    # ── Launch pipeline ───────────────────────────────────────────────────────
    exit_code = _launch(
        index_path = index_path,
        output_root = out,
        workers    = args.workers,
        stage      = args.stage,
        collection = args.collection,
        verbose    = args.verbose,
        export     = args.export,
    )

    # ── Final progress summary ────────────────────────────────────────────────
    print("\n" + "=" * 72, flush=True)
    print("  Final progress summary:", flush=True)
    print("=" * 72, flush=True)
    _print_status(status_csv)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

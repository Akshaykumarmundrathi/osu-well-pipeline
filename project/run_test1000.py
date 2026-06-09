"""
run_test1000.py — 1,000-per-collection validation run
======================================================

Scans all 13 ExportedFolderContents (N) flat directories, builds a
stratified 13,000-record test index (up to 1,000 per collection), then
launches the full pipeline against that index.

Already-processed records are excluded from the sample automatically:
  - From the main run output dir (D:\\project_outputs by default)
  - From any previous test1000 run in the test output dir

On re-run after a crash, the script rebuilds the same sample (same seed)
but removes records already processed in the test1000 output dir.

Usage:
    # Full test run, 4 workers
    python run_test1000.py --workers 4

    # Build index only (no processing)
    python run_test1000.py --build-index

    # Single stage only
    python run_test1000.py --stage grid --workers 4

    # Custom sample size
    python run_test1000.py --per-collection 500

    # Custom output / source dirs
    python run_test1000.py \\
        --collection-base D:\\ \\
        --main-output D:\\project_outputs \\
        --output D:\\project_outputs_test1000

    # Check progress (no processing)
    python run_test1000.py --status

    # Graceful stop (creates STOP file; next monthly-batch will flush and exit)
    python run_test1000.py --stop

Output
------
    <output>/
        dataset_index.csv        test1000 index (≤13 000 records)
        processing_status.csv    per-record stage results (resumed on re-run)
        grids/ locations/ counties/ metadata/ logs/   (same layout as main run)
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running from repo root or project subdir
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent.resolve()   # D:\project_modular\project
sys.path.insert(0, str(_HERE))

_env_file = _HERE.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Defaults (all overridable via env vars or CLI flags)
# ---------------------------------------------------------------------------
_COLLECTION_BASE_DEFAULT = os.environ.get("COLLECTION_BASE", r"D:\\")
_MAIN_OUTPUT_DEFAULT     = os.environ.get("OUTPUT_ROOT",
    r"D:\project_outputs" if sys.platform == "win32" else "/tmp/project_outputs")
_TEST_OUTPUT_DEFAULT     = os.environ.get("TEST1000_OUTPUT",
    r"D:\project_outputs_test1000" if sys.platform == "win32" else "/tmp/test1000")

# Collections to sample.  Keys = collection number, values = expected PDF count.
# All 13 collections are included by default.
_ALL_COLLECTIONS = list(range(1, 14))

PER_COLLECTION_DEFAULT = 1000
RANDOM_SEED_DEFAULT    = 42


# ---------------------------------------------------------------------------
# Step 1 — collect already-processed stems
# ---------------------------------------------------------------------------

def _load_done_stems(status_csv: Path) -> set[str]:
    """
    Read a processing_status.csv and return all pdf_stem values whose
    status is not 'pending' for the GRID stage (the first real stage).
    Any record that has been touched (done / failed / skipped) is excluded
    from the new sample — we never re-process a record that already ran.
    """
    done: set[str] = set()
    if not status_csv.exists():
        return done
    try:
        with status_csv.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Skip if grid_status is non-empty and non-pending:
                # that means the record was at least attempted.
                grid_st = row.get("grid_status", "").strip()
                if grid_st and grid_st != "pending":
                    done.add(row.get("pdf_stem", "").strip())
                    continue
                # Also skip if any stage has a done/failed status
                for field in ("latlong_status", "location_status",
                              "county_status", "dot_status"):
                    st = row.get(field, "").strip()
                    if st and st != "pending":
                        done.add(row.get("pdf_stem", "").strip())
                        break
    except Exception as exc:
        print(f"  [warn] Could not read {status_csv}: {exc}", flush=True)
    return done


# ---------------------------------------------------------------------------
# Step 2 — scan flat collection directories
# ---------------------------------------------------------------------------

def _scan_collections(
    base: Path,
    collections: list[int],
) -> dict[int, list[Path]]:
    """
    Walk each ExportedFolderContents (N) directory under *base* and return
    a dict mapping collection_num → list of PDF paths.

    PDFs are sorted (year/month order) for deterministic sampling.
    """
    import re
    dir_pattern = re.compile(r"ExportedFolderContents\s*\((\d+)\)", re.IGNORECASE)
    found: dict[int, Path] = {}

    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        m = dir_pattern.match(entry.name)
        if not m:
            continue
        n = int(m.group(1))
        if n in collections:
            found[n] = entry

    result: dict[int, list[Path]] = {}
    for n in collections:
        col_dir = found.get(n)
        if col_dir is None:
            print(f"  [warn] Collection {n} not found under {base} — skipping", flush=True)
            continue
        pdfs = sorted(col_dir.rglob("*.pdf"))
        result[n] = pdfs
        print(f"  C{n:2d}: found {len(pdfs):>7,} PDFs in {col_dir.name}", flush=True)

    return result


# ---------------------------------------------------------------------------
# Step 3 — build stratified sample index
# ---------------------------------------------------------------------------

def _build_sample(
    collection_pdfs: dict[int, list[Path]],
    done_stems: set[str],
    per_collection: int,
    seed: int,
) -> list[dict]:
    """
    For each collection, exclude done stems, then take up to *per_collection*
    records at random (reproducible via *seed*).

    Returns a list of row dicts matching the DatasetRecord / dataset_index.csv
    schema.
    """
    import re
    from datetime import datetime, timezone

    dir_pattern = re.compile(r"ExportedFolderContents\s*\((\d+)\)", re.IGNORECASE)
    rng     = random.Random(seed)
    rows: list[dict] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    for n, pdfs in sorted(collection_pdfs.items()):
        # Filter already-done
        candidates = [p for p in pdfs if p.stem not in done_stems]
        # Sample
        sample = rng.sample(candidates, min(per_collection, len(candidates)))
        sample.sort()  # keep deterministic order after sampling

        col_name = f"ExportedFolderContents ({n})"
        col_safe = f"ExportedFolderContents_{n}"

        for pdf in sample:
            # year_dir / month_dir / stem.pdf
            parts = pdf.parts
            # pdf.parent = .../ExportedFolderContents (N)/YEAR/MONTH-name
            month_dir = pdf.parent
            year_dir  = month_dir.parent

            year_str  = year_dir.name   # e.g. "1911"
            month_str = month_dir.name  # e.g. "01 - January"
            # safe version: collapse spaces/dashes
            month_safe = re.sub(r"[^a-zA-Z0-9_]", "_", month_str).strip("_")

            rows.append({
                "pdf_stem":        pdf.stem,
                "pdf_path":        str(pdf),
                "zip_path":        "",          # flat layout — no ZIP
                "collection":      col_name,
                "collection_num":  str(n),
                "year":            year_str,
                "month":           month_str,
                "collection_safe": col_safe,
                "month_safe":      month_safe,
                "file_size_bytes": str(pdf.stat().st_size),
                "scan_timestamp":  ts,
            })

        excluded = len(pdfs) - len(candidates)
        print(f"  C{n:2d}: {len(sample):>4} sampled  "
              f"({excluded:>5} excluded as already-done, "
              f"{len(pdfs) - excluded - len(sample):>5} remaining unsampled)",
              flush=True)

    return rows


# ---------------------------------------------------------------------------
# Step 4 — write index CSV
# ---------------------------------------------------------------------------

_INDEX_FIELDS = [
    "pdf_stem", "pdf_path", "zip_path",
    "collection", "collection_num", "year", "month",
    "collection_safe", "month_safe",
    "file_size_bytes", "scan_timestamp",
]


def _write_index(rows: list[dict], index_path: Path) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_INDEX_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\n  Index written: {index_path}  ({len(rows):,} records)", flush=True)


# ---------------------------------------------------------------------------
# Step 5 — launch main.py
# ---------------------------------------------------------------------------

def _launch_pipeline(
    index_path: Path,
    output_dir: Path,
    workers: int,
    stage: str | None,
    verbose: bool,
) -> int:
    """
    Invoke main.py as a subprocess.
    Returns the process exit code.
    """
    main_py = _HERE / "main.py"
    cmd = [
        sys.executable, str(main_py),
        "--index",   str(index_path),
        "--output",  str(output_dir),
        "--workers", str(workers),
        # Resume by default: skip records already done in this output dir
    ]
    if stage:
        cmd += ["--stage", stage]
    if verbose:
        cmd.append("--verbose")

    print("\n" + "-" * 72, flush=True)
    print("  Launching pipeline:", flush=True)
    print("  " + " ".join(cmd), flush=True)
    print("-" * 72, flush=True)

    start = time.time()
    ret   = subprocess.call(cmd)
    elapsed = time.time() - start
    h, rem = divmod(int(elapsed), 3600)
    m, s   = divmod(rem, 60)
    print(f"\n  Pipeline finished in {h:02d}:{m:02d}:{s:02d}  (exit {ret})", flush=True)
    return ret


# ---------------------------------------------------------------------------
# Progress / status display
# ---------------------------------------------------------------------------

def _print_status(status_csv: Path) -> None:
    """Quick progress summary from processing_status.csv."""
    if not status_csv.exists():
        print("  No processing_status.csv found -- run has not started yet.", flush=True)
        return

    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total = 0
    with status_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total += 1
            for stage in ("latlong", "grid", "location", "county", "dot"):
                st = row.get(f"{stage}_status", "pending") or "pending"
                counts[stage][st] += 1

    print(f"\n  Records in status CSV: {total:,}", flush=True)
    print(f"  {'Stage':<10} {'done':>8} {'failed':>8} {'skipped':>8} {'pending':>8}", flush=True)
    print(f"  {'-'*46}", flush=True)
    for stage in ("latlong", "grid", "location", "county", "dot"):
        c = counts[stage]
        print(f"  {stage:<10} {c.get('done',0):>8,} {c.get('failed',0):>8,} "
              f"{c.get('skipped',0):>8,} {c.get('pending',0):>8,}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    ap = argparse.ArgumentParser(
        description="1,000-per-collection validation run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--collection-base", type=Path,
        default=Path(_COLLECTION_BASE_DEFAULT),
        help="Parent dir containing ExportedFolderContents (N) folders "
             f"(default: {_COLLECTION_BASE_DEFAULT})",
    )
    ap.add_argument(
        "--main-output", type=Path,
        default=Path(_MAIN_OUTPUT_DEFAULT),
        help="Main pipeline output dir — already-processed stems are "
             f"excluded from sample (default: {_MAIN_OUTPUT_DEFAULT})",
    )
    ap.add_argument(
        "--output", type=Path,
        default=Path(_TEST_OUTPUT_DEFAULT),
        help=f"Test1000 output dir (default: {_TEST_OUTPUT_DEFAULT})",
    )
    ap.add_argument(
        "--per-collection", type=int, default=PER_COLLECTION_DEFAULT,
        metavar="N",
        help=f"Records to sample per collection (default: {PER_COLLECTION_DEFAULT})",
    )
    ap.add_argument(
        "--collections", type=int, nargs="+", default=_ALL_COLLECTIONS,
        metavar="N",
        help="Collection numbers to include (default: 1-13)",
    )
    ap.add_argument(
        "--seed", type=int, default=RANDOM_SEED_DEFAULT,
        help=f"Random seed for reproducibility (default: {RANDOM_SEED_DEFAULT})",
    )
    ap.add_argument(
        "--workers", type=int, default=1,
        help="Parallel pipeline workers (default: 1)",
    )
    ap.add_argument(
        "--stage", default=None,
        choices=["latlong", "grid", "location", "county", "dot"],
        help="Run only this stage (default: all stages)",
    )
    ap.add_argument(
        "--build-index", action="store_true",
        help="Build the index CSV and exit without running the pipeline",
    )
    ap.add_argument(
        "--rebuild-index", action="store_true",
        help="Force re-sample even if index already exists",
    )
    ap.add_argument(
        "--status", action="store_true",
        help="Print progress summary and exit",
    )
    ap.add_argument(
        "--stop", action="store_true",
        help="Create STOP file in test output dir to request graceful shutdown",
    )
    ap.add_argument(
        "--unstop", action="store_true",
        help="Remove STOP/PAUSE file so the next run continues",
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
    args = _parse_args()

    out_dir    = Path(args.output)
    index_path = out_dir / "dataset_index.csv"
    status_csv = out_dir / "processing_status.csv"

    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Stop / unstop ────────────────────────────────────────────────────────
    if args.stop:
        stop_f = out_dir / "STOP"
        stop_f.write_text("stop requested\n", encoding="utf-8")
        print(f"  STOP file created: {stop_f}", flush=True)
        print("  Pipeline will exit gracefully after the current batch.", flush=True)
        return

    if args.unstop:
        for f in [out_dir / "STOP", out_dir / "PAUSE"]:
            if f.exists():
                f.unlink()
                print(f"  Removed: {f}", flush=True)
        print("  Re-run to continue processing.", flush=True)
        return

    # ── Status only ──────────────────────────────────────────────────────────
    if args.status:
        _print_status(status_csv)
        return

    # ── Print run header ─────────────────────────────────────────────────────
    print("\n" + "=" * 72, flush=True)
    print("  run_test1000.py - 1,000-per-collection validation run", flush=True)
    print("=" * 72, flush=True)
    print(f"  Collections   : {args.collections}", flush=True)
    print(f"  Per-collection: {args.per_collection}", flush=True)
    print(f"  Seed          : {args.seed}", flush=True)
    print(f"  Workers       : {args.workers}", flush=True)
    print(f"  Stage         : {args.stage or 'all'}", flush=True)
    print(f"  Output dir    : {out_dir}", flush=True)
    print(f"  Main output   : {args.main_output}", flush=True)
    print(f"  Collection base: {args.collection_base}", flush=True)
    print()

    # ── Build index (or reuse existing) ──────────────────────────────────────
    if index_path.exists() and not args.rebuild_index:
        # Count rows in existing index
        try:
            with index_path.open(newline="", encoding="utf-8") as f:
                n_existing = sum(1 for _ in csv.DictReader(f))
            print(f"  Using existing index: {index_path}  ({n_existing:,} records)", flush=True)
            print(f"  Pass --rebuild-index to re-sample.\n", flush=True)
        except Exception:
            pass
    else:
        print("  Step 1: Loading already-done stems...", flush=True)

        done_stems: set[str] = set()

        # From the main pipeline run (e.g. D:\project_outputs)
        main_csv = Path(args.main_output) / "processing_status.csv"
        main_done = _load_done_stems(main_csv)
        if main_done:
            print(f"    {len(main_done):,} stems done in main output ({main_csv.name})",
                  flush=True)
        done_stems.update(main_done)

        # From any previous test1000 run in this output dir
        if status_csv.exists():
            test_done = _load_done_stems(status_csv)
            new_test  = test_done - main_done
            if new_test:
                print(f"    {len(new_test):,} additional stems done in test1000 output",
                      flush=True)
            done_stems.update(test_done)

        print(f"    {len(done_stems):,} total stems to exclude from sample\n", flush=True)

        print("  Step 2: Scanning collection directories...", flush=True)
        collection_pdfs = _scan_collections(
            Path(args.collection_base),
            args.collections,
        )

        if not collection_pdfs:
            print("  ERROR: No collection directories found. "
                  f"Check --collection-base ({args.collection_base})", flush=True)
            sys.exit(1)

        print(f"\n  Step 3: Building stratified sample...", flush=True)
        rows = _build_sample(
            collection_pdfs,
            done_stems,
            args.per_collection,
            args.seed,
        )

        if not rows:
            print("\n  All records in all collections have already been processed!", flush=True)
            print("  Nothing to do. Remove --rebuild-index is not needed.", flush=True)
            sys.exit(0)

        total_sampled = len(rows)
        _write_index(rows, index_path)

        # Per-collection summary
        coll_counts: dict[str, int] = defaultdict(int)
        for r in rows:
            coll_counts[r["collection_num"]] += 1
        print("\n  Sample breakdown:", flush=True)
        for n in sorted(args.collections):
            cnt = coll_counts.get(str(n), 0)
            bar = "#" * (cnt // 50) if cnt else ""
            print(f"    C{n:2d}: {cnt:>5,} records  {bar}", flush=True)
        print(f"\n  Total: {total_sampled:,} records across "
              f"{len(coll_counts)} collections", flush=True)

    if args.build_index:
        print("\n  --build-index flag set: index built, no processing.", flush=True)
        return

    # ── Launch pipeline ───────────────────────────────────────────────────────
    exit_code = _launch_pipeline(
        index_path = index_path,
        output_dir = out_dir,
        workers    = args.workers,
        stage      = args.stage,
        verbose    = args.verbose,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

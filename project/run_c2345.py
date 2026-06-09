"""
run_c2345.py — 100-per-year single-pass run for Collections 2, 3, 4, 5
=======================================================================

Builds a stratified sample (100 records per year per collection), runs
all pipeline stages in a single pass, logs every failure for future review,
and automatically publishes resolved records to the GitHub Pages map.

Design decisions
----------------
* **Per-year** stratification (not per-collection) so every year is
  represented on the map even when collection sizes are uneven.
* **Single pass, no retry** — failed stages are flagged in processing_status.csv
  and written to failed_records.csv for a future robust reprocessing run.
* **Skip already-done** — reads D:\\project_outputs\\processing_status.csv so
  records processed in any previous run are excluded from the sample.
* **Output to main dir** — results go to D:\\project_outputs (same as the
  main full run) so the map accumulates C1 + C2–C5 in one place.
* **Auto map push** — after the pipeline finishes, runs coord enrichment
  and pushes well_locations.json to GitHub Pages automatically.

Usage
-----
    # Build index only (see totals, dry run)
    python run_c2345.py --build-index

    # Full run — 2 workers (recommended: 2 on local, 4 on cloud)
    python run_c2345.py --workers 2

    # Resume after interruption (same command re-run)
    python run_c2345.py --workers 2

    # Progress check
    python run_c2345.py --status

    # Rebuild sample (re-randomise, e.g. after adding more records)
    python run_c2345.py --rebuild-index --workers 2

    # Push map without re-running the pipeline
    python run_c2345.py --push-map

    # Graceful stop / resume
    python run_c2345.py --stop
    python run_c2345.py --unstop

Output
------
    Index + progress tracking:  D:\\project_outputs_c2345\\
        dataset_index.csv        sampled records (≤3700)
        processing_status.csv    per-record stage results (resumed on re-run)
        failed_records.csv       all failed records (for future reprocessing)

    Pipeline output (shared with main run):  D:\\project_outputs\\
        grids/ locations/ counties/ metadata/ logs/
        processing_status.csv   (master tracker — C1 + C2-C5 combined)
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap — load .env, ensure project/ is on sys.path
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
# Configuration
# ---------------------------------------------------------------------------
# Collections this script handles.
TARGET_COLLECTIONS = [2, 3, 4, 5]

# Records to sample per year per collection.
PER_YEAR = 100

# Random seed — fixed for reproducibility.
RANDOM_SEED = 43   # different from run_test1000 seed (42) so samples differ

# Where the source PDFs live.
_BASE = Path(os.environ.get("COLLECTION_BASE",
             os.environ.get("SOURCE_ROOT", r"D:\\")))

# Where MAIN pipeline output lives (C1 + this run share this dir).
_MAIN_OUT = Path(os.environ.get("OUTPUT_ROOT",
    r"D:\project_outputs" if sys.platform == "win32" else "/tmp/project_outputs"))

# Separate index + tracking dir for this run (keeps C2-C5 progress independent).
_C2345_OUT = Path(os.environ.get("C2345_OUTPUT",
    r"D:\project_outputs_c2345" if sys.platform == "win32"
    else "/tmp/project_outputs_c2345"))

# Repo root (for build_map_data.py and docs/).
_REPO = Path(os.environ.get("REPO_ROOT", str(_HERE.parent)))

# ---------------------------------------------------------------------------
# Index field schema (mirrors scan_dataset.py / run_test1000.py)
# ---------------------------------------------------------------------------
_INDEX_FIELDS = [
    "pdf_stem", "pdf_path", "zip_path",
    "collection", "collection_num", "year", "month",
    "collection_safe", "month_safe",
    "file_size_bytes", "scan_timestamp",
]

# Failed-records CSV schema.
_FAILED_FIELDS = [
    "pdf_stem", "collection_num", "year", "month",
    "failed_stages",   # comma-sep list of failed stage names
    "error_types",     # comma-sep list of error_type strings
    "flagged_at",
]


# ---------------------------------------------------------------------------
# Step 1 — collect already-processed stems (from MAIN output)
# ---------------------------------------------------------------------------

def _load_done_stems(status_csv: Path) -> set[str]:
    """
    Return stems that have been touched (done / failed / skipped) for
    the GRID stage OR any other stage.  These are excluded from the
    new sample — we never re-process a record that already ran.
    """
    done: set[str] = set()
    if not status_csv.exists():
        return done
    try:
        with status_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                grid_st = row.get("grid_status", "").strip()
                if grid_st and grid_st != "pending":
                    done.add(row.get("pdf_stem", "").strip())
                    continue
                for field in ("latlong_status", "location_status",
                              "county_status", "dot_status"):
                    if row.get(field, "").strip() not in ("", "pending"):
                        done.add(row.get("pdf_stem", "").strip())
                        break
    except Exception as exc:
        print(f"  [warn] Could not read {status_csv}: {exc}", flush=True)
    return done


# ---------------------------------------------------------------------------
# Step 2 — scan flat collection directories, group by year
# ---------------------------------------------------------------------------

def _scan_by_year(
    base: Path,
    collections: list[int],
) -> dict[tuple[int, str], list[Path]]:
    """
    Scan ExportedFolderContents (N) directories and return a dict keyed by
    (collection_num, year_str) → sorted list of PDF paths.
    """
    dir_pattern = re.compile(r"ExportedFolderContents\s*\((\d+)\)\s*$", re.IGNORECASE)
    result: dict[tuple[int, str], list[Path]] = {}

    found_dirs: dict[int, Path] = {}
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        m = dir_pattern.match(entry.name)
        if not m:
            continue
        n = int(m.group(1))
        if n in collections:
            found_dirs[n] = entry

    for n in collections:
        col_dir = found_dirs.get(n)
        if col_dir is None:
            print(f"  [warn] C{n} not found under {base} — skipping", flush=True)
            continue

        # Walk year subdirs: ExportedFolderContents (N) / YEAR / MONTH / *.pdf
        year_total = 0
        for year_dir in sorted(col_dir.iterdir()):
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            pdfs = sorted(year_dir.rglob("*.pdf"))
            if not pdfs:
                continue
            result[(n, year_dir.name)] = pdfs
            year_total += len(pdfs)

        col_keys  = [(n, yr) for (cn, yr) in result if cn == n]
        n_years   = len(col_keys)
        n_pdfs    = sum(len(result[k]) for k in col_keys)
        print(f"  C{n}: {n_pdfs:>7,} PDFs across {n_years:3d} years  "
              f"({min(yr for _, yr in col_keys) if col_keys else '?'}"
              f"–{max(yr for _, yr in col_keys) if col_keys else '?'})",
              flush=True)

    return result


# ---------------------------------------------------------------------------
# Step 3 — build stratified sample (100/year/collection)
# ---------------------------------------------------------------------------

def _build_sample(
    by_year: dict[tuple[int, str], list[Path]],
    done_stems: set[str],
    per_year: int,
    seed: int,
) -> list[dict]:
    """
    For each (collection, year) bucket, exclude already-done stems, then
    sample up to `per_year` records at random (reproducible via `seed`).

    Returns a list of row dicts matching dataset_index.csv schema.
    """
    rng = random.Random(seed)
    rows: list[dict] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    total_excluded = 0
    total_available = 0

    for (n, year_str), pdfs in sorted(by_year.items()):
        candidates = [p for p in pdfs if p.stem not in done_stems]
        excluded = len(pdfs) - len(candidates)
        total_excluded  += excluded
        total_available += len(candidates)

        sample = rng.sample(candidates, min(per_year, len(candidates)))
        sample.sort()

        col_name = f"ExportedFolderContents ({n})"
        col_safe = f"ExportedFolderContents_{n}"

        for pdf in sample:
            month_dir  = pdf.parent
            month_str  = month_dir.name
            month_safe = re.sub(r"[^a-zA-Z0-9_]", "_", month_str).strip("_")

            rows.append({
                "pdf_stem":        pdf.stem,
                "pdf_path":        str(pdf),
                "zip_path":        "",
                "collection":      col_name,
                "collection_num":  str(n),
                "year":            year_str,
                "month":           month_str,
                "collection_safe": col_safe,
                "month_safe":      month_safe,
                "file_size_bytes": str(pdf.stat().st_size),
                "scan_timestamp":  ts,
            })

    # Summary header
    col_yr_counts: dict[int, dict[str, int]] = defaultdict(dict)
    for r in rows:
        cn = int(r["collection_num"])
        yr = r["year"]
        col_yr_counts[cn][yr] = col_yr_counts[cn].get(yr, 0) + 1

    print(f"\n  Sample breakdown (100/year/collection):", flush=True)
    for n in sorted(set(cn for cn, _ in by_year.keys())):
        yrs   = col_yr_counts.get(n, {})
        total = sum(yrs.values())
        yr_rng = f"{min(yrs) if yrs else '?'}-{max(yrs) if yrs else '?'}"
        print(f"    C{n:2d}: {total:>5,} records  ({len(yrs)} years  {yr_rng})",
              flush=True)

    print(f"\n  {len(rows):,} total records  "
          f"({total_excluded:,} excluded as already-done, "
          f"{total_available - len(rows):,} remaining unsampled)",
          flush=True)
    return rows


# ---------------------------------------------------------------------------
# Step 4 — write index CSV
# ---------------------------------------------------------------------------

def _write_index(rows: list[dict], index_path: Path) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_INDEX_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\n  Index written: {index_path}  ({len(rows):,} records)", flush=True)


# ---------------------------------------------------------------------------
# Step 5 — launch main.py pipeline
# ---------------------------------------------------------------------------

def _launch_pipeline(
    index_path: Path,
    output_dir: Path,
    workers: int,
    stage: str | None,
) -> int:
    """
    Invoke main.py as a subprocess writing to the MAIN output dir.
    Returns exit code (0 = clean, non-zero = pipeline error / interrupt).

    Note on skip-failed:
      main.py defaults to resume=True, skip_failed=True when re-run on an
      existing processing_status.csv.  Failed stages are NOT retried —
      they stay in processing_status.csv as 'failed' and are written to
      failed_records.csv at the end by _export_failed_records().
    """
    main_py = _HERE / "main.py"
    cmd = [
        sys.executable, str(main_py),
        "--index",   str(index_path),
        "--output",  str(output_dir),
        "--workers", str(workers),
        # resume=True (default): skip records already done in this output dir
        # skip_failed=True (default on resume): don't re-run failed stages
    ]
    if stage:
        cmd += ["--stage", stage]

    print("\n" + "-" * 72, flush=True)
    print("  Launching pipeline:", flush=True)
    print("  " + " ".join(cmd), flush=True)
    print("-" * 72, flush=True)

    start   = time.time()
    ret     = subprocess.call(cmd)
    elapsed = time.time() - start
    h, rem  = divmod(int(elapsed), 3600)
    m, s    = divmod(rem, 60)
    print(f"\n  Pipeline finished in {h:02d}:{m:02d}:{s:02d}  (exit {ret})",
          flush=True)
    return ret


# ---------------------------------------------------------------------------
# Step 6 — export failed records CSV
# ---------------------------------------------------------------------------

def _export_failed_records(
    index_path: Path,
    main_status_csv: Path,
    failed_csv: Path,
) -> int:
    """
    Cross-reference the index against processing_status.csv to find every
    record where at least one stage has status='failed'. Write a tidy
    failed_records.csv for future reprocessing.

    Returns number of failed records written.
    """
    # Build lookup: stem → row from processing_status
    status_rows: dict[str, dict] = {}
    if main_status_csv.exists():
        with main_status_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                status_rows[row.get("pdf_stem", "").strip()] = row

    # Load index to get collection/year/month for each stem
    index_rows: dict[str, dict] = {}
    if index_path.exists():
        with index_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                index_rows[row.get("pdf_stem", "").strip()] = row

    failed: list[dict] = []
    stages = ("latlong", "grid", "location", "county", "dot")

    for stem, idx_row in index_rows.items():
        sr = status_rows.get(stem)
        if sr is None:
            continue
        failed_stages = [s for s in stages if sr.get(f"{s}_status") == "failed"]
        if not failed_stages:
            continue
        error_types = [sr.get(f"{s}_error_type", "") or "" for s in failed_stages]
        failed.append({
            "pdf_stem":       stem,
            "collection_num": idx_row.get("collection_num", ""),
            "year":           idx_row.get("year", ""),
            "month":          idx_row.get("month", ""),
            "failed_stages":  ",".join(failed_stages),
            "error_types":    ",".join(error_types),
            "flagged_at":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        })

    failed.sort(key=lambda r: (r["collection_num"], r["year"], r["month"],
                               r["pdf_stem"]))
    failed_csv.parent.mkdir(parents=True, exist_ok=True)
    with failed_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_FAILED_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(failed)

    if failed:
        print(f"\n  Failed records: {len(failed)} written to {failed_csv}", flush=True)
        # Breakdown by stage
        stage_counts: dict[str, int] = defaultdict(int)
        for r in failed:
            for s in r["failed_stages"].split(","):
                stage_counts[s.strip()] += 1
        print("  By stage:", flush=True)
        for s, c in sorted(stage_counts.items(), key=lambda x: -x[1]):
            print(f"    {s:<12} {c:>5}", flush=True)
    else:
        print(f"\n  No failed records — clean run.", flush=True)

    return len(failed)


# ---------------------------------------------------------------------------
# Step 7 — coord enrichment + GitHub Pages map push
# ---------------------------------------------------------------------------

def _push_map(output_dir: Path, repo: Path) -> None:
    """
    Run run_coord_enrichment.py against `output_dir`, then call
    build_map_data.py --push to publish to GitHub Pages.
    """
    print("\n" + "=" * 72, flush=True)
    print("  Publishing results to GitHub Pages map...", flush=True)
    print("=" * 72, flush=True)

    enricher = _HERE / "run_coord_enrichment.py"
    builder  = _HERE / "build_map_data.py"

    # Step 7a — coord enrichment (PLSS RDS lookup)
    print("\n  Step 7a: Coordinate enrichment (RDS query)...", flush=True)
    r1 = subprocess.run(
        [sys.executable, str(enricher), "--output", str(output_dir)],
        cwd=str(_HERE),
    )
    if r1.returncode != 0:
        print("  [warn] Enrichment returned non-zero exit code — "
              "map may be incomplete.", flush=True)

    # Step 7b — build GeoJSON + push
    print("\n  Step 7b: Building GeoJSON and pushing to GitHub Pages...",
          flush=True)
    r2 = subprocess.run(
        [sys.executable, str(builder),
         "--output", str(output_dir),
         "--repo",   str(repo),
         "--push"],
        cwd=str(_HERE),
    )
    if r2.returncode != 0:
        print("  [warn] Map build/push returned non-zero exit code.", flush=True)
    else:
        print("\n  Map published OK.", flush=True)
        print("  https://akshaykumarmundrathi.github.io/osu-well-pipeline/",
              flush=True)


# ---------------------------------------------------------------------------
# Progress status
# ---------------------------------------------------------------------------

def _print_status(index_path: Path, main_status_csv: Path) -> None:
    if not index_path.exists():
        print("  No index found — run with --build-index first.", flush=True)
        return

    # Load index stems
    with index_path.open(newline="", encoding="utf-8") as f:
        index_stems = {row["pdf_stem"] for row in csv.DictReader(f)}

    print(f"\n  Index: {len(index_stems):,} records", flush=True)

    if not main_status_csv.exists():
        print("  processing_status.csv not found — run not started yet.", flush=True)
        return

    # Separate "in status CSV" from "not yet started".
    # A record that is in the CSV but has an empty / None / "pending" status value
    # counts as "not yet started" the same as a record that's not in the CSV at all.
    # This prevents the confusing display where pending=0 even though 3600+ records
    # haven't been touched yet.
    _ACTIVE = {"done", "failed", "skipped"}
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    n_in_status = 0
    started_stems: set[str] = set()
    with main_status_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            stem = row.get("pdf_stem", "")
            if stem not in index_stems:
                continue
            n_in_status += 1
            any_started = False
            for stage in ("latlong", "grid", "location", "county", "dot"):
                st = (row.get(f"{stage}_status") or "").strip()
                if st in _ACTIVE:
                    counts[stage][st] += 1
                    any_started = True
            if any_started:
                started_stems.add(stem)

    n_started   = len(started_stems)
    n_not_start = len(index_stems) - n_started
    print(f"  Processed: {n_started:,} / {len(index_stems):,}  "
          f"({n_not_start:,} not yet started)", flush=True)
    print(f"\n  {'Stage':<10} {'done':>8} {'failed':>8} "
          f"{'skipped':>8} {'pending':>8}", flush=True)
    print(f"  {'-'*46}", flush=True)
    for stage in ("latlong", "grid", "location", "county", "dot"):
        c = counts[stage]
        stage_done    = c.get("done", 0)
        stage_failed  = c.get("failed", 0)
        stage_skipped = c.get("skipped", 0)
        stage_total   = stage_done + stage_failed + stage_skipped
        stage_pending = len(index_stems) - stage_total
        print(f"  {stage:<10} {stage_done:>8,} {stage_failed:>8,} "
              f"{stage_skipped:>8,} {stage_pending:>8,}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    ap = argparse.ArgumentParser(
        description="100-per-year single-pass run for Collections 2-5",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--collection-base", type=Path, default=_BASE,
        help=f"Parent dir containing ExportedFolderContents (N) folders "
             f"(default: {_BASE})",
    )
    ap.add_argument(
        "--output", type=Path, default=_MAIN_OUT,
        help=f"Main pipeline output dir (C1 + C2-C5 combined, default: {_MAIN_OUT})",
    )
    ap.add_argument(
        "--index-dir", type=Path, default=_C2345_OUT,
        help=f"Dir for this run's index + failed_records.csv "
             f"(default: {_C2345_OUT})",
    )
    ap.add_argument(
        "--repo", type=Path, default=_REPO,
        help=f"Path to the osu-well-pipeline git repo (default: {_REPO})",
    )
    ap.add_argument(
        "--per-year", type=int, default=PER_YEAR,
        help=f"Records per year per collection (default: {PER_YEAR})",
    )
    ap.add_argument(
        "--collections", type=int, nargs="+", default=TARGET_COLLECTIONS,
        help=f"Collections to include (default: {TARGET_COLLECTIONS})",
    )
    ap.add_argument(
        "--seed", type=int, default=RANDOM_SEED,
        help=f"Random seed (default: {RANDOM_SEED})",
    )
    ap.add_argument(
        "--workers", type=int, default=2,
        help="Parallel workers for the pipeline (default: 2)",
    )
    ap.add_argument(
        "--stage", default=None,
        choices=["latlong", "grid", "location", "county", "dot"],
        help="Run only this stage (default: all)",
    )
    ap.add_argument(
        "--build-index", action="store_true",
        help="Build the sample index and exit without running the pipeline",
    )
    ap.add_argument(
        "--rebuild-index", action="store_true",
        help="Force re-sampling even if an index already exists",
    )
    ap.add_argument(
        "--no-push", action="store_true",
        help="Skip the GitHub Pages push after the pipeline finishes",
    )
    ap.add_argument(
        "--push-map", action="store_true",
        help="Only run enrichment + push map (no pipeline processing)",
    )
    ap.add_argument(
        "--status", action="store_true",
        help="Print progress summary and exit",
    )
    ap.add_argument(
        "--stop", action="store_true",
        help="Create STOP file in output dir for graceful shutdown",
    )
    ap.add_argument(
        "--unstop", action="store_true",
        help="Remove STOP/PAUSE file so the next run continues",
    )
    return ap.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = _parse_args()

    out_dir    = Path(args.output)
    index_dir  = Path(args.index_dir)
    index_path = index_dir / "dataset_index.csv"
    failed_csv = index_dir / "failed_records.csv"
    status_csv = out_dir   / "processing_status.csv"

    out_dir.mkdir(parents=True, exist_ok=True)
    index_dir.mkdir(parents=True, exist_ok=True)

    # ── Stop / unstop ────────────────────────────────────────────────────────
    if args.stop:
        stop_f = out_dir / "STOP"
        stop_f.write_text("stop requested by run_c2345\n", encoding="utf-8")
        print(f"  STOP file created: {stop_f}", flush=True)
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
        _print_status(index_path, status_csv)
        return

    # ── Push map only ────────────────────────────────────────────────────────
    if args.push_map:
        _push_map(out_dir, Path(args.repo))
        return

    # ── Print run header ─────────────────────────────────────────────────────
    print("\n" + "=" * 72, flush=True)
    print("  run_c2345.py — 100-per-year run for Collections 2, 3, 4, 5", flush=True)
    print("=" * 72, flush=True)
    print(f"  Collections  : {args.collections}", flush=True)
    print(f"  Per year     : {args.per_year}", flush=True)
    print(f"  Seed         : {args.seed}", flush=True)
    print(f"  Workers      : {args.workers}", flush=True)
    print(f"  Stage        : {args.stage or 'all'}", flush=True)
    print(f"  Pipeline out : {out_dir}", flush=True)
    print(f"  Index dir    : {index_dir}", flush=True)
    print(f"  Push map     : {'no (--no-push)' if args.no_push else 'yes (auto after pipeline)'}", flush=True)
    print()

    # ── Build index ───────────────────────────────────────────────────────────
    if not index_path.exists() or args.rebuild_index:
        print("  Step 1: Loading already-done stems from main output...", flush=True)
        done_stems = _load_done_stems(status_csv)
        print(f"    {len(done_stems):,} stems already processed — will be excluded",
              flush=True)

        print("\n  Step 2: Scanning collection directories...", flush=True)
        by_year = _scan_by_year(Path(args.collection_base), args.collections)
        if not by_year:
            print("  ERROR: No collection directories found.", flush=True)
            sys.exit(1)

        print("\n  Step 3: Building per-year sample...", flush=True)
        rows = _build_sample(by_year, done_stems, args.per_year, args.seed)
        if not rows:
            print("\n  All sampled records already processed — nothing to do.",
                  flush=True)
            sys.exit(0)

        _write_index(rows, index_path)
    else:
        with index_path.open(newline="", encoding="utf-8") as f:
            n_idx = sum(1 for _ in csv.DictReader(f))
        print(f"  Using existing index: {index_path}  ({n_idx:,} records)",
              flush=True)
        print(f"  Pass --rebuild-index to re-sample.\n", flush=True)

    if args.build_index:
        print("\n  --build-index: index ready, no processing.", flush=True)
        return

    # ── Launch pipeline ───────────────────────────────────────────────────────
    # main.py uses resume=True + skip_failed=True by default:
    #   - already-done stages are skipped
    #   - previously-failed stages are NOT retried (flagged for future review)
    ret = _launch_pipeline(
        index_path = index_path,
        output_dir = out_dir,
        workers    = args.workers,
        stage      = args.stage,
    )

    # ── Export failed records ─────────────────────────────────────────────────
    print("\n  Exporting failed records log...", flush=True)
    n_failed = _export_failed_records(index_path, status_csv, failed_csv)

    # ── Export success / dot CSV summaries ───────────────────────────────────
    print("\n  Exporting summary CSVs (success.csv / dot_locations.csv)...",
          flush=True)
    subprocess.run(
        [sys.executable, str(_HERE / "main.py"),
         "--export", "--output", str(out_dir)],
        cwd=str(_HERE),
    )

    # ── Auto map push ─────────────────────────────────────────────────────────
    if not args.no_push:
        _push_map(out_dir, Path(args.repo))
    else:
        print("\n  --no-push set: skipping map publish.", flush=True)
        print(f"  To publish manually:", flush=True)
        print(f"    python run_c2345.py --push-map", flush=True)

    # Summary
    print("\n" + "=" * 72, flush=True)
    print(f"  run_c2345.py complete  (pipeline exit code: {ret})", flush=True)
    if n_failed:
        print(f"  {n_failed} failed records logged -> {failed_csv}", flush=True)
        print(f"  These will be retried in a future robust-pipeline pass.", flush=True)
    print("=" * 72, flush=True)

    sys.exit(ret)


if __name__ == "__main__":
    main()

"""
run_test100.py — Fully automated end-to-end test: 100 files per collection

Phases (all unattended, no human input required):
  1.  Connection check  — RDS, Vision API, Gemini, git
  2.  Dataset index     — scan all 13 flat collection directories on D:\
  3.  Sample            — 100 random records per collection (seed=42)
  4.  Pipeline          — all 5 stages on 1300 records (workers=4)
  5.  Coord enrichment  — RDS PLSS lookup → lat/lon
  6.  Map build         — GeoJSON + GitHub Pages push

Usage:
    python run_test100.py                       # full run
    python run_test100.py --check-only          # connections only, no processing
    python run_test100.py --from-phase 4        # resume from a specific phase
    python run_test100.py --workers 2           # override worker count
    python run_test100.py --no-push             # skip GitHub push
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 output on Windows so print() doesn't crash on checkmark chars
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Bootstrap — load .env so all credentials are available
# ---------------------------------------------------------------------------
_HERE      = Path(__file__).parent
_ENV_PATH  = _HERE / ".env"

if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, _, v = _line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

# Always use a dedicated test directory — NEVER inherit production OUTPUT_ROOT.
# Override with TEST100_OUTPUT_ROOT env var for CI environments.
OUTPUT_ROOT   = Path(os.environ.get("TEST100_OUTPUT_ROOT", str(_HERE / "project_outputs_test100")))
PROJECT_DIR   = _HERE / "project"
SOURCE_ROOT   = Path(os.environ.get("SOURCE_ROOT", "D:/"))
SEED          = 42
SAMPLE_N      = 100

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def _header(msg: str):
    bar = "=" * 64
    print(f"\n{bar}")
    print(f"  [{_ts()}]  {msg}")
    print(bar)

def _ok(msg: str):
    print(f"  [{_ts()}]  OK   {msg}")

def _fail(msg: str):
    print(f"  [{_ts()}]  FAIL {msg}")

def _info(msg: str):
    print(f"  [{_ts()}]     {msg}")

def _run(cmd: list[str], cwd=None, env=None) -> int:
    """Run a subprocess, streaming stdout/stderr in real time. Returns exit code."""
    _info(f"Running: {' '.join(str(c) for c in cmd)}")
    proc = subprocess.run(
        cmd, cwd=str(cwd or PROJECT_DIR),
        env={**os.environ, **(env or {})},
    )
    return proc.returncode


# ---------------------------------------------------------------------------
# Phase 1 — Connection checks
# ---------------------------------------------------------------------------

def phase_check() -> bool:
    _header("Phase 1 — Connection checks")
    all_ok = True

    # --- credentials present -------------------------------------------------
    for var in ("RDS_HOST", "RDS_DBNAME", "RDS_USER", "RDS_PASSWORD"):
        if os.environ.get(var):
            _ok(f"{var} set")
        else:
            _fail(f"{var} MISSING — add to .env")
            all_ok = False

    gcp = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if gcp and Path(gcp).exists():
        _ok(f"GCP JSON exists: {Path(gcp).name}")
    elif not gcp:
        # auto-detect from credentials/
        cands = sorted((_HERE / "credentials").glob("*.json")) if (_HERE / "credentials").is_dir() else []
        if cands:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(cands[0])
            _ok(f"GCP JSON auto-detected: {cands[0].name}")
        else:
            _fail("GOOGLE_APPLICATION_CREDENTIALS not set and no credentials/*.json found")
            all_ok = False
    else:
        _fail(f"GCP JSON path set but file not found: {gcp}")
        all_ok = False

    if os.environ.get("GOOGLE_API_KEY"):
        _ok("GOOGLE_API_KEY set")
    else:
        _fail("GOOGLE_API_KEY MISSING — Gemini county extraction will be skipped")
        # not fatal — pipeline continues without Gemini

    # --- flat collection directories -----------------------------------------
    # PDFs are unzipped into ExportedFolderContents (N)\ flat directories.
    flat_dirs = sorted(SOURCE_ROOT.glob("ExportedFolderContents (*)"))
    flat_dirs = [d for d in flat_dirs if d.is_dir()]
    if len(flat_dirs) == 13:
        _ok(f"All 13 flat collection directories found on {SOURCE_ROOT}")
    elif len(flat_dirs) > 0:
        _ok(f"{len(flat_dirs)}/13 flat collection directories found (proceeding)")
    else:
        _fail(f"No 'ExportedFolderContents (N)' directories found under {SOURCE_ROOT}")
        all_ok = False

    # --- RDS live connection -------------------------------------------------
    _info("Testing RDS connection...")
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.environ.get("RDS_HOST", ""),
            port=int(os.environ.get("RDS_PORT", "5432")),
            dbname=os.environ.get("RDS_DBNAME", ""),
            user=os.environ.get("RDS_USER", ""),
            password=os.environ.get("RDS_PASSWORD", ""),
            sslmode="require", connect_timeout=10,
        )
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM plss_grid")
        n = cur.fetchone()[0]
        conn.close()
        _ok(f"RDS connected — plss_grid has {n:,} rows")
    except Exception as exc:
        _fail(f"RDS connection FAILED: {exc}")
        all_ok = False

    # --- Vision API live ping ------------------------------------------------
    _info("Testing Google Cloud Vision API...")
    try:
        from google.cloud import vision
        client = vision.ImageAnnotatorClient()
        # Smallest valid JPEG (1×1 white pixel)
        import base64
        tiny_jpg = base64.b64decode(
            "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkS"
            "Ew8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJ"
            "CQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
            "MjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf/"
            "EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAA"
            "AAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/ABUAAP/Z"
        )
        image  = vision.Image(content=tiny_jpg)
        result = client.text_detection(image=image)
        _ok("Vision API responding")
    except Exception as exc:
        _fail(f"Vision API FAILED: {exc}")
        all_ok = False

    # --- Gemini ping ---------------------------------------------------------
    _info("Testing Gemini API...")
    try:
        import google.generativeai as genai
        key_raw = os.environ.get("GOOGLE_API_KEY", "")
        # Support comma-separated keys (rotation list); use first key
        key = key_raw.split(",")[0].strip()
        genai.configure(api_key=key)
        model_name = os.environ.get("GEMINI_FLASH_MODEL", "gemini-2.5-flash-lite")
        model  = genai.GenerativeModel(model_name)
        resp   = model.generate_content("Reply with exactly the word: OK")
        if "ok" in (resp.text or "").lower():
            _ok(f"Gemini API responding ({model_name})")
        else:
            _ok(f"Gemini API responding (response: {resp.text[:40]!r})")
    except Exception as exc:
        _fail(f"Gemini API FAILED: {exc}  (county Gemini pass will be skipped)")
        # not fatal

    # --- git remote ----------------------------------------------------------
    _info("Checking git remote...")
    try:
        import subprocess
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=str(_HERE),
        )
        remote = r.stdout.strip()
        if "github.com" in remote:
            _ok(f"git remote: {remote}")
        else:
            _fail(f"Unexpected git remote: {remote}")
            all_ok = False
    except Exception as exc:
        _fail(f"git check failed: {exc}")
        all_ok = False

    # --- OpenCV --------------------------------------------------------------
    try:
        import cv2
        _ok(f"OpenCV {cv2.__version__}")
    except ImportError:
        _fail("OpenCV not installed — pip install opencv-python-headless")
        all_ok = False

    # --- PyMuPDF -------------------------------------------------------------
    try:
        import fitz
        _ok(f"PyMuPDF (fitz) {fitz.__version__}")
    except ImportError:
        _fail("PyMuPDF not installed — pip install pymupdf")
        all_ok = False

    print()
    if all_ok:
        _ok("ALL CONNECTIONS OK — ready to run")
    else:
        _fail("One or more checks failed — fix above before proceeding")
    return all_ok


# ---------------------------------------------------------------------------
# Phase 2 — Build dataset index
# ---------------------------------------------------------------------------

def phase_scan() -> Path:
    _header("Phase 2 — Build dataset_index.csv (flat collection directories)")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    index_path = OUTPUT_ROOT / "dataset_index.csv"

    # Reuse an existing production index if it's fresh (< 48 h old) and
    # has the expected record count (> 500K), to avoid an expensive 2-hour
    # re-scan of 576K flat files on every test run.
    prod_index = Path(os.environ.get("OUTPUT_ROOT", str(_HERE / "project_outputs"))) / "dataset_index.csv"
    _use_existing = False
    for candidate in (index_path, prod_index):
        if candidate.exists():
            import stat as _stat
            age_h = (time.time() - candidate.stat().st_mtime) / 3600
            n_existing = sum(1 for _ in open(str(candidate))) - 1
            if age_h < 48 and n_existing > 500_000:
                if candidate != index_path:
                    import shutil
                    shutil.copy2(str(candidate), str(index_path))
                    _ok(f"Reused fresh production index ({n_existing:,} records, {age_h:.0f}h old) -> {index_path}")
                else:
                    _ok(f"Reused existing test index ({n_existing:,} records, {age_h:.0f}h old)")
                _use_existing = True
                break

    if not _use_existing:
        _info("No fresh index found — scanning flat collection directories (this takes ~2 h)...")
        rc = _run(
            [sys.executable, "scan_dataset.py",
             "--source", str(SOURCE_ROOT),
             "--output", str(index_path)],
            cwd=PROJECT_DIR,
        )
        if rc != 0:
            raise RuntimeError(f"scan_dataset.py exited with code {rc}")

    n = sum(1 for _ in open(str(index_path))) - 1
    _ok(f"Dataset index ready: {n:,} records -> {index_path}")
    return index_path


# ---------------------------------------------------------------------------
# Phase 3 — Sample 100 per collection
# ---------------------------------------------------------------------------

def phase_sample(index_path: Path) -> Path:
    _header(f"Phase 3 — Sample {SAMPLE_N} records per collection (seed={SEED})")
    out_path = OUTPUT_ROOT / "dataset_index_test100.csv"

    random.seed(SEED)
    by_coll: dict[str, list] = {}
    with index_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            c = row.get("collection_num", "0") or "0"
            by_coll.setdefault(c, []).append(row)

    sample: list[dict] = []
    for cnum in sorted(by_coll.keys(), key=lambda x: int(x)):
        rlist  = by_coll[cnum]
        chosen = random.sample(rlist, min(SAMPLE_N, len(rlist)))
        sample.extend(chosen)
        _info(f"  Collection {cnum:>2}: {len(rlist):>7,} total  →  {len(chosen):>3} sampled")

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(sample)

    _ok(f"{len(sample)} records written -> {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Phase 4 — Run extraction pipeline
# ---------------------------------------------------------------------------

def phase_pipeline(index_path: Path, workers: int):
    _header(f"Phase 4 — Extraction pipeline  ({workers} workers)")
    _info(f"Output root: {OUTPUT_ROOT}")
    _info(f"Index:       {index_path}")

    t0 = time.monotonic()
    rc = _run(
        [sys.executable, "main.py",
         "--index",   str(index_path),
         "--output",  str(OUTPUT_ROOT),
         "--workers", str(workers),
         "--resume",
         "--no-retry"],
        cwd=PROJECT_DIR,
    )
    elapsed = time.monotonic() - t0
    if rc != 0:
        raise RuntimeError(f"main.py exited with code {rc}")

    _ok(f"Pipeline complete in {elapsed/60:.1f} min")

    # Print quick stats
    status_csv = OUTPUT_ROOT / "processing_status.csv"
    if status_csv.exists():
        rows = list(csv.DictReader(open(str(status_csv))))
        print()
        _info(f"{'Stage':<12}  {'done':>6}  {'failed':>7}  {'skipped':>8}")
        _info("-" * 40)
        for s in ("latlong", "grid", "location", "county", "dot"):
            done   = sum(1 for r in rows if r.get(f"{s}_status") == "done")
            failed = sum(1 for r in rows if r.get(f"{s}_status") == "failed")
            skip   = sum(1 for r in rows if r.get(f"{s}_status") == "skipped")
            _info(f"  {s:<10}  {done:>6}  {failed:>7}  {skip:>8}")


# ---------------------------------------------------------------------------
# Phase 5 — Coordinate enrichment
# ---------------------------------------------------------------------------

def phase_enrich():
    _header("Phase 5 — Coordinate enrichment (dot → lat/lon via PLSS RDS)")

    t0 = time.monotonic()
    rc = _run(
        [sys.executable, "run_coord_enrichment.py",
         "--output",       str(OUTPUT_ROOT),
         "--all-dot-done",
         "--include-centroid"],
        cwd=PROJECT_DIR,
    )
    elapsed = time.monotonic() - t0
    if rc != 0:
        raise RuntimeError(f"run_coord_enrichment.py exited with code {rc}")
    _ok(f"Enrichment complete in {elapsed:.0f}s")

    # Quick resolution rate
    dot_csv = OUTPUT_ROOT / "dot_coordinates.csv"
    if dot_csv.exists():
        rows = list(csv.DictReader(open(str(dot_csv))))
        resolved = [r for r in rows if r.get("resolved_lat") not in ("", None)]
        _info(f"  Total records: {len(rows):,}  |  Resolved: {len(resolved):,}  "
              f"({len(resolved)/max(1,len(rows)):.1%})")


# ---------------------------------------------------------------------------
# Phase 6 — Build map + GitHub push
# ---------------------------------------------------------------------------

def phase_mapbuild(push: bool):
    _header(f"Phase 6 — Map build {'+ GitHub push' if push else '(no push)'}")

    cmd = [
        sys.executable, "build_map_data.py",
        "--output", str(OUTPUT_ROOT),
        "--repo",   str(_HERE),
    ]
    if push:
        cmd.append("--push")

    rc = _run(cmd, cwd=PROJECT_DIR)
    if rc != 0:
        raise RuntimeError(f"build_map_data.py exited with code {rc}")

    _ok("Map build complete")
    if push:
        _ok("Pushed to GitHub Pages — check https://akshaykumarmundrathi.github.io/osu-well-pipeline/ in ~60s")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Automated test-100 end-to-end run")
    parser.add_argument("--check-only",  action="store_true", help="Connection checks only")
    parser.add_argument("--from-phase",  type=int, default=1, metavar="N",
                        help="Start from phase N (1=check, 2=scan, 3=sample, 4=pipeline, 5=enrich, 6=mapbuild)")
    parser.add_argument("--workers",     type=int, default=4)
    parser.add_argument("--no-push",     action="store_true", help="Skip GitHub push in phase 6")
    args = parser.parse_args()

    start = time.monotonic()
    print(f"\n{'='*64}")
    print(f"  OSU Well Records — Automated Test Run  (100/collection)")
    print(f"  Output: {OUTPUT_ROOT}")
    print(f"  {'='*60}")

    try:
        # Phase 1 — always run
        ok = phase_check()
        if not ok:
            print("\n  Fix the errors above, then re-run.\n")
            sys.exit(1)
        if args.check_only:
            return

        # Phase 2
        if args.from_phase <= 2:
            index_path = phase_scan()
        else:
            index_path = OUTPUT_ROOT / "dataset_index.csv"
            if not index_path.exists():
                raise FileNotFoundError(f"No index at {index_path} — run from phase 2 first")

        # Phase 3
        if args.from_phase <= 3:
            sample_path = phase_sample(index_path)
        else:
            sample_path = OUTPUT_ROOT / "dataset_index_test100.csv"
            if not sample_path.exists():
                raise FileNotFoundError(f"No sample at {sample_path} — run from phase 3 first")

        # Phase 4
        if args.from_phase <= 4:
            phase_pipeline(sample_path, args.workers)

        # Phase 5
        if args.from_phase <= 5:
            phase_enrich()

        # Phase 6
        if args.from_phase <= 6:
            phase_mapbuild(push=not args.no_push)

    except KeyboardInterrupt:
        print("\n\n  Interrupted — re-run with --from-phase 4 (or whichever phase) to resume\n")
        sys.exit(130)
    except Exception as exc:
        print(f"\n  FATAL: {exc}\n")
        import traceback; traceback.print_exc()
        sys.exit(1)

    total = time.monotonic() - start
    print(f"\n{'='*64}")
    print(f"  ALL PHASES COMPLETE  ({total/60:.1f} min total)")
    print(f"{'='*64}\n")


if __name__ == "__main__":
    main()

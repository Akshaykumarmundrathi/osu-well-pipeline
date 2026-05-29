"""
Smoke test -- per-stage inspection against 100 representative records.

Picks 20 local PDFs from each of the 5 tier-representative collections
(1=early, 7=transition, 9=mid, 11=late, 13=modern), writes a mini index,
then runs each stage in isolation with --no-resume and produces a tight
inspection table + aggregate stats.

Usage (from project dir):
    python smoke_test.py                        # all stages
    python smoke_test.py --stage grid           # one stage
    python smoke_test.py --rebuild-index        # re-sample 100 records
    python smoke_test.py --workers 2            # parallel within each stage
"""

import argparse
import csv
import os
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# -- Config --------------------------------------------------------------------
# Override with environment variables so this script works on any machine or
# inside a CI container.  On the local Windows workstation the D:\ defaults
# remain valid as long as the env vars are not set.

SMOKE_ROOT = Path(os.environ.get(
    "SMOKE_ROOT",
    str(Path(__file__).parent.parent / "smoke_test_output"),
))
INDEX_PATH = SMOKE_ROOT / "smoke_index.csv"

# COLLECTION_BASE points at the *parent* of the ExportedFolderContents dirs.
# Set COLLECTION_BASE=D:\ on Windows or /mnt/data on Linux/WSL.
_CB = Path(os.environ.get("COLLECTION_BASE", r"D:\\"))
COLLECTION_ROOTS = {
    1:  _CB / "ExportedFolderContents (1)",
    7:  _CB / "ExportedFolderContents (7)",
    9:  _CB / "ExportedFolderContents (9)",
    11: _CB / "ExportedFolderContents (11)",
    13: _CB / "ExportedFolderContents (13)",
}
# Collections whose root dirs are absent are skipped gracefully at index-build time.

PER_COLLECTION = 20   # 5 tiers x 20 = 100 total
RANDOM_SEED    = 42

ALL_STAGES = ["latlong", "grid", "location", "county", "dot"]
STAGE_LABELS = {
    "latlong":  "Lat / Lon",
    "grid":     "Grid",
    "location": "Location",
    "county":   "County",
    "dot":      "Dot",
}
TIER_OF = {1: "early", 7: "transition", 9: "mid", 11: "late", 13: "modern"}

# -- Index builder -------------------------------------------------------------

def _collection_num(cnum: int) -> int:
    return cnum


def build_index(force: bool = False) -> Path:
    """Scan local folders, pick PER_COLLECTION PDFs per collection, write CSV."""
    if INDEX_PATH.exists() and not force:
        print(f"  [index] Reusing existing {INDEX_PATH}  (use --rebuild-index to refresh)")
        return INDEX_PATH

    SMOKE_ROOT.mkdir(parents=True, exist_ok=True)
    random.seed(RANDOM_SEED)

    rows = []
    for cnum, cdir in COLLECTION_ROOTS.items():
        pdfs = sorted(cdir.rglob("*.pdf"))
        if not pdfs:
            print(f"  [WARN] No PDFs found under {cdir}")
            continue
        chosen = random.sample(pdfs, min(PER_COLLECTION, len(pdfs)))
        for pdf in chosen:
            stem = pdf.stem
            # Path segments: <root>/<year>/<month>/<stem>.pdf
            parts = pdf.relative_to(cdir).parts
            year  = parts[0] if len(parts) > 2 else ""
            month = parts[1] if len(parts) > 2 else ""
            rows.append({
                "pdf_stem":        stem,
                "pdf_path":        str(pdf),
                "collection":      f"ExportedFolderContents_{cnum}",
                "collection_num":  cnum,
                "year":            year,
                "month":           month,
                "collection_safe": f"ExportedFolderContents_{cnum}",
                "month_safe":      month.replace(" ", "_").replace("-", "_"),
                "file_size_bytes": pdf.stat().st_size,
                "scan_timestamp":  "",
                "zip_path":        "",
                "internal_path":   "",
            })
        print(f"  [index] Collection {cnum} ({TIER_OF[cnum]:<12}): "
              f"{len(chosen)} selected from {len(pdfs):,} local PDFs")

    FIELDS = [
        "pdf_stem", "pdf_path", "collection", "collection_num",
        "year", "month", "collection_safe", "month_safe",
        "file_size_bytes", "scan_timestamp", "zip_path", "internal_path",
    ]
    with INDEX_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"  [index] Wrote {len(rows)} records -> {INDEX_PATH}\n")
    return INDEX_PATH


# -- Stage runner --------------------------------------------------------------

def run_stage(stage: str, workers: int = 1) -> Path:
    """
    Run main.py for one stage against the smoke index.
    Output goes to SMOKE_ROOT/<stage>/.
    Returns the path to processing_status.csv for that stage.

    Special case: dot stage needs the grid stage's output directory so it
    can find grid images and read grid_status=done from the status CSV.
    We run it with --resume against the grid output dir.
    """
    if stage == "dot":
        out_dir = SMOKE_ROOT / "grid"
        resume_flag = "--resume"
    else:
        out_dir = SMOKE_ROOT / stage
        resume_flag = "--no-resume"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "main.py",
        "--index",     str(INDEX_PATH),
        "--stage",     stage,
        resume_flag,
        "--output",    str(out_dir),
        "--workers",   str(workers),
    ]

    label = STAGE_LABELS.get(stage, stage)
    bar   = "-" * 68
    print(f"\n{bar}")
    print(f"  SMOKE TEST: {label}   (100 records, no-resume)")
    print(f"{bar}")

    # Force Cloud Vision API on the smoke test — Tesseract is not installed
    # on all local machines and county/location rely on OCR annotations.
    env = os.environ.copy()
    env["USE_VISION_API"] = "1"

    t0 = time.monotonic()
    result = subprocess.run(
        cmd,
        cwd=str(Path(__file__).parent),
        capture_output=False,   # let stdout stream live to console
        env=env,
    )
    elapsed = time.monotonic() - t0

    if result.returncode not in (0, 130):
        print(f"\n  [WARN] Stage {stage!r} exited with code {result.returncode}")

    print(f"\n  Stage complete in {elapsed:.0f}s")
    return out_dir / "processing_status.csv"


# -- Result reader + inspector --------------------------------------------------

def _int(v) -> int:
    try:
        return int(float(v)) if v not in ("", None) else 0
    except Exception:
        return 0


def inspect_stage(stage: str, status_csv: Path):
    """Read processing_status.csv and print a per-record table + aggregate stats."""
    if not status_csv.exists():
        print(f"  [WARN] {status_csv} not found -- stage may have crashed\n")
        return

    with status_csv.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    label    = STAGE_LABELS.get(stage, stage)
    col_key  = f"{stage}_status"
    conf_key = f"{stage}_confidence"
    err_key  = f"{stage}_error_type"

    done_rows    = [r for r in rows if r.get(col_key) == "done"]
    failed_rows  = [r for r in rows if r.get(col_key) == "failed"]
    skipped_rows = [r for r in rows if r.get(col_key) == "skipped"]
    total        = len(rows)

    # -- Per-stage key-field extraction ----------------------------------------
    def _key_field(r: dict) -> str:
        if stage == "latlong":
            lat, lon = r.get("latlong_lat", ""), r.get("latlong_lon", "")
            wt = r.get("latlong_well_type", "")
            return f"({lat}, {lon})  [{wt}]" if lat else ""
        if stage == "grid":
            return r.get("grid_method", "").replace("extract_grid_region_", "")
        if stage == "location":
            sec = r.get("location_section", "?")
            twp = r.get("location_township", "?")
            rng = r.get("location_range", "?")
            return f"sec={sec}  twp={twp}  rng={rng}"
        if stage == "county":
            score = r.get("county_score", "")
            name  = r.get("county_name", "")
            meth  = r.get("county_method", "")
            return f"{name}  [{score}%]  via {meth}" if name else ""
        if stage == "dot":
            row_ = r.get("dot_row", "")
            col_ = r.get("dot_col", "")
            nw   = r.get("dot_nw", "")
            return f"row={row_}  col={col_}  nw={nw}" if row_ else ""
        return ""

    # -- Print per-record inspection table -------------------------------------
    W_STEM  = 40
    W_TIER  = 12
    W_CONF  = 6
    W_FIELD = 36

    HDR = (f"  {'PDF stem':<{W_STEM}}  {'Tier':<{W_TIER}}  "
           f"{'St':<4}  {'Conf':>{W_CONF}}  {'Key field / error':<{W_FIELD}}")
    DIV = "  " + "-" * (W_STEM + W_TIER + W_CONF + W_FIELD + 16)

    print(f"\n  -- Per-record: {label} --")
    print(HDR)
    print(DIV)

    # Group by tier so we can see per-tier patterns.
    tier_order = ["early", "transition", "mid", "late", "modern"]
    tier_of_row = {}
    for r in rows:
        cnum = _int(r.get("collection_num", 0))
        if   1 <= cnum <= 6:  t = "early"
        elif 7 <= cnum <= 8:  t = "transition"
        elif 9 <= cnum <= 10: t = "mid"
        elif 11 <= cnum <= 12:t = "late"
        else:                  t = "modern"
        tier_of_row[r["pdf_stem"]] = t

    prev_tier = None
    for r in sorted(rows, key=lambda r: (tier_of_row.get(r["pdf_stem"], ""),
                                          r.get("pdf_stem", ""))):
        tier   = tier_of_row.get(r["pdf_stem"], "?")
        status = r.get(col_key, "?")
        conf   = _int(r.get(conf_key, 0))
        stem   = r.get("pdf_stem", "")[:W_STEM]
        kf     = _key_field(r)
        err    = r.get(err_key, "")

        if tier != prev_tier:
            print(f"\n  -- tier: {tier} --")
            prev_tier = tier

        if status == "done":
            st_tag = "OK"
            detail = kf[:W_FIELD]
        elif status == "skipped":
            st_tag = "-"
            conf   = 0
            detail = "(skipped)"
        else:
            st_tag = "FAIL"
            conf   = 0
            detail = (err or "failed")[:W_FIELD]

        conf_s = f"{conf}%" if status == "done" else ""
        print(f"  {stem:<{W_STEM}}  {tier:<{W_TIER}}  "
              f"{st_tag:<4}  {conf_s:>{W_CONF}}  {detail:<{W_FIELD}}")

    print(DIV)

    # -- Aggregate stats -------------------------------------------------------
    det_rate = 100 * len(done_rows) / total if total else 0
    confs    = [_int(r.get(conf_key, 0)) for r in done_rows]
    avg_conf = int(sum(confs) / len(confs)) if confs else 0
    low_conf = sum(1 for c in confs if c < 80)

    # Per-tier detection
    tier_done    = Counter()
    tier_total   = Counter()
    tier_skipped = Counter()
    for r in rows:
        t = tier_of_row.get(r["pdf_stem"], "?")
        tier_total[t]   += 1
        if r.get(col_key) == "done":    tier_done[t]    += 1
        if r.get(col_key) == "skipped": tier_skipped[t] += 1

    # Error breakdown
    errs = Counter(r.get(err_key, "other") or "other"
                   for r in failed_rows)

    # Review queue count for this stage (dot shares the grid output dir)
    rq_base = "grid" if stage == "dot" else stage
    rq_path = (SMOKE_ROOT / rq_base / "manual_review" / "review_queue.csv")
    rq_count = 0
    if rq_path.exists():
        with rq_path.open(newline="", encoding="utf-8") as f:
            rq_count = sum(1 for row in csv.DictReader(f)
                           if row.get("stage") == stage)

    print(f"\n  -- Aggregate: {label} --")
    print(f"  Total records : {total}")
    print(f"  Detected      : {len(done_rows)}  ({det_rate:.0f}%)")
    print(f"  Skipped       : {len(skipped_rows)}")
    print(f"  Failed        : {len(failed_rows)}")
    print(f"  Avg confidence: {avg_conf}%   (low<80: {low_conf})")
    print(f"  Review flagged: {rq_count}")

    print(f"\n  Detection by tier:")
    for t in tier_order:
        if tier_total[t] == 0:
            continue
        pct  = 100 * tier_done[t] / tier_total[t]
        skip = f"  (skip={tier_skipped[t]})" if tier_skipped[t] else ""
        print(f"    {t:<12}  {tier_done[t]:>2}/{tier_total[t]:>2}  ({pct:.0f}%){skip}")

    if errs:
        print(f"\n  Error breakdown:")
        for e, n in errs.most_common():
            print(f"    {n:>3}  {e}")

    if stage == "county":
        # Show score distribution
        scores = [_int(r.get("county_score", 0)) for r in done_rows]
        bands  = [(95, 101, "95-100"), (86, 95, "86-94"),
                  (72, 86, "72-85"),  (0, 72, "<72")]
        if scores:
            print(f"\n  County score bands:")
            for lo, hi, lbl in bands:
                n = sum(1 for s in scores if lo <= s < hi)
                print(f"    {lbl}: {n}")

    if stage == "location":
        # Show field completeness
        sec_n = sum(1 for r in done_rows if r.get("location_section"))
        twp_n = sum(1 for r in done_rows if r.get("location_township"))
        rng_n = sum(1 for r in done_rows if r.get("location_range"))
        n     = len(done_rows) or 1
        print(f"\n  Field completeness (of detected):")
        print(f"    section   {sec_n}/{n}  ({100*sec_n//n}%)")
        print(f"    township  {twp_n}/{n}  ({100*twp_n//n}%)")
        print(f"    range     {rng_n}/{n}  ({100*rng_n//n}%)")

    print()


# -- Main ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Per-stage smoke test (100 records)")
    ap.add_argument("--stage",   choices=ALL_STAGES,
                    help="Run only one stage (default: all)")
    ap.add_argument("--rebuild-index", action="store_true",
                    help="Re-sample 100 records even if index exists")
    ap.add_argument("--workers", type=int, default=1,
                    help="Parallel workers inside each stage run")
    ap.add_argument("--inspect-only", action="store_true",
                    help="Skip running; just re-print inspection from existing outputs")
    args = ap.parse_args()

    stages = [args.stage] if args.stage else ALL_STAGES

    # Build / reuse the mini index
    build_index(force=args.rebuild_index)

    start = time.monotonic()
    for stage in stages:
        if args.inspect_only:
            # Dot reuses the grid output directory.
            base = "grid" if stage == "dot" else stage
            status_csv = SMOKE_ROOT / base / "processing_status.csv"
        else:
            status_csv = run_stage(stage, workers=args.workers)
        inspect_stage(stage, status_csv)

    elapsed = time.monotonic() - start
    mins, secs = divmod(int(elapsed), 60)
    print(f"{'='*68}")
    print(f"  Smoke test complete  ({mins}m {secs}s)")
    print(f"  Output root: {SMOKE_ROOT}")
    print(f"{'='*68}")


if __name__ == "__main__":
    main()

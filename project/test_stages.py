"""
Stage-by-stage integration test against real local PDFs.

Usage:
    cd D:\project_modular\project
    python test_stages.py                         # test all 5 stages on pdfs/../pdfs/
    python test_stages.py --pdf ..\pdfs\some.pdf  # single PDF
    python test_stages.py --stage grid            # one stage only
    python test_stages.py --dir ..\pdfs           # explicit PDF folder

Each stage is run and its result dict is printed.  The script does NOT write
processing_status.csv — it prints results to stdout only.

Exit codes:
    0  all tested stages produced a result dict (detected=True or detected=False
       with a classified error — i.e. no unhandled exceptions)
    1  at least one stage raised an unhandled exception
"""

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

# ── bootstrap sys.path ──────────────────────────────────────────────────────
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

# Load .env for local credentials if it exists
_env = _HERE.parent / ".env"
if _env.exists():
    for _ln in _env.read_text(encoding="utf-8").splitlines():
        _ln = _ln.strip()
        if _ln and not _ln.startswith("#") and "=" in _ln:
            _k, _, _v = _ln.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Also try credentials/ directory (same logic as config.py)
_creds_dir = _HERE.parent / "credentials"
if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and _creds_dir.is_dir():
    _cands = sorted(_creds_dir.glob("*.json"))
    if len(_cands) == 1:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_cands[0])
        print(f"[init] GOOGLE_APPLICATION_CREDENTIALS -> {_cands[0].name}")

from config import (
    ALL_STAGES, STAGE_LATLONG, STAGE_GRID, STAGE_LOCATION,
    STAGE_COUNTY, STAGE_DOT, GRID_REVIEW_BELOW, DOT_REVIEW_BELOW,
)
from pdf.pdf_manager import PDFDocumentManager
from scan_dataset import DatasetRecord, OutputPathBuilder
from utils.logging_utils import get_logger

_STAGE_LABEL = {
    STAGE_LATLONG:  "Lat/Lon",
    STAGE_GRID:     "Grid",
    STAGE_LOCATION: "Location",
    STAGE_COUNTY:   "County",
    STAGE_DOT:      "Dot",
}
log = get_logger("test_stages")


# ── helpers ─────────────────────────────────────────────────────────────────

def _w(n: int = 72) -> None:
    print("-" * n)


def _banner(msg: str) -> None:
    _w()
    print(f"  {msg}")
    _w()


def _fmt_result(r: dict) -> str:
    if r.get("detected"):
        core = {k: v for k, v in r.items()
                if k not in ("detected", "error", "_elapsed", "image_path")}
        return f"[DETECTED]  {core}"
    err = r.get("error", "no-error-field")
    return f"[NOT DETECTED]  error={err!r}"


def _run_stage(stage: str, manager, out_dir: Path,
               pdf_stem: str, record, grid_dir: Path,
               output_root: Path) -> dict:
    """Dispatch a single stage and return its result dict."""
    if stage == STAGE_LATLONG:
        from latlong.latlong_extractor import process_single_latlong
        return process_single_latlong(manager, pdf_stem, log)

    if stage == STAGE_GRID:
        from grid.scoring import process_single_grid
        return process_single_grid(manager, out_dir, pdf_stem, log)

    if stage == STAGE_LOCATION:
        from config import TIER_CONFIG, tier_for
        strategy = TIER_CONFIG.get(
            tier_for(getattr(record, "collection_num", 0)),
            {"location_strategy": "str_keywords"},
        )["location_strategy"]
        if strategy == "location_keyword":
            from location.location_keyword_extractor import (
                process_single_location_keyword,
            )
            return process_single_location_keyword(manager, out_dir, pdf_stem, log)
        from location.location_extractor import process_single_location
        return process_single_location(manager, out_dir, pdf_stem, log)

    if stage == STAGE_COUNTY:
        from county.county_extractor import process_single_county
        return process_single_county(manager, out_dir, pdf_stem, log)

    if stage == STAGE_DOT:
        from dot.dot_extractor import process_single_dot
        from config import tier_for
        tier = tier_for(getattr(record, "collection_num", 0))
        return process_single_dot(
            grid_dir, out_dir, pdf_stem, log,
            tier=tier, output_root=output_root,
        )

    raise ValueError(f"Unknown stage: {stage!r}")


# ── main test runner ─────────────────────────────────────────────────────────

def run_tests(pdfs: list[Path], stages: tuple,
              output_root: Path) -> int:
    """
    Run the given stages on each PDF.
    Returns the number of unhandled exceptions (0 = all good).
    """
    total_exceptions = 0

    for pdf_path in pdfs:
        record = DatasetRecord(
            pdf_stem        = pdf_path.stem,
            pdf_path        = str(pdf_path),
            collection      = "test",
            collection_safe = "test",
        )
        paths = OutputPathBuilder(output_root)

        _banner(f"PDF: {pdf_path.name}")
        print(f"  Output root : {output_root}")

        try:
            manager = PDFDocumentManager(str(pdf_path))
            pages   = manager.page_count()
            print(f"  Pages       : {pages}")
        except Exception as exc:
            print(f"  ERROR opening PDF: {exc}")
            total_exceptions += 1
            continue

        grid_dir = paths.grids_dir(record)
        grid_dir.mkdir(parents=True, exist_ok=True)

        for stage in stages:
            label   = _STAGE_LABEL.get(stage, stage)
            out_dir = {
                STAGE_LATLONG:  paths.grids_dir(record),
                STAGE_GRID:     paths.grids_dir(record),
                STAGE_LOCATION: paths.locations_dir(record),
                STAGE_COUNTY:   paths.counties_dir(record),
                STAGE_DOT:      paths.dots_dir(record),
            }[stage]
            out_dir.mkdir(parents=True, exist_ok=True)

            print(f"\n  [{label}]")
            t0 = time.monotonic()
            try:
                result = _run_stage(
                    stage, manager, out_dir,
                    record.pdf_stem, record, grid_dir, output_root,
                )
                elapsed = time.monotonic() - t0
                print(f"    elapsed : {elapsed:.1f}s")
                print(f"    result  : {_fmt_result(result)[:200]}")

                if result.get("image_path"):
                    img = Path(result["image_path"])
                    if img.exists():
                        print(f"    image   : {img}  ({img.stat().st_size:,} B)")
                    else:
                        print(f"    image   : {img}  [FILE NOT FOUND]")

                # Confidence annotation
                conf = int(result.get("confidence", 0))
                if stage == STAGE_GRID and result.get("detected"):
                    flag = " <- LOW CONF -> review queue" if conf < GRID_REVIEW_BELOW else ""
                    print(f"    conf    : {conf}%{flag}")
                elif stage == STAGE_DOT and result.get("detected"):
                    flag = " <- LOW CONF -> review queue" if conf < DOT_REVIEW_BELOW else ""
                    print(f"    conf    : {conf}%{flag}")

            except Exception:
                elapsed = time.monotonic() - t0
                print(f"    elapsed : {elapsed:.1f}s")
                print(f"    UNHANDLED EXCEPTION:")
                traceback.print_exc(file=sys.stdout)
                total_exceptions += 1

    _banner(f"Done — {len(pdfs)} PDF(s) × {len(stages)} stage(s)  "
            f"unhandled exceptions: {total_exceptions}")
    return total_exceptions


# ── connectivity checks ──────────────────────────────────────────────────────

def check_connections() -> None:
    """Quick sanity-check of GCP credentials and API reachability."""
    _banner("Connection / credential checks")

    # Google Application Credentials
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if gac and Path(gac).exists():
        import json as _json
        sa = _json.loads(Path(gac).read_text(encoding="utf-8"))
        print(f"  [OK] GOOGLE_APPLICATION_CREDENTIALS -> {Path(gac).name}")
        print(f"    SA email : {sa.get('client_email', '?')}")
        print(f"    key_id   : {sa.get('private_key_id', '?')}")
    else:
        print(f"  [MISS] GOOGLE_APPLICATION_CREDENTIALS not set or missing ({gac!r})")

    # Vision API test (import only -- no real call)
    try:
        from google.cloud import vision  # noqa: F401
        print("  [OK] google-cloud-vision importable")
    except ImportError as exc:
        print(f"  [MISS] google-cloud-vision not installed: {exc}")

    # Gemini API
    gemini_key = os.environ.get("GOOGLE_API_KEY", "")
    if gemini_key:
        print(f"  [OK] GOOGLE_API_KEY set ({len(gemini_key)} chars)")
    else:
        print("  [INFO] GOOGLE_API_KEY not set -- Gemini will use ADC (service account)")
    try:
        import google.generativeai as genai  # noqa: F401
        print("  [OK] google-generativeai importable")
    except ImportError as exc:
        print(f"  [MISS] google-generativeai not installed: {exc}")

    # Tesseract
    try:
        import pytesseract
        ver = pytesseract.get_tesseract_version()
        print(f"  [OK] Tesseract {ver}")
    except Exception as exc:
        print(f"  [MISS] Tesseract check failed: {exc}")

    # PyTorch / U-Net
    try:
        import torch
        print(f"  [OK] PyTorch {torch.__version__}  (CUDA: {torch.cuda.is_available()})")
        ckpt = _HERE.parent / "unet_best.pth"
        if ckpt.exists():
            print(f"  [OK] unet_best.pth ({ckpt.stat().st_size / 1024**2:.1f} MB)")
        else:
            print(f"  [MISS] unet_best.pth not found at {ckpt}")
    except ImportError:
        print("  [MISS] PyTorch not installed")

    _w()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Pipeline stage integration tests")
    ap.add_argument("--pdf",    type=Path, help="Single PDF to test")
    ap.add_argument("--dir",    type=Path,
                    default=_HERE.parent / "pdfs",
                    help="Folder of PDFs (default: ../pdfs/)")
    ap.add_argument("--stage",  choices=list(ALL_STAGES),
                    help="Run only this stage (default: all)")
    ap.add_argument("--limit",  type=int, default=3,
                    help="Max PDFs to test from --dir (default 3)")
    ap.add_argument("--output", type=Path,
                    default=Path(os.environ.get(
                        "OUTPUT_ROOT",
                        r"D:\project_outputs_test" if __import__("sys").platform == "win32"
                        else "/tmp/output_test"
                    )),
                    help="Output root for test artefacts")
    ap.add_argument("--no-check", action="store_true",
                    help="Skip connection/credential checks")
    args = ap.parse_args()

    if not args.no_check:
        check_connections()

    if args.pdf:
        pdfs = [args.pdf]
    elif args.dir.is_dir():
        pdfs = sorted(args.dir.rglob("*.pdf"))[: args.limit]
        if not pdfs:
            print(f"No PDFs found under {args.dir}")
            sys.exit(0)
    else:
        print(f"PDF directory not found: {args.dir}")
        sys.exit(1)

    stages = (args.stage,) if args.stage else ALL_STAGES

    args.output.mkdir(parents=True, exist_ok=True)
    n_exc = run_tests(pdfs, stages, args.output)
    sys.exit(1 if n_exc else 0)


if __name__ == "__main__":
    main()

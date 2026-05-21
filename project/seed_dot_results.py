"""
Import existing U-Net dot detections into processing_status.csv.

The well_dot_detector project already ran inference on 2,531 grid images
collected from ExportedFolderContents_1 (years 1911-1914) and wrote
D:\\well_dot_detector\\detected_dots.csv.

This script maps those flat-named results back to the pipeline's
DatasetRecord pdf_stems and marks STAGE_DOT as DONE (or FAILED if no
dot was found) without re-running inference.

Usage:
  python project/seed_dot_results.py
  python project/seed_dot_results.py \
      --dots   D:\\well_dot_detector\\detected_dots.csv \
      --grids  D:\\project_outputs\\grids \
      --status D:\\project_outputs\\processing_status.csv \
      --dry-run
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import OUTPUT_ROOT, PROCESSING_STATUS_CSV
from utils.processing_status import DONE, FAILED, ProcessingStatus

_DEFAULT_DOTS_CSV   = Path(r"D:\well_dot_detector\detected_dots.csv")
_DEFAULT_GRIDS_ROOT = OUTPUT_ROOT / "grids"


# ---------------------------------------------------------------------------
# Name mapping: flat filename -> (pdf_stem, grid_png_path)
# ---------------------------------------------------------------------------

def _build_flat_name(grid_png: Path) -> str:
    """
    Mirrors collect_images.unique_dest_name() so the flat name produced
    here matches the 'image' column in detected_dots.csv exactly.

    grid_png: .../ExportedFolderContents_N/year/month/pdf_stem/pdf_stem_page_N_grid.png
    -> flat:  year_month_pdf_stem_page_N_grid
    """
    parts = grid_png.parts
    try:
        # Find the ExportedFolderContents_N segment
        idx = next(i for i, p in enumerate(parts) if p.lower().startswith("exportedfoldercontents_"))
    except StopIteration:
        return grid_png.stem

    tail = parts[idx + 1:]  # [year, month, pdf_stem, filename.png]
    if len(tail) < 4:
        return grid_png.stem

    stem_dir = tail[2]   # pdf_stem directory name
    name     = grid_png.stem  # pdf_stem_page_N_grid

    # Strip the redundant stem prefix inside the filename.
    if name.startswith(stem_dir):
        name = name[len(stem_dir):].lstrip("_")  # -> page_N_grid

    flat = "_".join([*tail[:-1], name])

    # Sanitise (mirrors collect_images.py).
    for bad in '<>:"/\\|?*':
        flat = flat.replace(bad, "_")

    return flat


def build_grid_index(grids_root: Path) -> dict[str, tuple[str, Path]]:
    """
    Walk grids_root and return {flat_name -> (pdf_stem, grid_png_path)}.
    Only processes ExportedFolderContents_N subdirectories.
    """
    index: dict[str, tuple[str, Path]] = {}
    for col_dir in sorted(grids_root.iterdir()):
        if not col_dir.is_dir():
            continue
        for grid_png in col_dir.rglob("*_grid.png"):
            flat  = _build_flat_name(grid_png)
            # pdf_stem is the parent directory of the grid PNG.
            stem  = grid_png.parent.name
            index[flat] = (stem, grid_png)
    return index


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def seed(dots_csv: Path, grids_root: Path, status_path: Path,
         dry_run: bool = False):
    """
    Load detected_dots.csv, resolve each image to a pdf_stem via the grid
    index, and mark STAGE_DOT as DONE or FAILED in processing_status.csv.
    """
    import csv

    print(f"Building grid index from: {grids_root}")
    index = build_grid_index(grids_root)
    print(f"  {len(index):,} grid PNGs indexed")

    print(f"Loading dot results: {dots_csv}")
    with dots_csv.open(newline="", encoding="utf-8") as f:
        dot_rows = list(csv.DictReader(f))
    print(f"  {len(dot_rows):,} dot detections")

    # Build {flat_stem -> dot_row} — take highest-confidence detection per image.
    dot_by_flat: dict[str, dict] = {}
    for row in dot_rows:
        flat = row["image"]
        existing = dot_by_flat.get(flat)
        if existing is None or float(row["confidence"]) > float(existing["confidence"]):
            dot_by_flat[flat] = row

    print(f"Loading processing status: {status_path}")
    status = ProcessingStatus(status_path)
    # Suppress intermediate saves during bulk import — one final flush at the end.
    from utils import processing_status as _ps_mod
    _orig_interval = _ps_mod.SAVE_INTERVAL
    _ps_mod.SAVE_INTERVAL = 10_000_000
    n_already_done = sum(
        1 for stem in status._rows
        if status._rows[stem].get("dot_status") == DONE
    )
    print(f"  {len(status._rows):,} records  ({n_already_done} dot already DONE)")

    n_done = n_failed = n_no_record = n_skipped = 0

    # Mark DONE for all images in detected_dots.csv.
    for flat, dot in dot_by_flat.items():
        if flat not in index:
            n_no_record += 1
            continue
        pdf_stem, grid_png = index[flat]
        if pdf_stem not in status._rows:
            n_no_record += 1
            continue
        if status._rows[pdf_stem].get("dot_status") == DONE:
            n_skipped += 1
            continue

        confidence_int = round(float(dot["confidence"]) * 100)
        result = {
            "detected":   True,
            "row":        int(dot["row"]),
            "col":        int(dot["col"]),
            "nw":         dot["nw"],
            "confidence": confidence_int,
        }
        if not dry_run:
            status.mark_done(pdf_stem, "dot", result)
        n_done += 1

    # Mark FAILED (no_dot_found) for images present in the index but absent
    # from detected_dots.csv — the U-Net found no candidate above threshold.
    detected_flats = set(dot_by_flat.keys())
    for flat, (pdf_stem, _) in index.items():
        if flat in detected_flats:
            continue
        if pdf_stem not in status._rows:
            continue
        existing = status._rows[pdf_stem].get("dot_status", "")
        if existing in (DONE, FAILED):
            n_skipped += 1
            continue
        if not dry_run:
            status.mark_failed(pdf_stem, "dot", "no_dot_found")
        n_failed += 1

    _ps_mod.SAVE_INTERVAL = _orig_interval   # restore

    if not dry_run:
        print("\nSaving processing_status.csv ...")
        status.force_save()
        print(f"Seeded {n_done:,} DONE  |  {n_failed:,} FAILED  |  "
              f"{n_skipped:,} already set  |  {n_no_record:,} unmatched")
        print(f"Saved -> {status_path}")
    else:
        print(f"\n[DRY RUN] Would mark {n_done:,} DONE  |  {n_failed:,} FAILED  |  "
              f"{n_skipped:,} already set  |  {n_no_record:,} unmatched")


def main():
    ap = argparse.ArgumentParser(description="Seed dot stage results from detected_dots.csv")
    ap.add_argument("--dots",    type=Path, default=_DEFAULT_DOTS_CSV)
    ap.add_argument("--grids",   type=Path, default=_DEFAULT_GRIDS_ROOT)
    ap.add_argument("--status",  type=Path, default=PROCESSING_STATUS_CSV)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dots.exists():
        raise SystemExit(f"Dots CSV not found: {args.dots}")
    if not args.grids.exists():
        raise SystemExit(f"Grids root not found: {args.grids}")

    seed(args.dots, args.grids, args.status, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

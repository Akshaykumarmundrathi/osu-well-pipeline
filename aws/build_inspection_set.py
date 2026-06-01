"""
build_inspection_set.py  —  Download first + last PDF of every month-folder
============================================================================

Samples the S3 PDF archive: for each Collection / Year / Month folder,
downloads the first and last PDF file (sorted alphabetically by filename).

Use this to manually inspect how the form layout, grid position, and text
placement changes across collections and years — so you can tune the pipeline
configuration per collection tier.

Output layout:
  <output_dir>/
    Collection_1/
      1911/
        01 - January/
          FIRST__03701260_JUDIE BARNETT 10.pdf
          LAST___03736160_RIVERBED 7.pdf
        02 - February/
          ...
    Collection_7/
      ...
    _inspection_index.csv   ← full manifest of what was downloaded + skipped

Usage:
    python aws/build_inspection_set.py
    python aws/build_inspection_set.py --output D:/inspection_pdfs
    python aws/build_inspection_set.py --collections 1 7 9   # specific collections only
    python aws/build_inspection_set.py --dry-run             # list only, no download
    python aws/build_inspection_set.py --limit-months 20     # cap at 20 folders (quick test)
"""

import argparse
import csv
import os
import sys
import threading
from collections import defaultdict
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ── Config ────────────────────────────────────────────────────────────────────
BUCKET    = "osu-well-records-225989338968"
PDF_PFX   = "pdfs/"
REGION    = "us-east-1"
PROFILE   = os.environ.get("AWS_PROFILE_ACCOUNT1", "default")

_progress_lock = threading.Lock()
_downloaded    = 0
_skipped       = 0
_failed        = 0


def _p(msg, end="\n"):
    print(msg, end=end, flush=True)


# ── S3 listing ────────────────────────────────────────────────────────────────

def list_month_folders(s3, only_collections: list[int] | None) -> dict:
    """
    Scan s3://BUCKET/pdfs/ and build:
      { "ExportedFolderContents_N/YEAR/MONTH": [sorted list of pdf filenames] }

    Returns sorted dict so collections → years → months are in order.
    """
    _p("Scanning S3 for PDF structure…  (this takes ~1-2 min for 571K PDFs)")
    folders: dict[str, list[str]] = defaultdict(list)

    pag = s3.get_paginator("list_objects_v2")
    count = 0
    for page in pag.paginate(Bucket=BUCKET, Prefix=PDF_PFX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith(".pdf"):
                continue
            # pdfs / ExportedFolderContents_N / YEAR / MONTH / stem.pdf
            segs = key[len(PDF_PFX):].split("/")
            if len(segs) < 4:
                continue

            coll, year, month, filename = segs[0], segs[1], segs[2], segs[3]

            # Filter to requested collections
            if only_collections:
                import re
                m = re.search(r"\d+", coll)
                if not m or int(m.group()) not in only_collections:
                    continue

            folder_key = f"{coll}/{year}/{month}"
            folders[folder_key].append(filename)
            count += 1
            if count % 50_000 == 0:
                _p(f"  … {count:,} PDFs indexed, {len(folders):,} folders so far")

    # Sort filenames within each folder (alphabetical = chronological API number order)
    for k in folders:
        folders[k].sort()

    _p(f"  Done — {count:,} PDFs across {len(folders):,} month-folders\n")
    return dict(sorted(folders.items()))


# ── Download ──────────────────────────────────────────────────────────────────

def download_pdf(s3, s3_key: str, local_path: Path, dry_run: bool) -> bool:
    global _downloaded, _failed
    if dry_run:
        _p(f"  [DRY] {local_path.name}")
        return True
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists():
        with _progress_lock:
            _skipped += 1
        return True
    try:
        s3.download_file(BUCKET, s3_key, str(local_path))
        with _progress_lock:
            _downloaded += 1
        return True
    except ClientError as exc:
        _p(f"\n  WARN download failed {s3_key}: {exc}")
        with _progress_lock:
            _failed += 1
        return False


def sanitise(name: str) -> str:
    """Remove characters that cause problems in Windows paths."""
    for ch in r'\:*?"<>|':
        name = name.replace(ch, "_")
    return name


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Download first + last PDF of every S3 month-folder for manual inspection."
    )
    ap.add_argument("--output",  default=os.environ.get(
                        "INSPECTION_ROOT",
                        str(Path(__file__).parent.parent / "inspection_pdfs")
                    ),
                    help="Local output directory (default: ../inspection_pdfs/)")
    ap.add_argument("--profile", default=PROFILE,
                    help="AWS profile for Account 1 (default: %(default)s)")
    ap.add_argument("--collections", type=int, nargs="+", metavar="N",
                    help="Only download these collection numbers, e.g. --collections 1 7 9")
    ap.add_argument("--dry-run",     action="store_true",
                    help="List what would be downloaded without downloading")
    ap.add_argument("--limit-months", type=int, default=None, metavar="N",
                    help="Stop after this many month-folders (quick preview)")
    ap.add_argument("--both-on-same", action="store_true",
                    help="If folder has only 1 PDF, still copy it as both FIRST and LAST")
    args = ap.parse_args()

    output_dir = Path(args.output)
    _p(f"OSU Pipeline — Inspection Set Builder")
    _p(f"  S3 source:   s3://{BUCKET}/{PDF_PFX}")
    _p(f"  Local dest:  {output_dir}")
    if args.collections:
        _p(f"  Collections: {args.collections}")
    if args.dry_run:
        _p("  DRY RUN — no files will be downloaded")
    _p("")

    sess = boto3.Session(profile_name=args.profile, region_name=REGION)
    s3   = sess.client("s3")

    # 1. Build folder → [filenames] map
    folders = list_month_folders(s3, args.collections)

    if args.limit_months:
        folders = dict(list(folders.items())[:args.limit_months])
        _p(f"  (limited to first {args.limit_months} month-folders)\n")

    # 2. Build manifest and download
    manifest = []
    total_folders = len(folders)
    done_folders  = 0

    for folder_key, filenames in folders.items():
        done_folders += 1
        coll, year, month = folder_key.split("/", 2)

        # Clean names for filesystem
        coll_dir  = sanitise(coll)
        year_dir  = sanitise(year)
        month_dir = sanitise(month)
        local_folder = output_dir / coll_dir / year_dir / month_dir

        n = len(filenames)
        first_fn = filenames[0]
        last_fn  = filenames[-1]

        # Progress line
        if done_folders % 50 == 0 or done_folders == total_folders:
            _p(f"  [{done_folders:>4}/{total_folders}] {folder_key}  ({n} PDFs)")

        # Download FIRST
        first_stem = Path(first_fn).stem
        first_local = local_folder / f"FIRST__{first_stem}.pdf"
        first_key   = f"{PDF_PFX}{folder_key}/{first_fn}"
        ok_first = download_pdf(s3, first_key, first_local, args.dry_run)

        manifest.append({
            "collection": coll, "year": year, "month": month,
            "position": "FIRST", "filename": first_fn,
            "s3_key": first_key, "local_path": str(first_local),
            "total_in_folder": n, "status": "ok" if ok_first else "failed",
        })

        # Download LAST (only if different from first)
        if last_fn != first_fn:
            last_stem  = Path(last_fn).stem
            last_local = local_folder / f"LAST___{last_stem}.pdf"
            last_key   = f"{PDF_PFX}{folder_key}/{last_fn}"
            ok_last = download_pdf(s3, last_key, last_local, args.dry_run)

            manifest.append({
                "collection": coll, "year": year, "month": month,
                "position": "LAST",  "filename": last_fn,
                "s3_key": last_key,  "local_path": str(last_local),
                "total_in_folder": n, "status": "ok" if ok_last else "failed",
            })
        elif args.both_on_same:
            # Single-PDF folder — copy same file as LAST too
            last_local = local_folder / f"LAST___{first_stem}.pdf"
            if not args.dry_run and first_local.exists() and not last_local.exists():
                import shutil
                shutil.copy2(str(first_local), str(last_local))
            manifest.append({
                "collection": coll, "year": year, "month": month,
                "position": "LAST (=FIRST)", "filename": first_fn,
                "s3_key": first_key, "local_path": str(last_local),
                "total_in_folder": n, "status": "ok",
            })

    # 3. Write manifest CSV
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        index_path = output_dir / "_inspection_index.csv"
        with open(index_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(manifest[0].keys()))
            writer.writeheader()
            writer.writerows(manifest)
        _p(f"\n  Manifest: {index_path}")

    # 4. Summary
    _p(f"\n{'='*60}")
    _p(f"  Month-folders scanned:   {total_folders:,}")
    _p(f"  PDFs to download:        {len(manifest):,}  (2 per folder = first + last)")
    if not args.dry_run:
        _p(f"  Downloaded:              {_downloaded:,}")
        _p(f"  Already existed:         {_skipped:,}")
        _p(f"  Failed:                  {_failed:,}")
    _p(f"  Output:                  {output_dir}")
    _p(f"{'='*60}\n")
    _p("Now open the folder and browse — one subfolder per collection/year/month.")
    _p("Note down layout differences (grid position, STR text placement, county box).")


if __name__ == "__main__":
    main()

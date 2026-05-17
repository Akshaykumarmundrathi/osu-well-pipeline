"""
Phase 1 — Dataset Scanner

Scans D:\ for ExportedFolderContents (N).zip files (or a flat PDF folder).
Each ZIP's internal structure: {year}/{month}/*.pdf

CLI:
    python scan_dataset.py                     # full ZIP scan from D:\
    python scan_dataset.py --source D:\        # explicit source root
    python scan_dataset.py --flat D:\pdfs      # flat folder (local testing)
    python scan_dataset.py --dry-run           # scan without writing
    python scan_dataset.py --summary           # stats from existing index
    python scan_dataset.py --validate          # verify ZIPs/files still exist
"""

import argparse
import csv
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import DATASET_INDEX_CSV, OUTPUT_ROOT, SOURCE_ROOT
from utils.zip_reader import _extract_collection_num, list_pdfs_in_zip


# -- DatasetRecord -------------------------------------------------------------

@dataclass
class DatasetRecord:
    pdf_stem:         str
    pdf_path:         str   # full file path (flat) or  "zip::internal" (ZIP)
    collection:       str   = ""
    collection_num:   int   = 0
    year:             str   = ""
    month:            str   = ""
    collection_safe:  str   = ""
    month_safe:       str   = ""
    file_size_bytes:  int   = 0
    scan_timestamp:   str   = ""
    zip_path:         str   = ""   # path to the ZIP file  (empty for flat records)
    internal_path:    str   = ""   # path inside the ZIP   (empty for flat records)


# -- Path helpers --------------------------------------------------------------

def _safe(s: str) -> str:
    return re.sub(r"[^\w]", "_", s).strip("_")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# -- Output path builder -------------------------------------------------------

class OutputPathBuilder:
    """Derives all output paths from a DatasetRecord, mirroring input hierarchy."""

    def __init__(self, output_root: Path):
        self.root = output_root

    def _base(self, r: DatasetRecord, stage: str) -> Path:
        col   = r.collection_safe or "local"
        year  = r.year            or "unknown"
        month = r.month_safe      or "unknown"
        return self.root / stage / col / year / month / r.pdf_stem

    def grids_dir(self,    r: DatasetRecord) -> Path: return self._base(r, "grids")
    def locations_dir(self,r: DatasetRecord) -> Path: return self._base(r, "locations")
    def counties_dir(self, r: DatasetRecord) -> Path: return self._base(r, "counties")

    def metadata_path(self, r: DatasetRecord) -> Path:
        return self._base(r, "metadata") / "metadata.json"

    def log_path(self, r: DatasetRecord) -> Path:
        col   = r.collection_safe or "local"
        year  = r.year            or "unknown"
        month = r.month_safe      or "unknown"
        return self.root / "logs" / col / year / month / f"{r.pdf_stem}.log"


# -- Scanning ------------------------------------------------------------------

def scan_collection_root(source_root: Path) -> list[DatasetRecord]:
    """
    Finds ExportedFolderContents (N).zip files directly under source_root,
    then indexes every PDF inside each ZIP.
    """
    records: list[DatasetRecord] = []
    ts = _now()
    zip_pattern = re.compile(r"ExportedFolderContents\s*\((\d+)\)", re.IGNORECASE)

    for entry in sorted(source_root.iterdir()):
        # Accept both  "ExportedFolderContents (1).zip"  and  "ExportedFolderContents (1)"
        stem_name = entry.stem if entry.suffix.lower() == ".zip" else entry.name
        m = zip_pattern.match(stem_name)
        if not m:
            continue

        num      = int(m.group(1))
        col_safe = f"ExportedFolderContents_{num}"

        if entry.is_dir():
            # Treat as an already-extracted folder
            records.extend(_scan_extracted_folder(entry, entry.name, num, col_safe, ts))
        elif entry.suffix.lower() == ".zip":
            try:
                entries = list_pdfs_in_zip(entry)
            except RuntimeError as exc:
                print(f"WARNING: {exc}")
                continue

            for e in entries:
                records.append(DatasetRecord(
                    pdf_stem        = Path(e["pdf_name"]).stem,
                    pdf_path        = f"{entry}::{e['internal_path']}",
                    collection      = entry.name,
                    collection_num  = num,
                    year            = e["year"],
                    month           = e["month"],
                    collection_safe = col_safe,
                    month_safe      = _safe(e["month"]),
                    file_size_bytes = e["file_size"],
                    scan_timestamp  = ts,
                    zip_path        = str(entry),
                    internal_path   = e["internal_path"],
                ))

    return records


def _scan_extracted_folder(
    folder: Path, col_name: str, num: int, col_safe: str, ts: str
) -> list[DatasetRecord]:
    """Walk an already-extracted ExportedFolderContents directory."""
    records = []
    for year_dir in sorted(folder.iterdir()):
        if not year_dir.is_dir():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir():
                continue
            for pdf in sorted(month_dir.glob("*.pdf")):
                records.append(DatasetRecord(
                    pdf_stem        = pdf.stem,
                    pdf_path        = str(pdf),
                    collection      = col_name,
                    collection_num  = num,
                    year            = year_dir.name,
                    month           = month_dir.name,
                    collection_safe = col_safe,
                    month_safe      = _safe(month_dir.name),
                    file_size_bytes = pdf.stat().st_size,
                    scan_timestamp  = ts,
                ))
    return records


def scan_flat_folder(folder: Path) -> list[DatasetRecord]:
    """Flat folder of PDFs — for local testing."""
    ts = _now()
    return [
        DatasetRecord(
            pdf_stem        = pdf.stem,
            pdf_path        = str(pdf),
            collection      = "local",
            collection_safe = "local",
            file_size_bytes = pdf.stat().st_size,
            scan_timestamp  = ts,
        )
        for pdf in sorted(folder.glob("*.pdf"))
    ]


# -- CSV I/O -------------------------------------------------------------------

_FIELDS = list(DatasetRecord.__dataclass_fields__.keys())


def write_index(records: list[DatasetRecord], csv_path: Path):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS)
        w.writeheader()
        w.writerows(asdict(r) for r in records)
    print(f"Wrote {len(records)} records -> {csv_path}")


def load_index(csv_path: Path) -> list[DatasetRecord]:
    if not csv_path.exists():
        return []
    int_fields = {"collection_num", "file_size_bytes"}
    with csv_path.open(newline="", encoding="utf-8") as f:
        return [
            DatasetRecord(**{
                k: (int(v) if k in int_fields and v else v)
                for k, v in row.items()
            })
            for row in csv.DictReader(f)
        ]


# -- CLI -----------------------------------------------------------------------

def _summary(csv_path: Path):
    records = load_index(csv_path)
    if not records:
        print("No index found.")
        return
    from collections import Counter
    cols  = Counter(r.collection for r in records)
    years = [r.year for r in records if r.year.isdigit()]
    total_mb = sum(r.file_size_bytes for r in records) / 1e6
    print(f"Records     : {len(records)}")
    print(f"Total size  : {total_mb:.1f} MB")
    print(f"Collections : {len(cols)}")
    if years:
        print(f"Years       : {min(years)} – {max(years)}")


def _validate(csv_path: Path):
    records = load_index(csv_path)
    missing = []
    for r in records:
        if r.zip_path:
            if not Path(r.zip_path).exists():
                missing.append(r.zip_path)
        elif not Path(r.pdf_path).exists():
            missing.append(r.pdf_path)
    if missing:
        print(f"{len(missing)} missing:")
        for p in missing[:20]:
            print(f"  {p}")
    else:
        print(f"All {len(records)} sources present on disk.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source",   type=Path, default=SOURCE_ROOT)
    ap.add_argument("--flat",     type=Path)
    ap.add_argument("--output",   type=Path, default=DATASET_INDEX_CSV)
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--summary",  action="store_true")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    if args.summary:
        _summary(args.output); return
    if args.validate:
        _validate(args.output); return

    records = scan_flat_folder(args.flat) if args.flat else scan_collection_root(args.source)
    print(f"Found {len(records)} PDFs")
    if not args.dry_run:
        write_index(records, args.output)


if __name__ == "__main__":
    main()

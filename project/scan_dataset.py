"""
Phase 1 — Dataset Scanner

Scans an ExportedFolderContents directory tree (or flat PDF folder) and
builds dataset_index.csv.  PDFs are always flat — never inside a ZIP.

Expected directory structure under source_root:
    ExportedFolderContents (N)/
        {year}/
            {month}/
                *.pdf

CLI:
    python scan_dataset.py                     # scan from D:\
    python scan_dataset.py --source D:\        # explicit source root
    python scan_dataset.py --flat D:\pdfs      # flat folder of PDFs
    python scan_dataset.py --dry-run           # scan without writing
    python scan_dataset.py --summary           # stats from existing index
    python scan_dataset.py --validate          # verify files still exist
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
    """Filesystem-safe slug: collapse non-word chars to '_' and strip ends.

    Example: '01 - January' → '01_January'  (consecutive underscores merged).
    """
    return re.sub(r"_+", "_", re.sub(r"[^\w]", "_", s)).strip("_")


def _now() -> str:
    """UTC ISO-like timestamp ('YYYY-MM-DDTHH:MM:SS')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# -- Output path builder -------------------------------------------------------

class OutputPathBuilder:
    """Derives all output paths from a DatasetRecord, mirroring input hierarchy."""

    def __init__(self, output_root: Path):
        """Bind an output root used as the prefix for every derived path."""
        self.root = output_root

    def _parts(self, r: DatasetRecord) -> tuple[str, str, str]:
        """(collection, year, month) path-safe slugs with sensible fallbacks."""
        return (
            r.collection_safe or "local",
            r.year            or "unknown",
            r.month_safe      or "unknown",
        )

    def _base(self, r: DatasetRecord, stage: str) -> Path:
        """Per-stage output directory: <root>/<stage>/<col>/<year>/<month>/<stem>."""
        col, year, month = self._parts(r)
        return self.root / stage / col / year / month / r.pdf_stem

    def grids_dir(self,     r: DatasetRecord) -> Path: return self._base(r, "grids")
    def locations_dir(self, r: DatasetRecord) -> Path: return self._base(r, "locations")
    def counties_dir(self,  r: DatasetRecord) -> Path: return self._base(r, "counties")
    def dots_dir(self,      r: DatasetRecord) -> Path: return self._base(r, "dots")

    def metadata_path(self, r: DatasetRecord) -> Path:
        """Path to the per-record metadata.json file."""
        return self._base(r, "metadata") / "metadata.json"

    def log_path(self, r: DatasetRecord) -> Path:
        """Path to the per-record .log file under <root>/logs/..."""
        col, year, month = self._parts(r)
        return self.root / "logs" / col / year / month / f"{r.pdf_stem}.log"


# -- Scanning ------------------------------------------------------------------

def scan_collection_root(source_root: Path) -> list[DatasetRecord]:
    """
    Find ExportedFolderContents (N) directories directly under source_root
    and index every PDF inside them.  PDFs are flat — no ZIP reading.

    Expected layout:
        <source_root>/
            ExportedFolderContents (1)/
                1911/
                    01 - January/
                        *.pdf
            ExportedFolderContents (2)/
                ...
    """
    records: list[DatasetRecord] = []
    ts = _now()
    dir_pattern = re.compile(r"ExportedFolderContents\s*\((\d+)\)", re.IGNORECASE)

    for entry in sorted(source_root.iterdir()):
        if not entry.is_dir():
            continue
        m = dir_pattern.match(entry.name)
        if not m:
            continue
        num      = int(m.group(1))
        col_safe = f"ExportedFolderContents_{num}"
        records.extend(_scan_extracted_folder(entry, entry.name, num, col_safe, ts))

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


def _collection_num_from_path(path: Path) -> int:
    """
    Try to extract a collection number from any ancestor directory matching
    'ExportedFolderContents (N)'.  Returns 0 when no match is found.
    Used by scan_flat_folder so that --flat runs on real collection dirs
    still get tier-appropriate AR filtering in the grid stage.
    """
    import re
    _pat = re.compile(r"ExportedFolderContents\s*\((\d+)\)", re.I)
    for part in path.parts:
        m = _pat.search(part)
        if m:
            return int(m.group(1))
    return 0


def scan_flat_folder(folder: Path) -> list[DatasetRecord]:
    """
    Recursively find all PDFs under folder.  Used for local testing or any
    directory that doesn't follow the ExportedFolderContents (N) naming.

    When folder or its ancestors contain an 'ExportedFolderContents (N)'
    directory, collection_num is extracted automatically so grid-detection AR
    filters apply correctly for that collection's tier.
    """
    ts = _now()
    # Detect collection_num from the folder path itself (covers the case where
    # the user passes a sub-directory of a real collection, e.g.
    # --flat "D:\ExportedFolderContents (11)\2001\01 - January").
    base_cnum = _collection_num_from_path(folder.resolve())

    records = []
    for pdf in sorted(folder.rglob("*.pdf")):
        # Per-PDF collection_num: inherit from base, or re-detect if the PDF
        # lives deeper in a different ExportedFolderContents subtree.
        cnum = base_cnum or _collection_num_from_path(pdf.resolve())
        records.append(DatasetRecord(
            pdf_stem        = pdf.stem,
            pdf_path        = str(pdf),
            collection      = "local",
            collection_safe = "local",
            collection_num  = cnum,
            file_size_bytes = pdf.stat().st_size,
            scan_timestamp  = ts,
        ))
    return records


# -- CSV I/O -------------------------------------------------------------------

_FIELDS = list(DatasetRecord.__dataclass_fields__.keys())


def write_index(records: list[DatasetRecord], csv_path: Path):
    """Persist a DatasetRecord list to CSV, one row per record."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS)
        w.writeheader()
        w.writerows(asdict(r) for r in records)
    print(f"Wrote {len(records)} records -> {csv_path}")


def load_index(csv_path: Path) -> list[DatasetRecord]:
    """Read the index CSV back into DatasetRecord objects (empty if missing)."""
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
    """Print counts and totals from an existing dataset index CSV."""
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
    """Check that every indexed ZIP/PDF still exists on disk."""
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
    """CLI entry: scan ZIPs or a flat folder and write a dataset index CSV."""
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

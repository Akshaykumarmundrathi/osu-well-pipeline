"""
Helpers for reading PDFs that live inside ZIP archives.

ZIP structure expected (variable depth, last 3 parts used):
    ExportedFolderContents (1).zip
        └-- [optional_prefix/] {year} / {month} / filename.pdf
"""

import re
import zipfile
from pathlib import Path


def get_pdf_bytes(zip_path: str, internal_path: str) -> bytes:
    """Extract a single PDF from a ZIP, return raw bytes. Raises on failure."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        return zf.read(internal_path)


def list_pdfs_in_zip(zip_path: Path) -> list[dict]:
    """
    Walk a ZIP and return one dict per PDF entry:
      {internal_path, year, month, pdf_name, file_size}

    Handles variable prefix depth: uses the last 3 parts of the internal path
    as (year, month, pdf_name). Falls back to empty strings if depth < 3.
    """
    entries = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                if not name.lower().endswith(".pdf"):
                    continue

                parts = [p for p in name.split("/") if p]
                pdf_name = parts[-1]
                month    = parts[-2] if len(parts) >= 2 else ""
                year     = parts[-3] if len(parts) >= 3 else ""

                entries.append({
                    "internal_path": info.filename,
                    "year":          year,
                    "month":         month,
                    "pdf_name":      pdf_name,
                    "file_size":     info.file_size,
                })
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Corrupt ZIP: {zip_path}") from exc

    return entries


def _extract_collection_num(zip_name: str) -> int:
    """Parse the trailing '(N)' from an ExportedFolderContents zip name."""
    m = re.search(r"\((\d+)\)", zip_name)
    return int(m.group(1)) if m else 0

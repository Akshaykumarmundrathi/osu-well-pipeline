"""
S3-aware ZIP/PDF reader.

Mirrors `utils.zip_reader` but pulls bytes from S3 instead of the local
filesystem. Used inside the AWS Batch container; safe to import locally
since boto3 is only constructed lazily.

Usage:
  - dataset_index.csv has rows whose `zip_path` looks like
    "s3://bucket/key/path/to/file.zip" — the rest of the pipeline calls
    `_make_manager(record)` in main.py which detects the prefix and
    routes here.
  - Each unique ZIP is fetched ONCE per process and held in an LRU cache
    so PDFs read from the same ZIP don't re-download.
"""

import io
import zipfile
from functools import lru_cache
from urllib.parse import urlparse

import boto3

_s3 = None


def _client():
    """Lazy singleton boto3 S3 client (one per process)."""
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """
    Split 's3://bucket/key/path' into ('bucket', 'key/path').
    Raises ValueError on malformed input.
    """
    p = urlparse(uri)
    if p.scheme != "s3" or not p.netloc:
        raise ValueError(f"not an s3:// URI: {uri}")
    return p.netloc, p.path.lstrip("/")


@lru_cache(maxsize=64)
def _open_zip_cached(bucket: str, key: str) -> zipfile.ZipFile:
    """
    Stream a ZIP from S3 into memory and return an open ZipFile.
    LRU-cached so subsequent PDFs from the same archive are free.
    """
    obj  = _client().get_object(Bucket=bucket, Key=key)
    data = obj["Body"].read()
    return zipfile.ZipFile(io.BytesIO(data))


def get_pdf_bytes_s3(zip_uri: str, internal_path: str) -> bytes:
    """Extract one PDF (raw bytes) from a ZIP that lives in S3."""
    bucket, key = parse_s3_uri(zip_uri)
    zf = _open_zip_cached(bucket, key)
    return zf.read(internal_path)


def list_pdfs_in_s3_zip(zip_uri: str) -> list[dict]:
    """
    Index every PDF inside a ZIP at zip_uri (an s3:// URI). Returns the
    same shape `zip_reader.list_pdfs_in_zip` returns:
      {internal_path, year, month, pdf_name, file_size}
    """
    bucket, key = parse_s3_uri(zip_uri)
    zf = _open_zip_cached(bucket, key)
    entries = []
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
    return entries


def upload_directory(local_dir, bucket: str, key_prefix: str):
    """
    Upload every file under `local_dir` to s3://bucket/<key_prefix>/relpath.
    Used by the Batch entrypoint to ship per-slice outputs back to S3.
    """
    from pathlib import Path
    cli = _client()
    base = Path(local_dir)
    n = 0
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        cli.upload_file(str(path), bucket,
                        f"{key_prefix.rstrip('/')}/{rel}")
        n += 1
    return n

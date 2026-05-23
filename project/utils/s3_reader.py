"""
S3 PDF reader — flat layout only.

PDFs are stored as standalone objects in S3 (no ZIP wrapper).
The dataset_index.csv pdf_path column contains s3:// URIs.

Used inside the AWS Batch container; boto3 is constructed lazily so
this module is safe to import locally without AWS credentials.
"""

from pathlib import Path
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
    """Split 's3://bucket/key/path' into ('bucket', 'key/path')."""
    p = urlparse(uri)
    if p.scheme != "s3" or not p.netloc:
        raise ValueError(f"not an s3:// URI: {uri!r}")
    return p.netloc, p.path.lstrip("/")


def get_pdf_bytes_s3_flat(pdf_uri: str) -> bytes:
    """Fetch a standalone PDF object from S3 and return its raw bytes."""
    bucket, key = parse_s3_uri(pdf_uri)
    obj = _client().get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()


def upload_directory(local_dir, bucket: str, key_prefix: str) -> int:
    """
    Upload every file under local_dir to s3://bucket/<key_prefix>/relpath.
    Returns the number of files uploaded.
    """
    cli  = _client()
    base = Path(local_dir)
    n    = 0
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        cli.upload_file(str(path), bucket,
                        f"{key_prefix.rstrip('/')}/{rel}")
        n += 1
    return n

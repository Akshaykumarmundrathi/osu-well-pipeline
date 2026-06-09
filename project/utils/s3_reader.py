"""
S3 PDF reader — flat layout only.

PDFs are stored as standalone objects in S3 (no ZIP wrapper).
The dataset_index.csv pdf_path column contains s3:// URIs.

Used inside the AWS Batch container; boto3 is constructed lazily so
this module is safe to import locally without AWS credentials.
"""

import logging
from pathlib import Path
from urllib.parse import urlparse

import boto3

log = logging.getLogger(__name__)

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
    try:
        obj = _client().get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()
    except Exception as exc:
        log.error("S3 fetch failed for %s: %s", pdf_uri, exc)
        raise


def upload_file_to_s3(local_path: str, bucket: str, key: str) -> None:
    """
    Upload a single file to s3://bucket/key.
    Raises on failure (caller decides whether to swallow or propagate).
    """
    try:
        _client().upload_file(local_path, bucket, key)
        log.debug("S3 upload OK: %s → s3://%s/%s", local_path, bucket, key)
    except Exception as exc:
        log.error("S3 upload failed %s → s3://%s/%s: %s", local_path, bucket, key, exc)
        raise


def download_file_from_s3(bucket: str, key: str, local_path: str) -> bool:
    """
    Download s3://bucket/key to local_path.
    Returns True on success, False if the object does not exist.
    Raises on other errors (permissions, network, access denied).
    """
    try:
        _client().download_file(bucket, key, local_path)
        log.debug("S3 download OK: s3://%s/%s → %s", bucket, key, local_path)
        return True
    except Exception as exc:
        # botocore.exceptions.ClientError has a structured response dict;
        # boto3 transfer errors wrap it.  Check both for NoSuchKey / 404.
        err = str(exc)
        if "NoSuchKey" in err or "404" in err or "Not Found" in err:
            return False
        log.error("S3 download failed s3://%s/%s: %s", bucket, key, exc)
        raise


def upload_directory(local_dir, bucket: str, key_prefix: str) -> int:
    """
    Upload every file under local_dir to s3://bucket/<key_prefix>/relpath.
    Returns the number of files uploaded.  Files that fail to upload are
    logged as warnings but do not abort the rest of the upload.
    """
    cli  = _client()
    base = Path(local_dir)
    n    = 0
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel  = path.relative_to(base).as_posix()
        dest = f"{key_prefix.rstrip('/')}/{rel}"
        try:
            cli.upload_file(str(path), bucket, dest)
            n += 1
        except Exception as exc:
            log.warning("S3 upload failed %s -> s3://%s/%s: %s",
                        path, bucket, dest, exc)
    return n

"""
Google Cloud Vision OCR helpers.

- Singleton client. On gRPC channel failure (503 / UNAVAILABLE) the
  client is reset so the next call rebuilds a fresh channel.
- Retry with exponential back-off for transient failures (3, 6, 15s).
- All image data is passed in-memory via BytesIO — no temp files.
"""

import io
import time

from google.api_core.exceptions import ServiceUnavailable
from google.cloud import vision
from PIL import Image as PILImage

from ocr.preprocessing import preprocess_image
from pdf.pdf_manager import PDFDocumentManager

_client: vision.ImageAnnotatorClient | None = None
_RETRY_DELAYS = [3, 6, 15]   # 3 retries after first failure


def _get_client() -> vision.ImageAnnotatorClient:
    """Lazy singleton accessor for the Vision client."""
    global _client
    if _client is None:
        _client = vision.ImageAnnotatorClient()
    return _client


def _reset_client():
    """Drop the cached client; next _get_client() builds a fresh channel."""
    global _client
    _client = None


def _pil_to_bytes(image: PILImage.Image) -> bytes:
    """Encode a PIL image as PNG bytes (RGB)."""
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _call_with_retry(fn):
    """
    Invoke `fn()`. On ServiceUnavailable, reset the client and retry with
    exponential back-off. Any other exception propagates immediately.
    Raises the last ServiceUnavailable if all retries are exhausted.
    """
    last_exc = None
    for delay in [0] + _RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return fn()
        except ServiceUnavailable as exc:
            last_exc = exc
            _reset_client()
    raise last_exc


def _ocr_bytes(image_bytes: bytes):
    """Run document_text_detection on raw image bytes; returns the response."""
    return _call_with_retry(
        lambda: _get_client().document_text_detection(
            image=vision.Image(content=image_bytes)
        )
    )


def detect_text_with_vision(pil_image: PILImage.Image, *,
                            manager: PDFDocumentManager = None,
                            page_num: int = None):
    """
    Preprocess (grayscale + contrast + binarize) the given PIL image and
    run document_text_detection. Returns the `text_annotations` list
    (possibly empty).

    If both `manager` and `page_num` are provided, the result is cached
    under key (page_num, 'pre') on the manager, letting subsequent stages
    that need the same preprocessed annotations skip the Vision call.
    """
    cache_key = (page_num, "pre") if (manager and page_num is not None) else None
    if cache_key and cache_key in manager._ocr_cache:
        return manager._ocr_cache[cache_key]

    processed   = preprocess_image(pil_image).convert("RGB")
    image_bytes = _pil_to_bytes(processed)
    annotations = _ocr_bytes(image_bytes).text_annotations
    if cache_key:
        manager._ocr_cache[cache_key] = annotations
    return annotations


def get_page_annotations(
    pdf_path: str = None,
    page_num: int = 0,
    resolution_multiplier: float = 2.5,
    *,
    pdf_bytes: bytes = None,
    manager: PDFDocumentManager = None,
):
    """
    Render a PDF page and OCR it. Accepts a file path, raw bytes, or an
    existing PDFDocumentManager (preferred — avoids re-opening the doc).
    Returns (text_annotations | None, pil_image | None).

    When a manager is supplied, the (annotations, pil_image) tuple is
    cached on manager._ocr_cache[page_num] so subsequent stages on the
    same record skip the Vision API call.
    """
    try:
        if manager is None:
            manager = PDFDocumentManager(
                pdf_path, pdf_bytes=pdf_bytes,
                resolution_multiplier=resolution_multiplier,
            )

        cached = manager._ocr_cache.get(page_num)
        if cached is not None:
            return cached

        pil_image = manager.get_page_pil(page_num)
        if pil_image is None:
            return None, None

        response = _ocr_bytes(_pil_to_bytes(pil_image))
        if response.error.message:
            return None, pil_image
        result = (response.text_annotations or None), pil_image
        manager._ocr_cache[page_num] = result
        return result

    except Exception as exc:
        src = pdf_path or ("bytes" if pdf_bytes else "manager")
        print(f"Vision API error ({src} page {page_num}): {exc}")
        return None, None


def find_keyword_box(text_annotations, keywords: list[str]) -> list | None:
    """
    Return [x_min, y_min, x_max, y_max] of the first annotation token
    whose lowercased description contains any of the given keywords.
    Returns None if not found.
    """
    if not text_annotations:
        return None
    kw_lower = [k.lower() for k in keywords]
    for ann in text_annotations[1:]:
        cleaned = ann.description.lower().strip(".,:")
        if not any(kw in cleaned for kw in kw_lower):
            continue
        poly = ann.bounding_poly
        if not poly or not poly.vertices:
            continue
        try:
            xs = [v.x for v in poly.vertices]
            ys = [v.y for v in poly.vertices]
            if xs and ys:
                return [min(xs), min(ys), max(xs), max(ys)]
        except Exception:
            continue
    return None

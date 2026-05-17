"""
Google Cloud Vision OCR helpers.
- Singleton client — reset on connection errors, recreated automatically.
- Retry with exponential backoff for transient 503/UNAVAILABLE errors.
- All image data via BytesIO — no temp files.
"""

import io
import time

from google.api_core.exceptions import ServiceUnavailable
from google.cloud import vision
from PIL import Image as PILImage

from ocr.preprocessing import preprocess_image
from pdf.pdf_manager import PDFDocumentManager

_client: vision.ImageAnnotatorClient | None = None
_RETRY_DELAYS = [3, 6, 15]  # seconds; 3 attempts after first failure


def _get_client() -> vision.ImageAnnotatorClient:
    global _client
    if _client is None:
        _client = vision.ImageAnnotatorClient()
    return _client


def _reset_client():
    global _client
    _client = None


def _pil_to_bytes(image: PILImage.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _call_with_retry(fn):
    """
    Call fn(). On ServiceUnavailable (503 / grpc UNAVAILABLE), reset the
    client channel and retry with exponential back-off.
    Any other exception propagates immediately.
    """
    last_exc = None
    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            return fn()
        except ServiceUnavailable as exc:
            last_exc = exc
            _reset_client()
        except Exception:
            raise
    raise last_exc


def detect_text_with_vision(pil_image: PILImage.Image):
    """
    Preprocess PIL image in-memory -> document_text_detection.
    Retries on transient 503. Returns text_annotations list (may be empty).
    """
    processed   = preprocess_image(pil_image).convert("RGB")
    image_bytes = _pil_to_bytes(processed)

    def _call():
        return _get_client().document_text_detection(
            image=vision.Image(content=image_bytes)
        )

    response = _call_with_retry(_call)
    return response.text_annotations


def get_page_annotations(
    pdf_path: str = None,
    page_num: int = 0,
    resolution_multiplier: float = 2.5,
    *,
    pdf_bytes: bytes = None,
    manager: PDFDocumentManager = None,
):
    """
    Render a PDF page and run Vision OCR.
    Accepts a file path, raw bytes, or an existing PDFDocumentManager.
    Returns (text_annotations | None, pil_image | None).
    """
    try:
        if manager is None:
            manager = PDFDocumentManager(
                pdf_path, pdf_bytes=pdf_bytes,
                resolution_multiplier=resolution_multiplier,
            )

        pil_image = manager.get_page_pil(page_num)
        if pil_image is None:
            return None, None

        image_bytes = _pil_to_bytes(pil_image)

        def _call():
            return _get_client().document_text_detection(
                image=vision.Image(content=image_bytes)
            )

        response = _call_with_retry(_call)

        if response.error.message:
            return None, pil_image

        return (response.text_annotations or None), pil_image

    except Exception as exc:
        src = pdf_path or ("bytes" if pdf_bytes else "manager")
        print(f"Vision API error ({src} page {page_num}): {exc}")
        return None, None


def find_keyword_box(text_annotations, keywords: list[str]) -> list | None:
    """
    First annotation token matching any keyword -> [x_min, y_min, x_max, y_max].
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

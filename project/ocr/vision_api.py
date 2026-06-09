"""
OCR helpers — Tesseract by default (free, local, zero API cost).
Optional fallback to Google Cloud Vision API via USE_VISION_API=1 env var.

Public interface is identical to the previous Vision-API-only version so
all callers (county, location, latlong, grid) need zero changes:

  detect_text_with_vision(pil_image, *, manager, page_num) -> annotations
  get_page_annotations(pdf_path, page_num, ...)            -> (annotations, pil_image)
  find_keyword_box(annotations, keywords)                  -> [x0,y0,x1,y1] | None

Annotation objects are _FakeAnnotation instances from utils.api_cache —
same type used by the disk cache, so Vision and Tesseract paths are
fully interchangeable and cache hits look identical to fresh OCR.
"""

import io
import logging
import os
import time

import pytesseract
from pytesseract import Output as TessOutput
from PIL import Image as PILImage

from ocr.preprocessing import preprocess_image
from pdf.pdf_manager import PDFDocumentManager
from utils.api_cache import _FakeAnnotation, _FakePoly, _FakeVertex, get_cache

log = logging.getLogger(__name__)

# Set USE_VISION_API=1 to use Google Cloud Vision instead of Tesseract
USE_VISION_API = os.environ.get("USE_VISION_API", "0") == "1"

# Tesseract config — PSM 3 = fully auto layout, OEM 1 = LSTM engine
_TESS_CONFIG = "--psm 3 --oem 1"

# ---------------------------------------------------------------------------
# Tesseract backend
# ---------------------------------------------------------------------------

def _tesseract_annotations(image: PILImage.Image) -> list:
    """
    Run Tesseract on a PIL image, return list of _FakeAnnotation objects:
      [0]  = full-page text (description = all words joined)
      [1:] = word-level tokens with bounding boxes

    Matches the structure of Google Vision text_annotations so all
    downstream consumers (find_keyword_box, _tokens_left_of, etc.) work
    unchanged.
    """
    try:
        data = pytesseract.image_to_data(
            image.convert("RGB"),
            output_type=TessOutput.DICT,
            config=_TESS_CONFIG,
        )
    except Exception as exc:
        log.warning("Tesseract OCR failed: %s", exc)
        return []

    word_anns   = []
    full_parts  = []

    for i, word in enumerate(data["text"]):
        if not word or not word.strip():
            continue
        conf = int(data["conf"][i])
        if conf < 0:          # Tesseract marks non-word rows as conf=-1
            continue
        x, y  = int(data["left"][i]),  int(data["top"][i])
        w, h  = int(data["width"][i]), int(data["height"][i])
        poly  = _FakePoly([
            _FakeVertex(x,     y    ),   # top-left
            _FakeVertex(x + w, y    ),   # top-right
            _FakeVertex(x + w, y + h),   # bottom-right
            _FakeVertex(x,     y + h),   # bottom-left
        ])
        word_anns.append(_FakeAnnotation(word.strip(), poly))
        full_parts.append(word.strip())

    if not word_anns:
        return []

    # Index 0 = full-page text block (Vision API convention)
    full_ann = _FakeAnnotation(" ".join(full_parts), word_anns[0].bounding_poly)
    return [full_ann] + word_anns


# ---------------------------------------------------------------------------
# Google Cloud Vision backend (opt-in via USE_VISION_API=1)
# ---------------------------------------------------------------------------

_vision_client = None
_RETRY_DELAYS  = [3, 6, 15]


class _FakeResponse:
    """Wraps annotation list to look like a real Vision API response."""
    def __init__(self, annotations):
        self.text_annotations     = annotations
        self.error                = type("_E", (), {"message": ""})()
        self.full_text_annotation = None


def _get_vision_client():
    global _vision_client
    if _vision_client is None:
        from google.cloud import vision
        _vision_client = vision.ImageAnnotatorClient()
    return _vision_client


def _reset_vision_client():
    global _vision_client
    _vision_client = None


def _vision_call_with_retry(image_bytes: bytes):
    from google.api_core.exceptions import ServiceUnavailable
    from google.cloud import vision
    last_exc = None
    for delay in [0] + _RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return _get_vision_client().document_text_detection(
                image=vision.Image(content=image_bytes)
            )
        except ServiceUnavailable as exc:
            last_exc = exc
            _reset_vision_client()
    raise last_exc


def _vision_annotations(image: PILImage.Image) -> list:
    """Call Google Vision API and return raw text_annotations list."""
    buf   = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    resp  = _vision_call_with_retry(buf.getvalue())
    if resp.error.message:
        log.warning("Vision API error: %s", resp.error.message)
        return []
    return resp.text_annotations or []


# ---------------------------------------------------------------------------
# Unified OCR entry point (with disk cache)
# ---------------------------------------------------------------------------

def _ocr_image(image: PILImage.Image) -> list:
    """
    OCR a PIL image via the configured backend (Tesseract or Vision API).
    Checks the disk cache first; writes result to cache on a miss.
    Returns a list of annotation objects (empty list on failure).
    """
    buf   = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    image_bytes = buf.getvalue()

    cache = get_cache()
    if cache is not None:
        cached = cache.get(image_bytes)
        if cached is not None:
            return cached          # already _FakeAnnotation objects

    if USE_VISION_API:
        annotations = _vision_annotations(image)
    else:
        annotations = _tesseract_annotations(image)

    if cache is not None and annotations:
        cache.put(image_bytes, annotations)

    return annotations


# ---------------------------------------------------------------------------
# Public interface (identical to old vision_api.py)
# ---------------------------------------------------------------------------

def detect_text_with_vision(
    pil_image: PILImage.Image,
    *,
    manager: PDFDocumentManager = None,
    page_num: int = None,
) -> list:
    """
    Preprocess and OCR a PIL image.
    Result is cached on `manager` so subsequent stages skip the OCR call.
    Returns annotation list (possibly empty).
    """
    cache_key = (page_num, "pre") if (manager and page_num is not None) else None
    if cache_key and cache_key in manager._ocr_cache:
        return manager._ocr_cache[cache_key]

    # get_page_annotations (called by the grid stage) also OCRs each page and
    # stores the result at the plain int key `page_num`.  If that entry exists,
    # reuse it rather than running Tesseract again.  For noisy 1914 scans each
    # Tesseract call takes 10–30 minutes, so skipping the duplicate call is the
    # single biggest throughput lever.
    if manager is not None and page_num is not None:
        prior = manager._ocr_cache.get(page_num)
        if isinstance(prior, tuple) and len(prior) == 2:   # (annotations, pil_image)
            anns = prior[0]
            if anns:                                        # non-empty → reuse
                if cache_key:
                    manager._ocr_cache[cache_key] = anns   # promote for future hits
                return anns

    processed   = preprocess_image(pil_image).convert("RGB")
    annotations = _ocr_image(processed)

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
    Render a PDF page and OCR it.
    Returns (annotations | None, pil_image | None).
    Result is cached on the manager to avoid duplicate OCR calls.
    """
    try:
        if manager is None:
            manager = PDFDocumentManager(
                pdf_path, pdf_bytes=pdf_bytes,
                resolution_multiplier=resolution_multiplier,
            )

        # Primary cache: int key (our own format).
        cached = manager._ocr_cache.get(page_num)
        if cached is not None:
            return cached

        # NOTE: we deliberately do NOT reuse the (page_num, "pre") cache set by
        # detect_text_with_vision().  That function is called from the location
        # stage via iter_pil_pages() which yields **1-indexed** page numbers, while
        # get_page_annotations() uses **0-indexed** page numbers.  Reusing the
        # pre-cache would therefore return:
        #   • the wrong page's annotations (off-by-one physical page), AND
        #   • pil_image=None (no PIL stored on that path)
        # causing county extraction to silently fail on every second-page attempt.
        # The deduplication benefit (saving one Vision API call) is not worth the
        # correctness risk; we always re-OCR here using the correct 0-indexed page.

        pil_image = manager.get_page_pil(page_num)
        if pil_image is None:
            return None, None

        annotations = _ocr_image(pil_image)
        result      = (annotations or None), pil_image
        manager._ocr_cache[page_num] = result
        return result

    except Exception as exc:
        src = pdf_path or ("bytes" if pdf_bytes else "manager")
        log.warning("OCR error (%s page %d): %s", src, page_num, exc)
        return None, None


def find_keyword_box(text_annotations, keywords: list) -> list | None:
    """
    Return [x_min, y_min, x_max, y_max] of the first annotation token
    whose text contains any of the given keywords (case-insensitive).
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

"""
Google Cloud Vision OCR helpers.
- Singleton client — one connection negotiation per process.
- All image data via BytesIO — no temp files, parallel-safe.
"""

import io

from google.cloud import vision
from PIL import Image as PILImage

from ocr.preprocessing import preprocess_image
from pdf.pdf_manager import PDFDocumentManager

# ── Singleton client ──────────────────────────────────────────────────────────
_client: vision.ImageAnnotatorClient | None = None


def _get_client() -> vision.ImageAnnotatorClient:
    global _client
    if _client is None:
        _client = vision.ImageAnnotatorClient()
    return _client


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pil_to_bytes(image: PILImage.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ── Public API ────────────────────────────────────────────────────────────────

def detect_text_with_vision(pil_image: PILImage.Image):
    """Preprocess PIL image in-memory → document_text_detection → annotations."""
    processed  = preprocess_image(pil_image).convert("RGB")
    image_bytes = _pil_to_bytes(processed)
    response = _get_client().document_text_detection(
        image=vision.Image(content=image_bytes)
    )
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
        response = _get_client().document_text_detection(
            image=vision.Image(content=image_bytes)
        )

        if response.error.message:
            return None, pil_image

        return (response.text_annotations or None), pil_image

    except Exception as exc:
        src = pdf_path or ("bytes" if pdf_bytes else "manager")
        print(f"Vision API error ({src} page {page_num}): {exc}")
        return None, None


def find_keyword_box(text_annotations, keywords: list[str]) -> list | None:
    """
    First annotation token matching any keyword → [x_min, y_min, x_max, y_max].
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

"""
Location (Section / Township / Range) extraction pipeline.

process_single_location(manager, output_dir, pdf_stem, logger) → dict
  {detected, page, section, township, range, raw_text, confidence,
   image_path, annotated_path}

Scans all pages (expected ≤2), stops at first page with grouped STR keywords.
"""

import io
import logging
import re
from pathlib import Path

from google.cloud import vision

from config import LOCATION_KEYWORDS, RESOLUTION_MULTIPLIER
from location.grouping import (
    choose_group,
    find_keywords_lists,
    get_unified_bounding_box,
)
from ocr.vision_api import detect_text_with_vision
from pdf.pdf_manager import PDFDocumentManager
from utils.io_utils import annotate_page


def _extract_str(raw: str) -> tuple[str, str, str]:
    sec = re.search(r"sec(?:tion)?\.?\s*(\d+)", raw, re.I)
    twp = re.search(r"t(?:ownship|wn|vp|wp)\.?\s*([\d]+\s*[NS]?)", raw, re.I)
    rng = re.search(r"r(?:ange|ge)\.?\s*([\d]+\s*[EW]?)", raw, re.I)
    return (
        sec.group(1).strip() if sec else "",
        twp.group(1).strip() if twp else "",
        rng.group(1).strip() if rng else "",
    )


def _ocr_crop(crop) -> str:
    buf = io.BytesIO()
    crop.convert("RGB").save(buf, format="PNG")
    client   = vision.ImageAnnotatorClient()
    response = client.text_detection(image=vision.Image(content=buf.getvalue()))
    texts    = response.text_annotations
    return texts[0].description.strip() if texts else ""


def process_single_location(
    manager: PDFDocumentManager,
    output_dir: Path,
    pdf_stem: str,
    logger: logging.Logger | None = None,
    extend_right: int = 500,
    padding_height: int = 50,
    section_right_extension: int = 200,
) -> dict:
    """
    Scans all pages for grouped Section/Township/Range keywords.
    Crops and saves the region on first match.
    """
    log = logger or logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "detected": False, "page": None,
        "section": "", "township": "", "range": "",
        "raw_text": "", "confidence": 0,
        "image_path": None, "annotated_path": None,
    }

    try:
        for page_num, pil_image in manager.iter_pil_pages():
            annotations = detect_text_with_vision(pil_image)
            if not annotations:
                continue

            sections, townships, ranges = find_keywords_lists(
                annotations, LOCATION_KEYWORDS, extend_right, padding_height
            )
            group = choose_group(sections, townships, ranges, min_overlap=0.5)
            if group is None:
                continue

            unified_box = get_unified_bounding_box(group, section_right_extension)
            if unified_box is None:
                continue

            pw, ph = pil_image.size
            x0, y0 = max(0, int(unified_box[0])), max(0, int(unified_box[1]))
            x1, y1 = min(pw, int(unified_box[2])), min(ph, int(unified_box[3]))
            if x1 <= x0 or y1 <= y0:
                continue

            crop = pil_image.crop((x0, y0, x1, y1))

            crop_path = output_dir / f"{pdf_stem}_page_{page_num:02d}_location_crop.png"
            crop.save(str(crop_path))

            ann_path  = output_dir / f"{pdf_stem}_page_{page_num:02d}_location_page.png"
            annotate_page(pil_image, (x0, y0, x1, y1),
                          color="blue", label="STR").save(str(ann_path))

            raw_text  = _ocr_crop(crop)
            sec, twp, rng = _extract_str(raw_text)
            found = sum(bool(v) for v in (sec, twp, rng))
            conf  = (found * 100) // 3

            log.info("Location — page %d  sec=%s  twp=%s  rng=%s  conf=%d",
                     page_num, sec, twp, rng, conf)
            result.update(
                detected=True, page=page_num,
                section=sec, township=twp, range=rng,
                raw_text=raw_text, confidence=conf,
                image_path=str(crop_path),
                annotated_path=str(ann_path),
            )
            return result

    except Exception as exc:
        log.error("Location extraction error: %s", exc, exc_info=True)
        result["error"] = str(exc)

    log.warning("No STR location found")
    return result

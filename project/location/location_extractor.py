"""
Location (Section / Township / Range) extraction pipeline.

process_single_location(manager, output_dir, pdf_stem, logger) -> dict
  {detected, page, section, township, range, raw_text, confidence,
   image_path, annotated_path}

Uses ONE Vision API call per page — keyword grouping and text extraction
both reuse the same annotations, eliminating the previous double-call.
"""

import logging
import re
from pathlib import Path

from config import LOCATION_KEYWORDS
from location.grouping import (
    choose_group,
    find_keywords_lists,
    get_unified_bounding_box,
)
from ocr.vision_api import detect_text_with_vision
from pdf.pdf_manager import PDFDocumentManager
from utils.io_utils import annotate_page


def _clean(v: str) -> str:
    """Collapse whitespace/newlines and strip."""
    return re.sub(r"\s+", "", v).strip() if v else ""


def _extract_str(raw: str) -> tuple[str, str, str]:
    # Section: 1-2 digit number after sec/section (validated to 1..36 below).
    sec_m = re.search(r"sec(?:tion)?\.?\s*(\d{1,2})\b", raw, re.I)
    # Township: 1-3 digits, optional N/S suffix (OCR often loses the letter
    # on old scans, so we keep it optional — false positives are filtered
    # later by the ≥2-valid-fields rule).
    twp_m = re.search(r"t(?:ownship|wn|vp|wp)\.?\s*(\d{1,3}(?:\s*[NS])?)", raw, re.I)
    # Range: 1-3 digits, optional E/W suffix (same reasoning as township).
    rng_m = re.search(r"r(?:ange|ge)\.?\s*(\d{1,3}(?:\s*[EW])?)", raw, re.I)

    sec = _clean(sec_m.group(1)) if sec_m else ""
    twp = _clean(twp_m.group(1)) if twp_m else ""
    rng = _clean(rng_m.group(1)) if rng_m else ""

    # Section sanity: 1..36 (PLSS sections)
    if sec:
        try:
            n = int(sec)
            if not (1 <= n <= 36):
                sec = ""
        except ValueError:
            sec = ""
    return sec, twp, rng


def _annotations_in_box(annotations, x0, y0, x1, y1) -> str:
    """
    Collect text tokens whose bounding-box centre falls within the crop region.
    Avoids a second Vision API call on the crop image.
    """
    tokens = []
    for ann in annotations[1:]:
        poly = ann.bounding_poly
        if not poly or not poly.vertices:
            continue
        try:
            xs = [v.x for v in poly.vertices]
            ys = [v.y for v in poly.vertices]
            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                tokens.append(ann.description)
        except Exception:
            continue
    return " ".join(tokens)


def process_single_location(
    manager: PDFDocumentManager,
    output_dir: Path,
    pdf_stem: str,
    logger: logging.Logger | None = None,
    extend_right: int = 500,
    padding_height: int = 50,
    section_right_extension: int = 200,
) -> dict:
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
            # Single Vision API call — reused for keyword grouping and text
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
            x0 = max(0, int(unified_box[0]))
            y0 = max(0, int(unified_box[1]))
            x1 = min(pw, int(unified_box[2]))
            y1 = min(ph, int(unified_box[3]))
            if x1 <= x0 or y1 <= y0:
                continue

            crop = pil_image.crop((x0, y0, x1, y1))

            crop_path = output_dir / f"{pdf_stem}_page_{page_num:02d}_location_crop.png"
            crop.save(str(crop_path))

            ann_path = output_dir / f"{pdf_stem}_page_{page_num:02d}_location_page.png"
            annotate_page(pil_image, (x0, y0, x1, y1),
                          color="blue", label="STR").save(str(ann_path))

            # Extract text from the already-fetched annotations within the crop box
            raw_text = _annotations_in_box(annotations, x0, y0, x1, y1)
            if not raw_text:
                raw_text = annotations[0].description.strip() if annotations else ""

            sec, twp, rng = _extract_str(raw_text)
            found = sum(bool(v) for v in (sec, twp, rng))
            conf  = (found * 100) // 3

            log.info("Location -- page %d  sec=%s  twp=%s  rng=%s  conf=%d",
                     page_num, sec, twp, rng, conf)
            # Require >=2 valid fields. Single-field hits are too noisy
            # (often false positives from random numbers near keywords).
            if found < 2:
                continue   # try next page

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

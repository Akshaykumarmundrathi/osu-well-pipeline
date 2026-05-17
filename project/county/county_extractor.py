"""
County extraction pipeline — two Gemini passes + fuzzy match.

process_single_county(manager, output_dir, pdf_stem, logger) -> dict
  {detected, page, name, pass1_result, pass2_result,
   fuzzy_score, confidence, image_path, annotated_path}

Tries page 0, then page 1 if county keyword not found on page 0.
"""

import logging
from pathlib import Path

from config import (
    COUNTY_KEYWORDS,
    COUNTY_LIST_CLEAN,
    COUNTY_MAP_CLEAN_TO_ORIGINAL,
    EXTEND_LEFT_PIXELS,
    EXTEND_RIGHT_PIXELS,
    FUZZY_MATCH_THRESHOLD,
    RETRY_CONFIDENCE_THRESHOLD,
    VERTICAL_PADDING_PIXELS,
)
from county.prompts import prompt_pass1, prompt_pass2, setup_gemini
from ocr.vision_api import find_keyword_box, get_page_annotations
from pdf.pdf_manager import PDFDocumentManager
from utils.io_utils import annotate_page

# -- Fuzzy match ---------------------------------------------------------------

try:
    from rapidfuzz import process as _rfuzz

    def _fuzzy_match(text: str) -> tuple[str, int]:
        clean = text.lower().replace("county", "").strip()
        m = _rfuzz.extractOne(clean, COUNTY_LIST_CLEAN,
                               score_cutoff=FUZZY_MATCH_THRESHOLD)
        if m:
            name, score, _ = m
            return COUNTY_MAP_CLEAN_TO_ORIGINAL.get(name, ""), int(score)
        return "", 0

except ImportError:
    import difflib

    def _fuzzy_match(text: str) -> tuple[str, int]:
        clean   = text.lower().replace("county", "").strip()
        matches = difflib.get_close_matches(
            clean, COUNTY_LIST_CLEAN, n=1, cutoff=FUZZY_MATCH_THRESHOLD / 100
        )
        if matches:
            ratio = difflib.SequenceMatcher(None, clean, matches[0]).ratio()
            return COUNTY_MAP_CLEAN_TO_ORIGINAL.get(matches[0], ""), int(ratio * 100)
        return "", 0


# -- Gemini singleton ----------------------------------------------------------

_GEMINI = None


def _get_gemini():
    global _GEMINI
    if _GEMINI is None:
        _GEMINI = setup_gemini()   # (flash, pro, gen_config)
    return _GEMINI


def _gemini_call(model, cfg, prompt: str, pil_image) -> str:
    resp = model.generate_content([prompt, pil_image], generation_config=cfg)
    return resp.text.strip() if resp.text else ""


# -- Core logic ----------------------------------------------------------------

def _try_page(
    manager: PDFDocumentManager,
    page_num: int,
    output_dir: Path,
    pdf_stem: str,
    log: logging.Logger,
) -> tuple[dict | None, bool]:
    """
    Attempt county extraction on a single page.
    Returns (result_dict, keyword_found).
    result_dict is None if keyword not found on this page.
    """
    annotations, pil_image = get_page_annotations(
        manager=manager, page_num=page_num
    )
    if pil_image is None:
        log.warning("Could not render page %d", page_num + 1)
        return None, False

    if not annotations:
        log.warning("No OCR annotations on page %d", page_num + 1)
        return None, False

    kw_box = find_keyword_box(annotations, COUNTY_KEYWORDS)
    if kw_box is None:
        log.debug("County keyword not on page %d", page_num + 1)
        return None, False   # caller should try next page

    pw, ph  = pil_image.size
    x0, y0, x1, y1 = kw_box
    crop_box = (
        max(0,  int(x0 - EXTEND_LEFT_PIXELS)),
        max(0,  int(y0 - VERTICAL_PADDING_PIXELS)),
        min(pw, int(x1 + EXTEND_RIGHT_PIXELS)),
        min(ph, int(y1 + VERTICAL_PADDING_PIXELS)),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        log.error("Invalid crop box on page %d: %s", page_num + 1, crop_box)
        return {"error": "invalid_crop"}, True

    crop = pil_image.crop(crop_box)

    crop_path = output_dir / f"{pdf_stem}_page_{page_num+1:02d}_county_crop.png"
    ann_path  = output_dir / f"{pdf_stem}_page_{page_num+1:02d}_county_page.png"
    crop.save(str(crop_path))
    annotate_page(pil_image, crop_box, color="green",
                  label="County").save(str(ann_path))

    result = {
        "detected": False, "page": page_num + 1,
        "name": "", "pass1_result": "", "pass2_result": "",
        "fuzzy_score": 0, "confidence": 0,
        "image_path": str(crop_path), "annotated_path": str(ann_path),
    }

    # -- Gemini Pass 1 (Flash) -------------------------------------------------
    try:
        flash, pro, cfg = _get_gemini()

        raw1 = _gemini_call(flash, cfg, prompt_pass1, crop)
        result["pass1_result"] = raw1
        log.debug("Pass 1 raw: %r", raw1)

        if raw1.lower() != "not detected.":
            name, score = _fuzzy_match(raw1)
            if name and score >= RETRY_CONFIDENCE_THRESHOLD:
                log.info("County (P1) = %r  score=%d", name, score)
                result.update(detected=True, name=name,
                               fuzzy_score=score, confidence=score)
                return result, True

        # -- Gemini Pass 2 (Pro) -----------------------------------------------
        raw2 = _gemini_call(pro, cfg, prompt_pass2, crop)
        result["pass2_result"] = raw2
        log.debug("Pass 2 raw: %r", raw2)

        if raw2.lower() != "not detected.":
            name, score = _fuzzy_match(raw2)
            if name and score >= FUZZY_MATCH_THRESHOLD:
                log.info("County (P2) = %r  score=%d", name, score)
                result.update(detected=True, name=name,
                               fuzzy_score=score, confidence=score)
                return result, True

        log.warning("County not matched after 2 passes (page %d)", page_num + 1)
        result["error"] = "no_match"

    except Exception as exc:
        log.error("Gemini failed on page %d: %s", page_num + 1, exc, exc_info=True)
        result["error"] = str(exc)

    return result, True


def process_single_county(
    manager: PDFDocumentManager,
    output_dir: Path,
    pdf_stem: str,
    logger: logging.Logger | None = None,
) -> dict:
    """
    Tries page 0 first; if county keyword not found, tries page 1.
    Returns structured extraction dict.
    """
    log = logger or logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)

    total_pages = manager.page_count()

    for page_num in range(min(total_pages, 2)):   # try up to 2 pages
        r, found_keyword = _try_page(manager, page_num, output_dir, pdf_stem, log)
        if found_keyword:
            return r   # keyword was on this page (result may still be "no_match")

    log.warning("County keyword not found on any of the first 2 pages")
    return {
        "detected": False, "page": None,
        "name": "", "pass1_result": "", "pass2_result": "",
        "fuzzy_score": 0, "confidence": 0,
        "image_path": None, "annotated_path": None,
        "error": "keyword_not_found",
    }

"""
County extraction pipeline — structural OCR-anchor + Gemini fallback.

process_single_county(manager, output_dir, pdf_stem, logger) -> dict
  {detected, page, name, pass1_result, pass2_result,
   fuzzy_score, confidence, image_path, annotated_path, error?}

Strategy (in order)
-------------------
1. **OCR direct-anchor**: collect tokens immediately to the LEFT of the
   "County" keyword and fuzzy-match against the 77-county list. Most
   forms write "<NAME> County" with the name within 1-3 tokens; this
   alone resolves the majority of records and costs zero Gemini calls.
2. **Gemini Pass 1 (Flash)** on the keyword-anchored crop (cheap model).
   Accept at confidence >= RETRY_CONFIDENCE_THRESHOLD (95).
3. **Gemini Pass 2 (Pro)** on the same crop, accept at FUZZY_MATCH_THRESHOLD (86).

Retry path may invoke this with `crop_scale > 1.0` to widen the crop or
`full_page=True` to send the entire page to Pro.
"""

import logging
from pathlib import Path

from config import (
    COUNTY_KEYWORDS,
    COUNTY_LIST_CLEAN,
    COUNTY_MAP_CLEAN_TO_ORIGINAL,
    COUNTY_RETRY_CROP_SCALE,
    EXTEND_LEFT_PIXELS,
    EXTEND_RIGHT_PIXELS,
    FUZZY_MATCH_THRESHOLD,
    MAX_COUNTY_PAGES,
    RETRY_CONFIDENCE_THRESHOLD,
    VERTICAL_PADDING_PIXELS,
)
from county.prompts import prompt_pass1, prompt_pass2, setup_gemini, _rate_limited_generate
from ocr.vision_api import find_keyword_box, get_page_annotations
from pdf.pdf_manager import PDFDocumentManager
from utils.io_utils import annotate_page


# -- Fuzzy match ---------------------------------------------------------------

try:
    from rapidfuzz import process as _rfuzz

    def _fuzzy_match(text: str) -> tuple[str, int]:
        """Best fuzzy match in COUNTY_LIST_CLEAN above FUZZY_MATCH_THRESHOLD."""
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
        """difflib fallback when rapidfuzz isn't installed."""
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
    """Lazy-construct the (flash, pro, gen_config) triple once per process."""
    global _GEMINI
    if _GEMINI is None:
        _GEMINI = setup_gemini()
    return _GEMINI


def _gemini_call(model, cfg, prompt: str, pil_image) -> str:
    """Send (prompt, image) to Gemini with rate-limiting and return the stripped text reply.

    Accessing resp.text raises ValueError when Gemini blocks the response for
    safety reasons — treat that as a clean "not detected" rather than an exception
    so county records don't get classified as error:exception.
    """
    resp = _rate_limited_generate(model, prompt, pil_image, cfg)
    try:
        text = resp.text
        return text.strip() if text else ""
    except ValueError:
        return "Not detected."  # safety-blocked — treat as no match


# -- Structural anchor (zero-cost county detection) ----------------------------

def _tokens_left_of(annotations, kw_box, max_tokens: int = 4,
                    same_line_tol: int = 35) -> str:
    """
    Return the text of up to `max_tokens` annotation tokens that sit
    immediately to the LEFT of `kw_box` and share its vertical band
    (within +/- same_line_tol pixels of the keyword's y-centre).

    Vertical tolerance is generous (35 px) to catch cursive / handwritten
    county names that wobble slightly off the baseline of the printed
    "County" keyword.
    """
    if kw_box is None or not annotations:
        return ""
    kx0, ky0, kx1, ky1 = kw_box
    ky_mid = (ky0 + ky1) / 2

    candidates = []
    for ann in annotations[1:]:
        poly = ann.bounding_poly
        if not poly or not poly.vertices:
            continue
        try:
            xs = [v.x for v in poly.vertices]
            ys = [v.y for v in poly.vertices]
            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)
            x_max = max(xs)
        except Exception:
            continue
        if x_max >= kx0:                       # not to the left
            continue
        if abs(cy - ky_mid) > same_line_tol:   # not on the same line
            continue
        candidates.append((cx, ann.description))

    # Sort by x ascending; take the rightmost `max_tokens` (those nearest the keyword).
    candidates.sort(key=lambda t: t[0])
    tail = candidates[-max_tokens:]
    return " ".join(desc for _, desc in tail)


def _try_structural_anchor(annotations, kw_box, log) -> tuple[str, int]:
    """
    Fuzzy-match the tokens left of the County keyword. Returns
    (matched_name, fuzzy_score) or ("", 0) if below threshold.
    """
    text = _tokens_left_of(annotations, kw_box, max_tokens=4)
    if not text:
        return "", 0
    name, score = _fuzzy_match(text)
    log.debug("County structural anchor: text=%r -> %r score=%d",
              text, name, score)
    return name, score


# -- Per-page logic ------------------------------------------------------------

def _try_page(
    manager: PDFDocumentManager,
    page_num: int,
    output_dir: Path,
    pdf_stem: str,
    log: logging.Logger,
    crop_scale: float = 1.0,
    full_page_gemini: bool = False,
) -> tuple[dict | None, bool]:
    """
    Attempt county extraction on a single page.
    Returns (result_dict, keyword_found).
    result_dict is None if the County keyword isn't on this page.

    Parameters:
      crop_scale         multiplier on the keyword crop margins (retry uses >1)
      full_page_gemini   if True, sends the WHOLE page to Gemini Pro (retry)
    """
    annotations, pil_image = get_page_annotations(
        manager=manager, page_num=page_num,
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
        return None, False

    # -- Strategy 1: zero-cost structural anchor -----------------------------
    name, score = _try_structural_anchor(annotations, kw_box, log)
    if name and score >= RETRY_CONFIDENCE_THRESHOLD:
        log.info("County (anchor) = %r  score=%d", name, score)
        return {
            "detected": True, "page": page_num + 1,
            "name": name, "pass1_result": "", "pass2_result": "",
            "fuzzy_score": score, "confidence": score,
            "image_path": None, "annotated_path": None,
            "method": "anchor",
        }, True

    # -- Strategy 2/3: Gemini on crop or full page ---------------------------
    pw, ph = pil_image.size
    x0, y0, x1, y1 = kw_box
    left_pad   = int(EXTEND_LEFT_PIXELS      * crop_scale)
    right_pad  = int(EXTEND_RIGHT_PIXELS     * crop_scale)
    vert_pad   = int(VERTICAL_PADDING_PIXELS * crop_scale)
    crop_box = (
        max(0,  int(x0 - left_pad)),
        max(0,  int(y0 - vert_pad)),
        min(pw, int(x1 + right_pad)),
        min(ph, int(y1 + vert_pad)),
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
        "method": "",
    }

    # If structural anchor produced a weaker (but plausible) match, remember it.
    anchor_name, anchor_score = name, score

    try:
        flash, pro, cfg = _get_gemini()

        if full_page_gemini:
            # Skip Flash, send the whole page directly to Pro.
            raw2 = _gemini_call(pro, cfg, prompt_pass2, pil_image)
            result["pass2_result"] = raw2
            if raw2.lower() != "not detected.":
                n2, s2 = _fuzzy_match(raw2)
                if n2 and s2 >= FUZZY_MATCH_THRESHOLD:
                    result.update(detected=True, name=n2,
                                  fuzzy_score=s2, confidence=s2,
                                  method="full_page_pro")
                    return result, True
        else:
            # -- Pass 1 (Flash) on the crop ----------------------------------
            raw1 = _gemini_call(flash, cfg, prompt_pass1, crop)
            result["pass1_result"] = raw1
            if raw1.lower() != "not detected.":
                n1, s1 = _fuzzy_match(raw1)
                if n1 and s1 >= RETRY_CONFIDENCE_THRESHOLD:
                    log.info("County (P1) = %r  score=%d", n1, s1)
                    result.update(detected=True, name=n1,
                                  fuzzy_score=s1, confidence=s1,
                                  method="flash")
                    return result, True

            # -- Pass 2 (Pro) on the crop ------------------------------------
            raw2 = _gemini_call(pro, cfg, prompt_pass2, crop)
            result["pass2_result"] = raw2
            if raw2.lower() != "not detected.":
                n2, s2 = _fuzzy_match(raw2)
                if n2 and s2 >= FUZZY_MATCH_THRESHOLD:
                    log.info("County (P2) = %r  score=%d", n2, s2)
                    result.update(detected=True, name=n2,
                                  fuzzy_score=s2, confidence=s2,
                                  method="pro")
                    return result, True

        # -- Fallback: accept weaker structural anchor if above min threshold ----
        if anchor_name and anchor_score >= FUZZY_MATCH_THRESHOLD:
            log.info("County (anchor-weak) = %r  score=%d",
                     anchor_name, anchor_score)
            result.update(detected=True, name=anchor_name,
                          fuzzy_score=anchor_score, confidence=anchor_score,
                          method="anchor_weak")
            return result, True

        log.warning("County not matched on page %d", page_num + 1)
        result["error"] = "no_match"

    except Exception as exc:
        log.error("Gemini failed on page %d: %s", page_num + 1, exc, exc_info=True)
        result["error"] = str(exc)

    return result, True


# -- Public entry point --------------------------------------------------------

def process_single_county(
    manager: PDFDocumentManager,
    output_dir: Path,
    pdf_stem: str,
    logger: logging.Logger | None = None,
    crop_scale: float = 1.0,
    full_page_gemini: bool = False,
    max_pages: int | None = None,
) -> dict:
    """
    Iterate the first `max_pages` (default: MAX_COUNTY_PAGES) until the
    County keyword is located. Then run structural-anchor + Gemini.

    Retry callers can pass:
      crop_scale=COUNTY_RETRY_CROP_SCALE  (wider crop, helps OCR-quality issues)
      full_page_gemini=True               (skip crop, feed whole page to Pro)
      max_pages=<int>                     (override default 2-page limit)
    """
    cap = max_pages if max_pages is not None else getattr(
        process_single_county, "_max_pages_override", MAX_COUNTY_PAGES,
    )

    log = logger or logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)

    total_pages = manager.page_count()
    for page_num in range(min(total_pages, cap)):
        r, found_keyword = _try_page(
            manager, page_num, output_dir, pdf_stem, log,
            crop_scale=crop_scale, full_page_gemini=full_page_gemini,
        )
        if found_keyword:
            return r

    log.warning("County keyword not found on first %d page(s)", cap)
    return {
        "detected": False, "page": None,
        "name": "", "pass1_result": "", "pass2_result": "",
        "fuzzy_score": 0, "confidence": 0,
        "image_path": None, "annotated_path": None,
        "error": "keyword_not_found",
    }

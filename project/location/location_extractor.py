"""
Location (Section / Township / Range) extraction pipeline.

process_single_location(manager, output_dir, pdf_stem, logger) -> dict
  {detected, page, section, township, range, raw_text, confidence,
   image_path, annotated_path, error?}

Strategy
--------
1. OCR the page (cached on the manager so other stages reuse).
2. Try grouped-keyword extraction: align section + township + range
   on the same line (vertical-overlap pairing).
3. If grouping fails, fall back to per-keyword extraction: for each
   detected `sec` / `twp` / `rge` token, regex the text immediately to
   its right and combine the three results.
4. Accept only when >=2 of the 3 fields are populated. The section is
   range-validated to 1..36 (PLSS sections).
"""

import logging
import re
from pathlib import Path

from config import (
    LOCATION_KEYWORDS, LOCATION_MIN_OVERLAP, LOCATION_MIN_OVERLAP_RETRY,
)
from location.grouping import (
    choose_group,
    find_keywords_lists,
    get_unified_bounding_box,
)
from ocr.vision_api import detect_text_with_vision
from pdf.pdf_manager import PDFDocumentManager
from utils.io_utils import annotate_page


# -- Regex helpers -------------------------------------------------------------

def _clean(v: str) -> str:
    """Collapse whitespace/newlines and strip."""
    return re.sub(r"\s+", "", v).strip() if v else ""


_SEC_RE = re.compile(r"sec(?:tion)?\.?\s*(\d{1,2})\b", re.I)
_TWP_RE = re.compile(r"t(?:ownship|wn|vp|wp)\.?\s*(\d{1,3}(?:\s*[NS])?)", re.I)
_RNG_RE = re.compile(r"r(?:ange|ge)\.?\s*(\d{1,3}(?:\s*[EW])?)", re.I)
# Bare-number regex for per-keyword right-side extraction.
_NUM_RE = re.compile(r"(\d{1,3})\s*([NSEW])?", re.I)


def _validate_section(s: str) -> str:
    """Return s only if it parses as a PLSS section (1..36)."""
    if not s:
        return ""
    try:
        n = int(s)
        return s if 1 <= n <= 36 else ""
    except ValueError:
        return ""


# Oklahoma townships max out around 29N / 8S; ranges around 26E / 23W. Anything
# beyond 50 in either is OCR noise (e.g. "191" from concatenated tokens).
_TWPRNG_MAX = 50


def _validate_twprng(v: str) -> str:
    """Bound township/range numeric part to 1..50; reject obvious OCR noise."""
    if not v:
        return ""
    m = re.match(r"(\d{1,3})\s*([NSEWnsew]?)\b", v)
    if not m:
        return ""
    try:
        n = int(m.group(1))
    except ValueError:
        return ""
    if not (1 <= n <= _TWPRNG_MAX):
        return ""
    suffix = m.group(2).upper()
    return f"{n}{suffix}" if suffix else str(n)


def _extract_str(raw: str) -> tuple[str, str, str]:
    """Pull (section, township, range) values from a free-text blob."""
    sec_m = _SEC_RE.search(raw)
    twp_m = _TWP_RE.search(raw)
    rng_m = _RNG_RE.search(raw)

    sec = _validate_section(_clean(sec_m.group(1)) if sec_m else "")
    twp = _validate_twprng(_clean(twp_m.group(1)) if twp_m else "")
    rng = _validate_twprng(_clean(rng_m.group(1)) if rng_m else "")
    return sec, twp, rng


def _annotations_in_box(annotations, x0, y0, x1, y1) -> str:
    """Join text tokens whose bbox centre lies inside (x0,y0,x1,y1)."""
    tokens = []
    for ann in annotations[1:]:
        poly = ann.bounding_poly
        if not poly or not poly.vertices:
            continue
        try:
            xs = [v.x for v in poly.vertices]
            ys = [v.y for v in poly.vertices]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                tokens.append(ann.description)
        except Exception:
            continue
    return " ".join(tokens)


def _per_keyword_extract(annotations, keyword_boxes: dict, page_w: int,
                         right_extend: int = 500) -> tuple[str, str, str]:
    """
    Fallback when keyword grouping fails. For each of section / township /
    range, grab the FIRST keyword box, extend right by `right_extend`,
    collect tokens, and pick the first plausible number (with optional
    N/S or E/W). Returns (sec, twp, rng).
    """
    def _first_value(boxes: list) -> str:
        if not boxes:
            return ""
        x0, y0, x1, y1 = boxes[0]
        text = _annotations_in_box(annotations, x1, y0, x1 + right_extend, y1)
        # Strip the keyword itself if it leaks in.
        text = re.sub(r"^\s*\S+\s*", "", text).strip()
        m = _NUM_RE.search(text)
        if not m:
            return ""
        digits, suffix = m.group(1), (m.group(2) or "").upper()
        return f"{digits}{suffix}" if suffix else digits

    sec = _validate_section(_first_value(keyword_boxes.get("section", [])))
    twp = _validate_twprng(_first_value(keyword_boxes.get("township", [])))
    rng = _validate_twprng(_first_value(keyword_boxes.get("range",    [])))
    return sec, twp, rng


# -- Public entry point --------------------------------------------------------

def process_single_location(
    manager: PDFDocumentManager,
    output_dir: Path,
    pdf_stem: str,
    logger: logging.Logger | None = None,
    extend_right: int = 500,
    padding_height: int = 50,
    section_right_extension: int = 200,
    min_overlap: float | None = None,
) -> dict:
    """
    Scan pages for section/township/range. Try grouped pairing first;
    fall back to per-keyword right-side extraction. Returns the result
    dict with `detected` True only if >=2 of the 3 fields are populated.

    `min_overlap` overrides the strict default (config.LOCATION_MIN_OVERLAP).
    Retry path passes the loose value for a second chance.
    """
    log = logger or logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)

    overlap = min_overlap if min_overlap is not None else LOCATION_MIN_OVERLAP

    result = {
        "detected": False, "page": None,
        "section": "", "township": "", "range": "",
        "raw_text": "", "confidence": 0,
        "image_path": None, "annotated_path": None,
    }

    try:
        for page_num, pil_image in manager.iter_pil_pages():
            annotations = detect_text_with_vision(
                pil_image, manager=manager, page_num=page_num,
            )
            if not annotations:
                continue

            sections, townships, ranges = find_keywords_lists(
                annotations, LOCATION_KEYWORDS, extend_right, padding_height,
            )

            sec = twp = rng = ""
            raw_text = ""
            crop_box = None

            # -- Strategy 1: grouped extraction ----------------------------
            group = choose_group(sections, townships, ranges,
                                 min_overlap=overlap)
            if group is not None:
                unified_box = get_unified_bounding_box(group, section_right_extension)
                if unified_box is not None:
                    pw, ph = pil_image.size
                    x0 = max(0, int(unified_box[0]))
                    y0 = max(0, int(unified_box[1]))
                    x1 = min(pw, int(unified_box[2]))
                    y1 = min(ph, int(unified_box[3]))
                    if x1 > x0 and y1 > y0:
                        crop_box = (x0, y0, x1, y1)
                        raw_text = _annotations_in_box(annotations, x0, y0, x1, y1)
                        sec, twp, rng = _extract_str(raw_text)

            found = sum(bool(v) for v in (sec, twp, rng))

            # -- Strategy 2: per-keyword fallback --------------------------
            if found < 2 and (sections or townships or ranges):
                p_sec, p_twp, p_rng = _per_keyword_extract(
                    annotations,
                    {"section": sections, "township": townships, "range": ranges},
                    pil_image.size[0], right_extend=extend_right,
                )
                # Merge: keep existing non-empty values, fill the rest.
                sec = sec or p_sec
                twp = twp or p_twp
                rng = rng or p_rng
                found = sum(bool(v) for v in (sec, twp, rng))
                if not raw_text:
                    raw_text = f"sec={sec} twp={twp} rng={rng}"

            if found < 2:
                continue

            conf = (found * 100) // 3

            # Save crop + annotated page when we have a crop box.
            crop_path = ann_path = None
            if crop_box is not None:
                x0, y0, x1, y1 = crop_box
                crop = pil_image.crop(crop_box)
                crop_path = output_dir / f"{pdf_stem}_page_{page_num:02d}_location_crop.png"
                crop.save(str(crop_path))
                ann_path  = output_dir / f"{pdf_stem}_page_{page_num:02d}_location_page.png"
                annotate_page(pil_image, crop_box,
                              color="blue", label="STR").save(str(ann_path))

            log.info("Location -- page %d  sec=%s  twp=%s  rng=%s  conf=%d",
                     page_num, sec, twp, rng, conf)
            result.update(
                detected=True, page=page_num,
                section=sec, township=twp, range=rng,
                raw_text=raw_text, confidence=conf,
                image_path=str(crop_path) if crop_path else None,
                annotated_path=str(ann_path) if ann_path else None,
            )
            return result

    except Exception as exc:
        log.error("Location extraction error: %s", exc, exc_info=True)
        result["error"] = str(exc)
        return result

    # No STR found — flag as retriable so dispatcher can rerun with loose
    # min_overlap.
    log.warning("No STR location found")
    result["error"] = "not_found"
    return result


# Convenience constants for retry callers.
RETRY_MIN_OVERLAP = LOCATION_MIN_OVERLAP_RETRY

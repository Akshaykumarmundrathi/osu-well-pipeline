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

try:
    import numpy as _np
except ImportError:
    _np = None

try:
    from PIL import ImageEnhance as _PILEnhance
    from PIL import Image as _PIL
except ImportError:
    _PILEnhance = None
    _PIL = None

from config import (
    ILLEGIBLE_WORD_THRESHOLD,
    LOCATION_KEYWORDS, LOCATION_MIN_OVERLAP, LOCATION_MIN_OVERLAP_RETRY,
)
from location.grouping import (
    choose_group,
    find_keywords_lists,
    get_unified_bounding_box,
)
from ocr.quadrant_extractor import extract_quadrant
from ocr.vision_api import detect_text_with_vision
from pdf.pdf_manager import PDFDocumentManager
from utils.io_utils import annotate_page


# -- Regex helpers -------------------------------------------------------------

def _clean(v: str) -> str:
    """Collapse whitespace/newlines and strip."""
    return re.sub(r"\s+", "", v).strip() if v else ""


# Separator character class used between a field label and its value.
# Handles: period, dash/en-dash, space, newline — all observed in real forms.
_SEP = r"[\.\-\s]"

# Section: "Section 19", "SEC 12", "SEC-12", "Sec. 18", "Sec - 12", "Sect 14"
_SEC_RE = re.compile(rf"\bsec(?:tion|t)?{_SEP}*(\d{{1,2}})\b", re.I)

# Township variants observed across all 13 collections:
#   Variant A/B/C: "Township 18N", "TWP 23N", "Twp. 18", "tvp 18N"
#   Variant D:     "T - 23N"  (bare T label, common on early/transition forms)
#   Requires at least one separator after bare "T" to avoid false matches on
#   English words ("the", "to", "type" all fail because [\.\-\s]+ needs ≥1 sep).
_TWP_RE = re.compile(
    rf"\bt(?:ownship|wn|vp|wp)?{_SEP}+(\d{{1,3}}(?:{_SEP}*[NS])?)",
    re.I,
)

# Range variants observed across all 13 collections:
#   Variant A/B/C: "Range 10W", "RGE 10W", "Rge. 8E"
#   Variant D:     "R - 10W"  (bare R label)
#   Same separator-required guard as township.
_RNG_RE = re.compile(
    rf"\br(?:ange|ge)?{_SEP}+(\d{{1,3}}(?:{_SEP}*[EW])?)",
    re.I,
)

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
    """
    Pull (section, township, range) values from a free-text blob.

    Handles all observed Oklahoma form label variants (Collections 1–13):
      A) "Section 19  Township 18N  Range 12W"   — full keyword
      B) "SECTION SW/4 of 13 TOWNSHIP 18 RANGE 11" — 'of N' fallback for quad prefix
      C) "Sec. 18  Twp. 18  Range 7"             — abbreviated with period
      D) "SEC - 12  T - 23N  R - 10W"            — dash-separated labels (early forms)
      E) "SEC  TWP  RGE  Well No." layout         — W from "Well" rejected by \b guard
    """
    sec_m = _SEC_RE.search(raw)
    twp_m = _TWP_RE.search(raw)
    rng_m = _RNG_RE.search(raw)

    sec = _validate_section(_clean(sec_m.group(1)) if sec_m else "")
    twp = _validate_twprng(_clean(twp_m.group(1)) if twp_m else "")
    rng = _validate_twprng(_clean(rng_m.group(1)) if rng_m else "")

    # Layout B fallback: "SECTION SW/4 of 13" — section number follows "of".
    # Only activate when a "section" keyword is present but no digit immediately
    # followed — avoids false positives from unrelated "of" occurrences.
    if not sec and re.search(r"\bsec(?:tion)?\b", raw, re.I):
        of_m = re.search(r"\bof\s+(\d{1,2})\b", raw, re.I)
        if of_m:
            sec = _validate_section(of_m.group(1))

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


# -- Gemini STR fallback (Strategy 3) -----------------------------------------
# Mirrors the original phaseI.py / OSU_WELL_CHECKPOINT1.py approach:
# preprocess the crop → send to Gemini Flash → parse structured response.
# Activated only when OCR-regex and per-keyword extraction both yield < 2 fields.

_STR_GEMINI_PROMPT = """\
Oklahoma oil/gas well record image. Extract ONLY:

- Section: <number 1-36, or Not detected>
- Township: <number + N or S, e.g. 18N, or Not detected>
- Range: <number + E or W, e.g. 8E, or Not detected>

Examples
  "Section 18  Township 21N  Range 8E"  →  Section: 18 / Township: 21N / Range: 8E
  "Sec 5  T 25S  R 15E"                 →  Section: 5  / Township: 25S / Range: 15E

Reply with ONLY the three dash-lines above, nothing else."""


def _preprocess_for_gemini(pil_img):
    """
    Greyscale → contrast × 2 → brightness × 1.2 → binarize.
    Matches the preprocessing in the original phaseI.py / checkpoint code.
    Returns a PIL Image (grayscale binarized).
    """
    if _PILEnhance is None or _np is None:
        return pil_img
    try:
        import cv2 as _cv2
    except ImportError:
        _cv2 = None
    img = pil_img.convert("L")
    img = _PILEnhance.Contrast(img).enhance(2.0)
    img = _PILEnhance.Brightness(img).enhance(1.2)
    if _cv2 is not None:
        arr = _np.array(img)
        _, arr = _cv2.threshold(arr, 128, 255, _cv2.THRESH_BINARY)
        img = _PIL.fromarray(arr)
    return img


def _gemini_str_from_crop(pil_crop, logger) -> tuple[str, str, str]:
    """
    Send a preprocessed STR crop to Gemini Flash and extract
    (section, township, range).

    Returns ("", "", "") silently on any failure so the caller can decide
    whether to accept the partial result or fall through to 'not_found'.
    Requires GOOGLE_API_KEY; if unset the function returns empty strings
    without raising.
    """
    try:
        # Lazy import avoids circular deps and keeps startup fast.
        from county.prompts import setup_gemini, _rate_limited_generate
        flash, _, gen_cfg = setup_gemini()
        img = _preprocess_for_gemini(pil_crop)
        resp = _rate_limited_generate(flash, _STR_GEMINI_PROMPT, img, gen_cfg)
        try:
            raw = resp.text.strip() if (resp and hasattr(resp, "text")) else ""
        except ValueError:
            return "", "", ""

        def _parse_field(pattern: str) -> str:
            m = re.search(pattern, raw, re.I)
            if not m:
                return ""
            v = m.group(1).strip()
            return "" if "not" in v.lower() else v

        sec = _validate_section(_parse_field(r"Section\s*[:\-]\s*([^\n/]+)"))
        twp = _validate_twprng(_parse_field(r"Township\s*[:\-]\s*([^\n/]+)"))
        rng = _validate_twprng(_parse_field(r"Range\s*[:\-]\s*([^\n/]+)"))
        logger.info(
            "Location Gemini fallback: sec=%r twp=%r rng=%r  (raw=%r)",
            sec, twp, rng, raw[:80],
        )
        return sec, twp, rng
    except Exception as exc:
        logger.debug("Location Gemini fallback skipped: %s", exc)
        return "", "", ""


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
        "quadrant_pdf": "", "quadrant_db": "",
        "quadrant_row": "", "quadrant_col": "",
        "quadrant_confidence": 0,
    }

    try:
        for page_num, pil_image in manager.iter_pil_pages():
            annotations = detect_text_with_vision(
                pil_image, manager=manager, page_num=page_num,
            )
            if not annotations:
                continue

            # Fast illegibility guard: if Tesseract returned fewer than
            # ILLEGIBLE_WORD_THRESHOLD word tokens the page is almost certainly
            # handwritten beyond what OCR can decode.  Skipping the grouping
            # and keyword-search saves 30-90s of wasted computation per page.
            word_count = len(annotations) - 1   # index 0 is full-page blob
            if word_count < ILLEGIBLE_WORD_THRESHOLD:
                log.debug("Page %d: only %d words from OCR — skipping (illegible)",
                          page_num, word_count)
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
                        # Extended box: wider + taller to capture quadrant labels
                        # (they appear in the same legal description block)
                        ext = 250
                        ex0 = max(0,  x0 - ext)
                        ey0 = max(0,  y0 - ext)
                        ex1 = min(pw, x1 + ext)
                        ey1 = min(ph, y1 + ext)
                        raw_text = _annotations_in_box(annotations, ex0, ey0, ex1, ey1)
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

            # -- Strategy 3: Gemini on the crop (matches original phaseI.py) --
            # When OCR regex + per-keyword both failed to yield ≥2 fields,
            # send the binarized crop to Gemini Flash — same approach the
            # original monolithic pipeline used before the modular rewrite.
            # Only fires when a crop was found (Strategy 1 produced a bbox)
            # and GOOGLE_API_KEY is configured.
            if found < 2 and crop_box is not None:
                g_sec, g_twp, g_rng = _gemini_str_from_crop(
                    pil_image.crop(crop_box), log,
                )
                sec = sec or g_sec
                twp = twp or g_twp
                rng = rng or g_rng
                found = sum(bool(v) for v in (sec, twp, rng))
                if found > 0 and raw_text == "":
                    raw_text = f"gemini: sec={sec} twp={twp} rng={rng}"

            if found < 2:
                continue

            # Confidence: 3-strategy scoring.
            # OCR regex (strat 1/2) → up to 100; Gemini fallback (strat 3) → 75 cap.
            gemini_used = raw_text.startswith("gemini:")
            conf = min(75, (found * 100) // 3) if gemini_used else (found * 100) // 3

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

            # -- Quadrant label extraction from the same text block ----------
            quad = extract_quadrant(raw_text)
            quad_pdf = quad_db = quad_row = quad_col = ""
            quad_conf = 0
            if quad and quad["levels"] == 3:
                quad_pdf  = quad["pdf_label"]
                quad_db   = quad["db_label"]
                quad_row  = str(quad["row"])
                quad_col  = str(quad["col"])
                quad_conf = int(quad["confidence"] * 100)
                log.info("Location -- quadrant found: pdf=%s db=%s row=%s col=%s",
                         quad_pdf, quad_db, quad_row, quad_col)

            log.info("Location -- page %d  sec=%s  twp=%s  rng=%s  conf=%d",
                     page_num, sec, twp, rng, conf)
            result.update(
                detected=True, page=page_num,
                section=sec, township=twp, range=rng,
                raw_text=raw_text, confidence=conf,
                image_path=str(crop_path) if crop_path else None,
                annotated_path=str(ann_path) if ann_path else None,
                quadrant_pdf=quad_pdf, quadrant_db=quad_db,
                quadrant_row=quad_row, quadrant_col=quad_col,
                quadrant_confidence=quad_conf,
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

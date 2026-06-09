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
from grid.form_classifier import (
    STR_TOP_HEADER,
    STR_RIGHT_OF_GRID,
    STR_LEFT_OF_GRID,
    STR_UPPER_RIGHT,
    STR_VERTICAL_RIGHT,
    STR_VERTICAL_LEFT,
    STR_ANY,
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


# -- STR zone region filter ----------------------------------------------------

def _region_for_zone(
    str_zone: str | None,
    page_w: int,
    page_h: int,
    grid_bbox=None,   # (x, y, w, h) full-page pixels, or None
) -> tuple[int, int, int, int] | None:
    """
    Convert a ``str_zone`` hint from the form classifier into a
    ``(x0, y0, x1, y1)`` search region.

    When zone is None or 'any' the whole page is returned (None),
    meaning no filtering is applied.

    Grid-relative zones (right_of_grid / left_of_grid) require
    ``grid_bbox``; if it is None they fall back to page halves.

    Zones
    ─────
    top_header     → top 40 % of page, full width
                     (T1_LARGE: STR in letterhead above the grid)
    right_of_grid  → same vertical band as grid, right 55 % of page
                     (T2_MED / T3_SMALL: labels to the right of grid)
    left_of_grid   → same vertical band as grid, left 45 % of page
                     (MID / LATE: STR block to the left)
    upper_right    → upper-right quadrant (y < 40 %, x > 35 %)
                     (T3_SMALL / T4_NOANCHOR: small upper-right block)
    vertical_right → right 50 % of page (stacked layout, right side)
    vertical_left  → left 50 % of page  (stacked layout, left side)
    any / None     → None (full page)
    """
    if not str_zone or str_zone == STR_ANY:
        return None

    # Generous padding so we never clip a label that slightly overruns a boundary.
    _PAD = 40

    if str_zone == STR_TOP_HEADER:
        return (0, 0, page_w, int(page_h * 0.42))

    if str_zone == STR_UPPER_RIGHT:
        return (int(page_w * 0.33), 0, page_w, int(page_h * 0.42))

    if str_zone in (STR_VERTICAL_RIGHT, STR_VERTICAL_LEFT):
        if str_zone == STR_VERTICAL_RIGHT:
            return (int(page_w * 0.45), 0, page_w, page_h)
        return (0, 0, int(page_w * 0.55), page_h)

    # Grid-relative zones — prefer grid_bbox if available.
    if grid_bbox is not None:
        try:
            gx, gy, gw, gh = int(grid_bbox[0]), int(grid_bbox[1]), \
                              int(grid_bbox[2]), int(grid_bbox[3])
            # Vertical band: from well above grid top to well below grid bottom.
            vy0 = max(0,      gy - gh - _PAD)
            vy1 = min(page_h, gy + gh + _PAD)
        except (TypeError, ValueError, IndexError):
            gx = gw = 0
            vy0, vy1 = 0, page_h
    else:
        gx = gw = 0
        vy0, vy1 = 0, page_h

    if str_zone == STR_RIGHT_OF_GRID:
        x0 = max(0, gx + gw - _PAD) if gw else int(page_w * 0.40)
        return (x0, vy0, page_w, vy1)

    if str_zone == STR_LEFT_OF_GRID:
        x1 = min(page_w, gx + _PAD) if gx else int(page_w * 0.55)
        return (0, vy0, x1, vy1)

    return None  # unknown zone — no filter


def _filter_annotations_to_region(annotations, region):
    """
    Return a new annotation list (preserving index-0 full-text blob) where
    every token annotation has its bounding-box centre inside ``region``.

    ``region`` is (x0, y0, x1, y1) or None (pass-through).
    """
    if region is None or not annotations:
        return annotations
    x0, y0, x1, y1 = region
    filtered = [annotations[0]]   # keep full-text blob at index 0
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
                filtered.append(ann)
        except Exception:
            continue
    return filtered


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


# -- Vertical label-over-value extraction (Strategy 4) -----------------------
# Used for Collection 11+ "LATE" form layout where the Section, Township, and
# Range labels appear as column headers with their values directly below:
#
#   County    SEC    TWP    RGE
#   Garvin    15     23N    10W
#
# OR in a stacked single-column layout:
#   County       SEC        TWP        RGE
#   Garvin       15         23N        10W
#
# Algorithm: find tokens whose text matches a field label; for each label
# find the closest non-label token BELOW it within the expected distance.

# Labels recognised in vertical layout (matches Collection 11+ form headers).
_VLABEL_SEC = re.compile(r"^sec(?:tion|t)?\.?$", re.I)
_VLABEL_TWP = re.compile(r"^(?:t(?:ownship|wp|wn|vp)?\.?)$", re.I)
_VLABEL_RNG = re.compile(r"^(?:r(?:ange|ge)?\.?)$", re.I)
# All three combined for quick "is this a label?" check.
_VLABEL_ANY = re.compile(
    r"^(?:sec(?:tion|t)?|t(?:ownship|wp|wn|vp)?|r(?:ange|ge)?|county)\.?$",
    re.I,
)


def _extract_str_vertical(annotations) -> tuple[str, str, str]:
    """
    Strategy 4: vertical label-over-value extraction for Collection 11+.

    Scans all OCR tokens to find SEC/TWP/RGE label tokens, then reads the
    first non-label token appearing directly below each label (same x-band,
    within max_dy pixels).

    Returns (section, township, range) — any field that cannot be resolved
    is returned as ''.

    Tolerances:
      x_tol  = 55 px  (label and value must share the same column)
      min_dy = 10 px  (value is strictly below the label bottom edge)
      max_dy = 160 px (no further away than ~2 line heights)
    """
    if not annotations or len(annotations) < 2:
        return "", "", ""

    # Build list of (cx, cy, bottom_y, text) for all tokens.
    tokens = []
    for ann in annotations[1:]:
        poly = ann.bounding_poly
        if not poly or not poly.vertices:
            continue
        try:
            xs = [v.x for v in poly.vertices]
            ys = [v.y for v in poly.vertices]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            bot_y  = max(ys)
            tokens.append((cx, cy, bot_y, (ann.description or "").strip()))
        except Exception:
            continue

    if not tokens:
        return "", "", ""

    # Find the first label token matching each field type.
    sec_label = next(((cx, cy, by, t) for cx, cy, by, t in tokens
                      if _VLABEL_SEC.match(t)), None)
    twp_label = next(((cx, cy, by, t) for cx, cy, by, t in tokens
                      if _VLABEL_TWP.match(t)), None)
    rng_label = next(((cx, cy, by, t) for cx, cy, by, t in tokens
                      if _VLABEL_RNG.match(t)), None)

    x_tol, min_dy, max_dy = 55, 10, 160

    def _value_below(label_tuple) -> str:
        """Return the closest non-label token directly below the given label."""
        if label_tuple is None:
            return ""
        lx, _, l_bot, _ = label_tuple
        candidates = []
        for cx, cy, _, t in tokens:
            if abs(cx - lx) > x_tol:
                continue
            dy = cy - l_bot
            if min_dy <= dy <= max_dy and not _VLABEL_ANY.match(t):
                candidates.append((dy, t))
        if candidates:
            return min(candidates)[1]
        return ""

    sec = _validate_section(_value_below(sec_label))
    twp = _validate_twprng(_value_below(twp_label))
    rng = _validate_twprng(_value_below(rng_label))
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
    str_strategy_hint: str | None = None,
    str_zone: str | None = None,
    grid_bbox=None,
) -> dict:
    """
    Scan pages for section/township/range. Strategy order:

    1. Grouped-keyword pairing (vertical-overlap alignment of SEC/TWP/RGE boxes).
    2. Per-keyword right-side extraction (fallback when grouping fails).
    3. Gemini Flash on the keyword-crop (last resort when OCR quality is poor).
    4. Vertical label-over-value extraction — for Collection 11+ "LATE" layout
       where field labels and values appear as column header / value rows.

    Form-type hints (from the grid classifier, set by main._dispatch):
    ─────────────────────────────────────────────────────────────────
    ``str_zone``          – page region where STR is expected (STR_* constant
                           from grid/form_classifier.py).  When supplied,
                           token annotations are PRE-FILTERED to this region
                           before any strategy runs.  This prevents a keyword
                           like "SEC" from matching an unrelated label elsewhere
                           on the page and dramatically reduces false-positives
                           for records with complex multi-section layouts.

    ``grid_bbox``         – (x, y, w, h) in full-page pixels of the detected
                           grid.  Used to compute exact grid-relative zones
                           (STR_RIGHT_OF_GRID, STR_LEFT_OF_GRID).

    ``str_strategy_hint`` – when "vertical_stack", Strategy 4 runs FIRST
                           (Collection 11+ tabular header layout); otherwise
                           Strategy 4 is the last fallback.

    ``min_overlap``       – overrides the strict default (config.LOCATION_MIN_OVERLAP).
                           Retry path passes the loose value for a second chance.
    """
    log = logger or logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)

    overlap = min_overlap if min_overlap is not None else LOCATION_MIN_OVERLAP
    prefer_vertical = (str_strategy_hint == "vertical_stack")

    result = {
        "detected": False, "page": None,
        "section": "", "township": "", "range": "",
        "raw_text": "", "confidence": 0,
        "image_path": None, "annotated_path": None,
        "quadrant_pdf": "", "quadrant_db": "",
        "quadrant_row": "", "quadrant_col": "",
        "quadrant_confidence": 0,
    }

    # Compute the search region once (same across all pages).
    # _region_for_zone returns None for STR_ANY / unknown zones, which means
    # _filter_annotations_to_region passes all annotations through unchanged.
    pw0 = ph0 = 1  # placeholder; updated inside the page loop before use
    _zone_region_cache: list = [None]  # mutable container so inner scope can update

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

            pw, ph = pil_image.size
            # Compute the zone-filter region (only once; page size is constant).
            if pw != pw0 or ph != ph0:
                pw0, ph0 = pw, ph
                zone_region = _region_for_zone(str_zone, pw, ph, grid_bbox)
                _zone_region_cache[0] = zone_region
                if zone_region:
                    log.debug(
                        "STR zone filter active: zone=%s region=%s",
                        str_zone, zone_region,
                    )
            else:
                zone_region = _zone_region_cache[0]

            # Apply zone filter — reduces false keyword matches on complex pages.
            ann_for_grouping = _filter_annotations_to_region(annotations, zone_region)

            sections, townships, ranges = find_keywords_lists(
                ann_for_grouping, LOCATION_KEYWORDS, extend_right, padding_height,
            )

            sec = twp = rng = ""
            raw_text = ""
            crop_box = None
            strat_used = ""

            # -- Strategy 4 EARLY: vertical label-over-value (LATE form layout) --
            # When the form classifier signals a "vertical_stack" layout (Collection
            # 11+ top-right grid) try the vertical extractor FIRST, before the
            # horizontal grouping strategies.  This prevents the grouping from
            # wasting time on a layout it cannot handle.
            if prefer_vertical:
                v_sec, v_twp, v_rng = _extract_str_vertical(ann_for_grouping)
                if sum(bool(v) for v in (v_sec, v_twp, v_rng)) >= 2:
                    sec, twp, rng = v_sec, v_twp, v_rng
                    raw_text = f"vertical: sec={sec} twp={twp} rng={rng}"
                    strat_used = "vertical"
                    log.debug("Strategy 4 (vertical-first): sec=%s twp=%s rng=%s",
                              sec, twp, rng)

            found = sum(bool(v) for v in (sec, twp, rng))

            # -- Strategy 1: grouped extraction ----------------------------
            if found < 2:
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
                            g1_sec, g1_twp, g1_rng = _extract_str(raw_text)
                            sec = sec or g1_sec
                            twp = twp or g1_twp
                            rng = rng or g1_rng
                            if not strat_used:
                                strat_used = "grouped"

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
                if not strat_used:
                    strat_used = "per_keyword"

            # -- Strategy 3: Gemini on the keyword crop ----------------------
            # Run when OCR regex yields fewer than 3 fields so that any
            # missing field (e.g. Range when section + township were parsed)
            # is filled by Gemini reading the crop image directly.
            #
            # Using `found < 3` (instead of the old `found < 2`) catches the
            # common "2-of-3" partial result where OCR succeeds on two labels
            # but misses one value — Gemini fills in the gap without replacing
            # the two correct values (the `sec = sec or g_sec` merge below
            # preserves existing non-empty values).
            #
            # Two sub-cases:
            #   3a. Strategy 1 found a keyword group → use that crop bbox.
            #       This is the v4-style tight-crop approach: keyword positions
            #       from OCR → span all 3 keyword boxes → crop PNG → Gemini.
            #   3b. No keyword group (T2_MED handwritten forms where OCR
            #       finds zero tokens) → fall back to the zone-filter region
            #       (right of grid / upper-right quadrant).  Critical path for
            #       forms where Gemini reads handwriting from the expected zone.
            if found < 3:
                _gemini_crop_box = crop_box   # may be None
                if _gemini_crop_box is None and zone_region is not None:
                    # Use the zone region as the Gemini crop (clamped to page).
                    zx0, zy0, zx1, zy1 = zone_region
                    _gemini_crop_box = (
                        max(0, zx0), max(0, zy0),
                        min(pw, zx1), min(ph, zy1),
                    )
                    if _gemini_crop_box[2] <= _gemini_crop_box[0] or \
                       _gemini_crop_box[3] <= _gemini_crop_box[1]:
                        _gemini_crop_box = None

                if _gemini_crop_box is not None:
                    _prev_found = found   # track how many fields OCR had before Gemini
                    g_sec, g_twp, g_rng = _gemini_str_from_crop(
                        pil_image.crop(_gemini_crop_box), log,
                    )
                    sec = sec or g_sec
                    twp = twp or g_twp
                    rng = rng or g_rng
                    found = sum(bool(v) for v in (sec, twp, rng))
                    if found > 0 and raw_text == "":
                        raw_text = f"gemini: sec={sec} twp={twp} rng={rng}"
                    if found > 0:
                        if not strat_used:
                            # No OCR fields at all — pure Gemini result.
                            strat_used = "gemini_zone" if crop_box is None else "gemini"
                        elif _prev_found > 0:
                            # Gemini filled gaps in a partial OCR result.
                            strat_used = strat_used + "+gemini_fill"

            # -- Strategy 4 LATE: vertical label-over-value (normal order) -----
            # For non-LATE form types, try vertical extraction as a last resort
            # after all horizontal strategies have failed.
            if found < 2 and not prefer_vertical:
                v_sec, v_twp, v_rng = _extract_str_vertical(ann_for_grouping)
                sec = sec or v_sec
                twp = twp or v_twp
                rng = rng or v_rng
                found = sum(bool(v) for v in (sec, twp, rng))
                if found > 0 and not raw_text:
                    raw_text = f"vertical: sec={sec} twp={twp} rng={rng}"
                if not strat_used and found > 0:
                    strat_used = "vertical"

            if found < 2:
                continue

            # Confidence: strategy-weighted scoring.
            # grouped / per-keyword / vertical (pure OCR) → up to 100 per field.
            # gemini / gemini_zone → cap at 75 (Gemini can misread printed numbers).
            # *+gemini_fill → Gemini filled a gap; score the mix between OCR and Gemini.
            _is_pure_gemini  = strat_used in ("gemini", "gemini_zone")
            _is_gemini_fill  = strat_used and "+gemini_fill" in strat_used
            base_conf = (found * 100) // 3
            if _is_pure_gemini:
                conf = min(75, base_conf)
            elif _is_gemini_fill:
                # OCR gave partial; Gemini topped up — score at 85 ceiling.
                conf = min(85, base_conf)
            else:
                conf = base_conf

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

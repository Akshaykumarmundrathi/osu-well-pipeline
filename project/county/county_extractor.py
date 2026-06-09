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
import os
import re
from pathlib import Path

from config import (
    COUNTY_KEYWORDS,
    COUNTY_LIST_CLEAN,
    COUNTY_MAP_CLEAN_TO_ORIGINAL,
    COUNTY_RETRY_CROP_SCALE,
    EXTEND_LEFT_PIXELS,
    EXTEND_RIGHT_PIXELS,
    FUZZY_MATCH_THRESHOLD,
    ILLEGIBLE_WORD_THRESHOLD,
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


from config import VALID_COUNTY_LIST_ORIGINAL as _COUNTY_FULL_LIST


def _parse_county_full_response(text: str) -> tuple[str, int]:
    """
    Enhanced county response parser — ported from original
    parse_county_response_fuzzy_v2() in OSU_WELL_CHECKPOINT1.py.

    Handles verbose Gemini Pro responses like
    "Based on the text, this appears to be Creek County" where a simple
    base-name fuzzy match on the whole string would fail.

    Priority:
      1. Exact full-name match (score 100)
      2. Word-boundary containment check — longest names first (score 95)
      3. Base-name fuzzy fallback via _fuzzy_match()
    """
    text = text.strip()
    text_lower = text.lower()
    if not text or any(p in text_lower for p in
                       ("not detected", "none found", "no county", "n/a")):
        return "", 0

    # 1. Exact full-name match
    for cname in _COUNTY_FULL_LIST:
        if text.lower() == cname.lower():
            return cname, 100

    # 2. Containment check (longest names first so "Roger Mills" beats "Rogers")
    for cname in sorted(_COUNTY_FULL_LIST, key=len, reverse=True):
        if re.search(rf"\b{re.escape(cname)}\b", text, re.I):
            return cname, 95

    # 3. Base-name fuzzy match (existing logic)
    return _fuzzy_match(text)


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

    Handles the most common county format: "<name> County"
    (the name precedes the printed word "County").

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


def _tokens_right_of(annotations, kw_box, max_tokens: int = 4,
                     same_line_tol: int = 35) -> str:
    """
    Return the text of up to `max_tokens` annotation tokens that sit
    immediately to the RIGHT of `kw_box` on the same horizontal line.

    Handles the "County: <name>" format — the name follows the keyword and
    optional colon.  Colons that appear as stand-alone OCR tokens are
    stripped from the returned text.

    Example input tokens right of 'County':  [':', 'Oklahoma']
    Returns: 'Oklahoma'
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
            cx  = sum(xs) / len(xs)
            cy  = sum(ys) / len(ys)
            x_min = min(xs)
        except Exception:
            continue
        if x_min <= kx1:                       # not to the right
            continue
        if abs(cy - ky_mid) > same_line_tol:   # not on the same line
            continue
        candidates.append((cx, ann.description))

    # Sort by x ascending; take the leftmost `max_tokens` (those nearest keyword).
    candidates.sort(key=lambda t: t[0])
    head = candidates[:max_tokens]
    # Strip leading colon artefacts (e.g. the ':' after "County:").
    texts = [re.sub(r'^[:]+\s*', '', desc).strip() for _, desc in head]
    return " ".join(t for t in texts if t)


def _tokens_below_keyword(annotations, kw_box, max_tokens: int = 2,
                           x_tol: int = 80, min_dy: int = 5,
                           max_dy: int = 120) -> str:
    """
    Return text of up to `max_tokens` tokens directly BELOW `kw_box`.

    Handles the stacked / tabular format used in Collection 11+ forms:
        County          ← keyword box
        Garvin          ← value directly below

    or the tabular row layout:
        County   SEC   TWP   RGE
        Garvin   15    23N   10W

    `x_tol`  : horizontal tolerance (px) — the value must be roughly
               centred on the keyword's x-centre to avoid grabbing an
               adjacent field's value.
    `min_dy` : minimum distance below keyword bottom edge (avoids same-line).
    `max_dy` : maximum distance below keyword bottom edge (avoids next section).
    """
    if kw_box is None or not annotations:
        return ""
    kx0, ky0, kx1, ky1 = kw_box
    kx_mid = (kx0 + kx1) / 2

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
        except Exception:
            continue
        if abs(cx - kx_mid) > x_tol:          # not vertically aligned
            continue
        dy = cy - ky1                          # distance below keyword bottom
        if min_dy <= dy <= max_dy:
            candidates.append((dy, ann.description))

    candidates.sort(key=lambda t: t[0])       # closest first
    head = candidates[:max_tokens]
    return " ".join(desc for _, desc in head)


def _try_structural_anchor(annotations, kw_box, log,
                           county_format_hint: str | None = None) -> tuple[str, int]:
    """
    Fuzzy-match tokens around the County keyword using three strategies.

    Strategy order is driven by ``county_format_hint`` from the form
    classifier so the most likely format is tried first, reducing the
    number of OCR token reads needed per record:

    ``"name_county"``   → LEFT first  (most common on early forms: "Oklahoma County")
    ``"county_colon"``  → RIGHT first (colon format: "County: Oklahoma")
    ``"stacked"``       → BELOW first (Collection 11+ tabular: County / Garvin)
    ``"any"`` / None    → LEFT → RIGHT → BELOW (default order)

    After trying the priority strategy, the remaining two are tried in their
    default order so nothing is missed if the hint is wrong.

    Returns the first strategy that produces a match at or above
    RETRY_CONFIDENCE_THRESHOLD (95).  If none clears that threshold, returns
    the best (name, score) pair found across all three so the caller can use
    it as a weak fallback at FUZZY_MATCH_THRESHOLD (72).
    """
    from grid.form_classifier import COUNTY_NAME_THEN_WORD, COUNTY_COLON_AFTER, COUNTY_STACKED

    # Build ordered strategy list driven by hint.
    _all = [
        ("left",  _tokens_left_of),
        ("right", _tokens_right_of),
        ("below", _tokens_below_keyword),
    ]
    if county_format_hint == COUNTY_STACKED:
        ordered = [("below", _tokens_below_keyword),
                   ("left",  _tokens_left_of),
                   ("right", _tokens_right_of)]
    elif county_format_hint == COUNTY_COLON_AFTER:
        ordered = [("right", _tokens_right_of),
                   ("left",  _tokens_left_of),
                   ("below", _tokens_below_keyword)]
    else:  # COUNTY_NAME_THEN_WORD, "any", or None → default left-first
        ordered = _all

    best_name, best_score = "", 0

    for label, strategy_fn in ordered:
        text = strategy_fn(annotations, kw_box)
        if not text:
            continue
        name, score = _fuzzy_match(text)
        log.debug("County anchor (%s): text=%r -> %r score=%d",
                  label, text, name, score)
        if name and score >= RETRY_CONFIDENCE_THRESHOLD:
            # High-confidence hit — return immediately, no Gemini needed.
            return name, score
        if name and score > best_score:
            best_name, best_score = name, score

    return best_name, best_score


# -- Per-page logic ------------------------------------------------------------

def _try_page(
    manager: PDFDocumentManager,
    page_num: int,
    output_dir: Path,
    pdf_stem: str,
    log: logging.Logger,
    crop_scale: float = 1.0,
    full_page_gemini: bool = False,
    county_format_hint: str | None = None,
) -> tuple[dict | None, bool]:
    """
    Attempt county extraction on a single page.
    Returns (result_dict, keyword_found).
    result_dict is None if the County keyword isn't on this page.

    Parameters:
      crop_scale          multiplier on the keyword crop margins (retry uses >1)
      full_page_gemini    if True, sends the WHOLE page to Gemini Pro (retry)
      county_format_hint  form-classifier hint; controls strategy priority in
                          _try_structural_anchor() so the expected token layout
                          (left / right / below) is tried first.
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

    # Illegibility guard: very few OCR tokens → handwritten or blank page.
    # The "County" keyword won't be found, and calling Gemini on noise is wasteful.
    word_count = len(annotations) - 1
    if word_count < ILLEGIBLE_WORD_THRESHOLD:
        log.debug("Page %d: only %d words from OCR — skipping county (illegible)",
                  page_num + 1, word_count)
        return None, False

    kw_box = find_keyword_box(annotations, COUNTY_KEYWORDS)
    if kw_box is None:
        log.debug("County keyword not on page %d", page_num + 1)
        return None, False

    # -- Strategy 1: zero-cost structural anchor -----------------------------
    name, score = _try_structural_anchor(annotations, kw_box, log,
                                         county_format_hint=county_format_hint)
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
    from grid.form_classifier import COUNTY_STACKED, COUNTY_COLON_AFTER
    pw, ph = pil_image.size
    x0, y0, x1, y1 = kw_box
    # Form-type-aware padding.
    # COUNTY_STACKED (Collection 11+): county value is BELOW the keyword so
    #   increase vertical padding and reduce horizontal noise.
    # COUNTY_COLON_AFTER: value is to the RIGHT, clamp left extension.
    # Default (COUNTY_NAME_THEN_WORD / "any"): value LEFT of keyword — use
    #   standard left-heavy crop; cap at 50% of page width to avoid including
    #   the grid image on T2_MED landscape forms.
    _base_left  = EXTEND_LEFT_PIXELS
    _base_right = EXTEND_RIGHT_PIXELS
    _base_vert  = VERTICAL_PADDING_PIXELS
    if county_format_hint == COUNTY_STACKED:
        _base_vert  = 120   # extend well below to capture the stacked value
        _base_left  = 200   # no need for wide left on stacked layout
        _base_right = 200
    elif county_format_hint == COUNTY_COLON_AFTER:
        _base_left  = 100   # value is to the right, narrow left side
        _base_right = 600
    else:
        # name_county or any: value is left of "County" — cap left pad so we
        # don't bleed into the grid on T2_MED landscape forms (page ~3200px wide).
        _base_left = min(_base_left, int(pw * 0.45))

    left_pad  = int(_base_left  * crop_scale)
    right_pad = int(_base_right * crop_scale)
    vert_pad  = int(_base_vert  * crop_scale)
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

    # When Gemini is disabled, skip the Gemini block entirely — no RuntimeError,
    # just set the sentinel and fall through to the anchor_weak fallback below.
    _gemini_disabled = os.environ.get("GEMINI_DISABLED", "0").strip() == "1"
    if _gemini_disabled:
        log.debug("Gemini disabled (GEMINI_DISABLED=1) — skipping Gemini on page %d",
                  page_num + 1)
        result["error"] = "gemini_disabled"
    else:
        try:
            flash, pro, cfg = _get_gemini()

            if full_page_gemini:
                # Skip Flash, send the whole page directly to Pro.
                raw2 = _gemini_call(pro, cfg, prompt_pass2, pil_image)
                result["pass2_result"] = raw2
                if raw2.lower() != "not detected.":
                    # Use enhanced parser for Pro: handles "Creek County" inside
                    # verbose responses (ported from parse_county_response_fuzzy_v2).
                    n2, s2 = _parse_county_full_response(raw2)
                    if n2 and s2 >= FUZZY_MATCH_THRESHOLD:
                        result.update(detected=True, name=n2,
                                      fuzzy_score=s2, confidence=s2,
                                      method="full_page_pro")
                        return result, True
            else:
                # -- Pass 1 (Flash) on the crop ----------------------------------
                # Flash prompt asks for base name only → use base-name fuzzy match.
                raw1 = _gemini_call(flash, cfg, prompt_pass1, crop)
                result["pass1_result"] = raw1
                p1_not_found = raw1.lower().strip() in ("not detected.", "not detected", "")
                if not p1_not_found:
                    n1, s1 = _fuzzy_match(raw1)
                    if n1 and s1 >= RETRY_CONFIDENCE_THRESHOLD:
                        log.info("County (P1) = %r  score=%d", n1, s1)
                        result.update(detected=True, name=n1,
                                      fuzzy_score=s1, confidence=s1,
                                      method="flash")
                        return result, True

                # -- Pass 2 (Pro) on the crop ------------------------------------
                # Always try Pro when Flash fails — Pro uses the richer full-name
                # prompt and the enhanced word-boundary parser, which handles
                # handwritten county names that Flash's base-name prompt misses.
                #
                # Previous logic skipped Pro when both Flash AND the structural
                # anchor failed ("skip_pass2 = p1_not_found and not anchor_name").
                # That caused ~8% county failures (327/4054) in test100 because
                # T2_MED handwritten forms have no OCR anchor and Flash fails
                # to read cursive → Pro was never called.  Fix: always run Pro.
                raw2 = _gemini_call(pro, cfg, prompt_pass2, crop)
                result["pass2_result"] = raw2
                if raw2.lower().strip() not in ("not detected.", "not detected", ""):
                    # Pro prompt asks for full name → use enhanced parser with
                    # word-boundary containment check (handles verbose responses).
                    n2, s2 = _parse_county_full_response(raw2)
                    if n2 and s2 >= FUZZY_MATCH_THRESHOLD:
                        log.info("County (P2) = %r  score=%d", n2, s2)
                        result.update(detected=True, name=n2,
                                      fuzzy_score=s2, confidence=s2,
                                      method="pro")
                        return result, True

            log.warning("County not matched on page %d", page_num + 1)
            result["error"] = "no_match"

        except Exception as exc:
            # All Gemini failures: quota, network, bad response — log at ERROR.
            # GEMINI_DISABLED path is now handled above and never reaches here.
            log.error("Gemini failed on page %d: %s", page_num + 1, exc, exc_info=True)
            result["error"] = str(exc)

    # -- Fallback: accept weaker structural anchor if above min threshold --------
    # Placed OUTSIDE the try block so a Gemini RuntimeError (quota exhausted,
    # API key invalid, network failure) never discards a valid Vision OCR anchor
    # result.  anchor_name/anchor_score were captured before the Gemini call.
    if not result.get("detected") and anchor_name and anchor_score >= FUZZY_MATCH_THRESHOLD:
        log.info("County (anchor-weak) = %r  score=%d  (Gemini unavailable or no_match)",
                 anchor_name, anchor_score)
        result.update(detected=True, name=anchor_name,
                      fuzzy_score=anchor_score, confidence=anchor_score,
                      method="anchor_weak")

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
    county_format_hint: str | None = None,
) -> dict:
    """
    Iterate the first `max_pages` (default: MAX_COUNTY_PAGES) until the
    County keyword is located. Then run structural-anchor + Gemini.

    Retry callers can pass:
      crop_scale=COUNTY_RETRY_CROP_SCALE  (wider crop, helps OCR-quality issues)
      full_page_gemini=True               (skip crop, feed whole page to Pro)
      max_pages=<int>                     (override default 2-page limit)
      county_format_hint=<str>            (COUNTY_* from form_classifier —
                                           controls which token direction the
                                           structural anchor tries first)
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
            county_format_hint=county_format_hint,
        )
        if found_keyword:
            # Keyword found on this page — inline escalation for no_match.
            # When the crop-level approach fails (both anchor and Gemini crop
            # return nothing), automatically send the FULL PAGE to Pro.  This
            # catches handwritten county names that Gemini can't read from a
            # narrow keyword crop but can identify from the full form context.
            #
            # Only escalate when:
            #   (a) crop-level result is no_match (not already in full-page mode)
            #   (b) not already called with full_page_gemini=True (avoid loop)
            if (r is not None
                    and not r.get("detected")
                    and r.get("error") in ("no_match", "invalid_crop", None, "")
                    and not full_page_gemini):
                log.info("County: crop-level no_match on page %d — escalating to full-page Pro",
                         page_num + 1)
                r2, _ = _try_page(
                    manager, page_num, output_dir, pdf_stem, log,
                    crop_scale=1.0, full_page_gemini=True,
                    county_format_hint=county_format_hint,
                )
                if r2 is not None and r2.get("detected"):
                    return r2
                # Full-page also failed — return original crop result so the
                # caller sees the partial pass1/pass2 evidence for debugging.
            return r

    log.warning("County keyword not found on first %d page(s)", cap)
    return {
        "detected": False, "page": None,
        "name": "", "pass1_result": "", "pass2_result": "",
        "fuzzy_score": 0, "confidence": 0,
        "image_path": None, "annotated_path": None,
        "error": "keyword_not_found",
    }

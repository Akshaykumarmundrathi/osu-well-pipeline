"""
'Location:' keyword extractor for collections 9 and later.

Post-(9) forms no longer print 'Section / Township / Range' as separate
labels. Instead a single 'Location:' line carries values like:

    Location: NW/4 SEC 14, T-18N, R-13E, Tulsa County
    Location: SE/4 NE/4 31  21N  4W  Creek
    Surface Location:  Sec 22, Twp 27N, Rge 13W   Washington

This module finds the 'Location' keyword, reads the OCR tokens to its
right (and the line below if needed), and extracts section/township/
range/county in one pass. It complements `location_extractor.py`
(which expects three separate keyword boxes) and is selected via
TIER_CONFIG['location_strategy'] == 'location_keyword'.

Public entry point mirrors process_single_location's return shape so
the dispatcher can swap implementations without callers changing.
"""

import logging
import re
from pathlib import Path

from config import (
    COUNTY_LIST_CLEAN,
    COUNTY_MAP_CLEAN_TO_ORIGINAL,
    FUZZY_MATCH_THRESHOLD,
    ILLEGIBLE_WORD_THRESHOLD,
    LOCATION_LINE_KEYWORDS,
)
from ocr.vision_api import detect_text_with_vision
from pdf.pdf_manager import PDFDocumentManager
from utils.io_utils import annotate_page

# -- Regex helpers -------------------------------------------------------------

# Stand-alone section: just the digits in context, e.g. "SEC 14", "sec. 22"
_SEC_RE = re.compile(r"\bsec(?:tion)?\.?\s*(\d{1,2})\b", re.I)

# Township with required direction: 18N / T-18N / Twp 27N
_TWP_RE = re.compile(
    r"(?:\bT[\.\-\s]*|township\s*|twp\.?\s*|twn\.?\s*)?"
    r"(\d{1,2})\s*([NS])\b",
    re.I,
)

# Range with required direction: 13E / R-4W / Rge 13W
_RNG_RE = re.compile(
    r"(?:\bR[\.\-\s]*|range\s*|rge\.?\s*)?"
    r"(\d{1,2})\s*([EW])\b",
    re.I,
)

# County name token. Cleanest heuristic: any word(s) followed by ' County',
# OR a bare word that fuzzy-matches the known list. The fuzzy match is run
# after token extraction so we don't need the keyword here.
_COUNTY_RE = re.compile(r"([A-Za-z][A-Za-z\s\-\.]{2,30})\s+county\b", re.I)


# -- Fuzzy match (mirrors county_extractor) -----------------------------------

try:
    from rapidfuzz import process as _rfuzz

    def _fuzzy_county(text: str) -> tuple[str, int]:
        """Best fuzzy match against COUNTY_LIST_CLEAN; '' if below threshold."""
        clean = re.sub(r"\bcounty\b", "", text, flags=re.I).strip().lower()
        if not clean:
            return "", 0
        m = _rfuzz.extractOne(clean, COUNTY_LIST_CLEAN,
                              score_cutoff=FUZZY_MATCH_THRESHOLD)
        if m:
            name, score, _ = m
            return COUNTY_MAP_CLEAN_TO_ORIGINAL.get(name, ""), int(score)
        return "", 0

except ImportError:
    import difflib

    def _fuzzy_county(text: str) -> tuple[str, int]:
        """difflib fallback."""
        clean = re.sub(r"\bcounty\b", "", text, flags=re.I).strip().lower()
        if not clean:
            return "", 0
        matches = difflib.get_close_matches(
            clean, COUNTY_LIST_CLEAN, n=1, cutoff=FUZZY_MATCH_THRESHOLD / 100,
        )
        if matches:
            ratio = difflib.SequenceMatcher(None, clean, matches[0]).ratio()
            return COUNTY_MAP_CLEAN_TO_ORIGINAL.get(matches[0], ""), int(ratio * 100)
        return "", 0


# -- OCR helpers ---------------------------------------------------------------

def _find_location_keyword_box(annotations, keywords) -> tuple | None:
    """
    Locate one of the keyword phrases in the annotations and return its
    union bounding box (x_min, y_min, x_max, y_max). Phrases may span
    multiple tokens. The first match wins.
    """
    if not annotations or len(annotations) < 2:
        return None
    full = (annotations[0].description or "").lower()
    for phrase in keywords:
        phrase_l = phrase.lower()
        if phrase_l not in full:
            continue
        words = re.split(r"\s+", phrase_l.strip())
        words = [w for w in words if w]
        bbox = _phrase_bbox(annotations[1:], words)
        if bbox:
            return bbox
    return None


def _phrase_bbox(token_anns, words):
    """Find consecutive token annotations matching `words`; return union bbox."""
    n = len(words)
    if n == 0:
        return None
    descs = [(ann.description or "").lower().strip(".,:") for ann in token_anns]
    for i in range(len(descs) - n + 1):
        if all(words[k] in descs[i + k] for k in range(n)):
            xs, ys = [], []
            for ann in token_anns[i:i + n]:
                if not ann.bounding_poly or not ann.bounding_poly.vertices:
                    continue
                xs.extend(v.x for v in ann.bounding_poly.vertices)
                ys.extend(v.y for v in ann.bounding_poly.vertices)
            if xs:
                return (min(xs), min(ys), max(xs), max(ys))
    return None


def _tokens_in_region(annotations, x0, y0, x1, y1) -> str:
    """Join token descriptions whose bbox centre falls inside the region."""
    out = []
    for ann in annotations[1:]:
        poly = ann.bounding_poly
        if not poly or not poly.vertices:
            continue
        try:
            xs = [v.x for v in poly.vertices]
            ys = [v.y for v in poly.vertices]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        except Exception:
            continue
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            out.append(ann.description)
    return " ".join(out)


# -- Public entry point --------------------------------------------------------

def process_single_location_keyword(
    manager: PDFDocumentManager,
    output_dir: Path,
    pdf_stem: str,
    logger: logging.Logger | None = None,
    right_extend: int = 1100,
    below_extend: int = 220,
    side_padding: int = 50,
    min_overlap: float | None = None,  # accepted for API parity (unused here)
) -> dict:
    """
    Find the 'Location:' line and extract (section, township, range,
    county_name) from it. Same return shape as `process_single_location`
    plus an extra `county_name` field that callers may merge into the
    county stage's row.
    """
    log = logger or logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "detected":      False, "page": None,
        "section": "", "township": "", "range": "",
        "county_name":   "", "county_score": 0,
        "raw_text":      "", "confidence": 0,
        "image_path":    None, "annotated_path": None,
    }

    try:
        for page_num, pil_image in manager.iter_pil_pages():
            annotations = detect_text_with_vision(
                pil_image, manager=manager, page_num=page_num,
            )
            if not annotations:
                continue

            # Illegibility guard: fewer tokens than threshold means a blank or
            # handwritten page — "Location:" keyword won't be found anyway.
            word_count = len(annotations) - 1
            if word_count < ILLEGIBLE_WORD_THRESHOLD:
                log.debug("Page %d: only %d words — skipping (illegible)",
                          page_num, word_count)
                continue

            kw_box = _find_location_keyword_box(annotations,
                                                LOCATION_LINE_KEYWORDS)
            if kw_box is None:
                continue

            pw, ph = pil_image.size
            kx0, ky0, kx1, ky1 = kw_box

            # Capture both 'to the right' (same line) and 'below'
            # (multi-line layout) of the Location keyword.
            right_x0 = kx1
            right_x1 = min(pw, kx1 + right_extend)
            right_y0 = max(0, ky0 - side_padding)
            right_y1 = min(ph, ky1 + side_padding)
            below_x0 = max(0, kx0 - side_padding)
            below_x1 = min(pw, kx1 + right_extend)
            below_y0 = ky1
            below_y1 = min(ph, ky1 + below_extend)

            right_text = _tokens_in_region(annotations, right_x0, right_y0,
                                           right_x1, right_y1)
            below_text = _tokens_in_region(annotations, below_x0, below_y0,
                                           below_x1, below_y1)
            raw_text = (right_text + " " + below_text).strip()
            if not raw_text:
                continue

            sec  = _extract_section(raw_text)
            twp  = _extract_township(raw_text)
            rng  = _extract_range(raw_text)
            cname, cscore = _extract_county_from_text(raw_text)

            found = sum(bool(v) for v in (sec, twp, rng))
            # Require at least two fields OR a county to count as detected.
            if found < 2 and not cname:
                continue

            conf = max((found * 100) // 3, cscore if cname else 0)

            # Save the crop region we read for review.
            cx0 = min(right_x0, below_x0)
            cy0 = min(right_y0, below_y0)
            cx1 = max(right_x1, below_x1)
            cy1 = max(right_y1, below_y1)
            crop = pil_image.crop((cx0, cy0, cx1, cy1))
            crop_path = output_dir / f"{pdf_stem}_page_{page_num:02d}_location_kw_crop.png"
            crop.save(str(crop_path))
            ann_path = output_dir / f"{pdf_stem}_page_{page_num:02d}_location_kw_page.png"
            annotate_page(pil_image, (cx0, cy0, cx1, cy1),
                          color="purple", label="Location").save(str(ann_path))

            log.info("Location(kw) -- page %d sec=%s twp=%s rng=%s county=%s",
                     page_num, sec, twp, rng, cname or "-")
            result.update(
                detected=True, page=page_num,
                section=sec, township=twp, range=rng,
                county_name=cname, county_score=cscore,
                raw_text=raw_text, confidence=conf,
                image_path=str(crop_path),
                annotated_path=str(ann_path),
            )
            return result

    except Exception as exc:
        log.error("Location-keyword extraction error: %s", exc, exc_info=True)
        result["error"] = str(exc)
        return result

    result["error"] = "not_found"
    return result


# -- Field extraction helpers --------------------------------------------------

def _extract_section(text: str) -> str:
    """Return a section number in 1..36 or ''."""
    m = _SEC_RE.search(text)
    if not m:
        return ""
    try:
        n = int(m.group(1))
        return str(n) if 1 <= n <= 36 else ""
    except ValueError:
        return ""


def _extract_township(text: str) -> str:
    """Return e.g. '18N' or ''."""
    m = _TWP_RE.search(text)
    if not m:
        return ""
    n = int(m.group(1))
    if not (1 <= n <= 50):
        return ""
    return f"{n}{m.group(2).upper()}"


def _extract_range(text: str) -> str:
    """Return e.g. '13W' or ''."""
    m = _RNG_RE.search(text)
    if not m:
        return ""
    n = int(m.group(1))
    if not (1 <= n <= 50):
        return ""
    return f"{n}{m.group(2).upper()}"


def _extract_county_from_text(text: str) -> tuple[str, int]:
    """
    Try, in order:
      1. Explicit '<NAME> County' match in the raw text.
      2. Last few tokens against the fuzzy county list.
    Returns (canonical_name, score).
    """
    m = _COUNTY_RE.search(text)
    if m:
        name, score = _fuzzy_county(m.group(1))
        if name:
            return name, score
    # Tail-fuzzy: try the rightmost word(s).
    tokens = re.findall(r"[A-Za-z][A-Za-z\-]+", text)
    for window in (3, 2, 1):
        if len(tokens) >= window:
            tail = " ".join(tokens[-window:])
            name, score = _fuzzy_county(tail)
            if name:
                return name, score
    return "", 0

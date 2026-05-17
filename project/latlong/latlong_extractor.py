"""
Lat/Long extraction stage — runs first on every PDF.

process_single_latlong(manager, pdf_stem, logger) -> dict
  {detected, page, lat, lon, well_name, well_type, confidence, error}

Strategy
--------
1. Run Vision OCR on each page (same call as location/county — no extra cost
   when lat/lon is absent; saves location+grid cost when lat/lon is present).
2. Look for labelled coordinates first: "Lat: 36.1234" / "Lon: -97.5678"
3. Fall back to any pair of decimal numbers with 4+ decimal places whose
   values are in valid lat (-90..90) and lon (-180..180) ranges.
4. Also extract well type (oil / gas / water/disposal) from OCR text and
   from the PDF stem (SWD suffix = water/disposal).

Returns detected=False if no coordinates found (no error; pipeline continues
to grid + location stages).
"""

import logging
import re
from pathlib import Path

from ocr.vision_api import detect_text_with_vision
from pdf.pdf_manager import PDFDocumentManager

# -- Lat/lon patterns ----------------------------------------------------------

# Labelled: "Lat: 36.123456"  "Longitude=-97.654321"  "LAT 36.12345"
_RE_LAT_LABEL = re.compile(
    r'\blat(?:itude)?\s*[=:.]?\s*(-?\d{1,2}\.\d{4,})', re.I
)
_RE_LON_LABEL = re.compile(
    r'\blon(?:gitude|g)?\s*[=:.]?\s*(-?1?[0-9]{1,2}\.\d{4,})', re.I
)

# Unlabelled: any decimal number with 4+ fractional digits
_RE_DECIMAL = re.compile(r'-?\d{1,3}\.\d{4,}')

# -- Well type patterns --------------------------------------------------------

_WELL_TYPE_RULES = [
    (re.compile(r'\b(?:salt\s*water|saltwater|swd|disposal|injection)\b', re.I),
     "water/disposal"),
    (re.compile(r'\bgas\s+well\b|\bgas\s+prod|\bgas\s+unit\b', re.I), "gas"),
    (re.compile(r'\boil\s+well\b|\boil\s+prod|\bcrude\b', re.I), "oil"),
    (re.compile(r'\bgas\b', re.I), "gas"),
    (re.compile(r'\boil\b', re.I), "oil"),
]


# -- Helpers -------------------------------------------------------------------

def _extract_well_name(pdf_stem: str) -> str:
    """
    PDF stem format: {api_or_prefix}_{WELL NAME}_{record_id}
    Returns the middle section as the well name.
    """
    first = pdf_stem.find("_")
    last  = pdf_stem.rfind("_")
    if first != -1 and last != first:
        return pdf_stem[first + 1: last]
    return pdf_stem


def _well_type_from_name(well_name: str) -> str:
    """Quick check on well name for type hints (e.g. '... 1 SWD')."""
    wn = well_name.upper()
    if "SWD" in wn or "DISPOSAL" in wn or "INJECTION" in wn:
        return "water/disposal"
    if "GAS" in wn:
        return "gas"
    if "OIL" in wn:
        return "oil"
    return ""


def _detect_well_type(ocr_text: str, well_name: str) -> str:
    # Check well name first (fast, no regex needed for common patterns)
    wt = _well_type_from_name(well_name)
    if wt:
        return wt
    for pattern, label in _WELL_TYPE_RULES:
        if pattern.search(ocr_text):
            return label
    return ""


def _is_valid_lat(v: float) -> bool:
    return -90.0 <= v <= 90.0


def _is_valid_lon(v: float) -> bool:
    return -180.0 <= v <= 180.0


def _extract_labeled(text: str):
    """
    Returns (lat, lon, confidence) or (None, None, 0).
    confidence=95 when both lat and lon labels found.
    """
    m_lat = _RE_LAT_LABEL.search(text)
    m_lon = _RE_LON_LABEL.search(text)
    if m_lat and m_lon:
        try:
            lat = float(m_lat.group(1))
            lon = float(m_lon.group(1))
            if _is_valid_lat(lat) and _is_valid_lon(lon):
                return lat, lon, 95
        except ValueError:
            pass
    return None, None, 0


def _extract_unlabeled(text: str):
    """
    Find all decimal numbers with 4+ fractional digits, then try to pair
    one valid lat with one valid lon.
    Returns (lat, lon, confidence) or (None, None, 0).
    confidence=75 (no labels — less certain about which is lat vs lon).
    """
    candidates = []
    for m in _RE_DECIMAL.finditer(text):
        try:
            v = float(m.group())
            candidates.append(v)
        except ValueError:
            continue

    # Need at least two candidates
    if len(candidates) < 2:
        return None, None, 0

    # Try consecutive pairs: first valid lat + next valid lon
    for i in range(len(candidates) - 1):
        a, b = candidates[i], candidates[i + 1]
        if _is_valid_lat(a) and _is_valid_lon(b) and a != b:
            return a, b, 75
        # Sometimes lon comes before lat
        if _is_valid_lat(b) and _is_valid_lon(a) and a != b:
            return b, a, 70   # lower confidence — order swapped

    return None, None, 0


# -- Public entry point --------------------------------------------------------

def process_single_latlong(
    manager: PDFDocumentManager,
    pdf_stem: str,
    logger: logging.Logger | None = None,
) -> dict:
    """
    Scans all pages for decimal-degree lat/lon coordinates.
    Returns on first page where coordinates are found.
    """
    log = logger or logging.getLogger(__name__)

    well_name = _extract_well_name(pdf_stem)
    result = {
        "detected":  False,
        "page":      None,
        "lat":       "",
        "lon":       "",
        "well_name": well_name,
        "well_type": _well_type_from_name(well_name),  # fast check from stem
        "confidence": 0,
        "error":     None,
    }

    try:
        # Lat/Lon labels are virtually always on cover/metadata page.
        # Limit to first 2 pages — saves Vision API calls on 3+ page docs
        # where coords are not present.
        _MAX_PAGES = 2
        for idx, (page_num, pil_image) in enumerate(manager.iter_pil_pages()):
            if idx >= _MAX_PAGES:
                break
            annotations = detect_text_with_vision(pil_image)
            if not annotations:
                continue

            full_text = annotations[0].description if annotations else ""

            lat, lon, conf = _extract_labeled(full_text)
            if lat is None:
                lat, lon, conf = _extract_unlabeled(full_text)

            if lat is not None and lon is not None:
                well_type = _detect_well_type(full_text, well_name)
                log.info(
                    "Lat/Lon -- page %d  lat=%.6f  lon=%.6f  conf=%d  type=%s",
                    page_num, lat, lon, conf, well_type or "unknown",
                )
                result.update(
                    detected=True, page=page_num,
                    lat=str(lat), lon=str(lon),
                    well_type=well_type,
                    confidence=conf,
                )
                return result

    except Exception as exc:
        log.error("LatLon extraction error: %s", exc, exc_info=True)
        result["error"] = str(exc)

    return result

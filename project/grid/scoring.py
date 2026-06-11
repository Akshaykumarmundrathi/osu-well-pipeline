"""
Grid extraction pipeline.

process_single_grid(manager, output_dir, pdf_stem, logger, relaxed=False) -> dict
  {detected, page, bbox, method, confidence, image_path, error?}

Strategy
--------
Each page is tried in this order:

  1. **Structural anchor**: OCR the page (Tesseract / Vision API; cached on
     the manager), look for one of the printed phrases beside the STR grid:
     'Spot Well Correctly' / 'Locate Well Correctly' / 'LOCATE WELL' /
     'Locate Well And Dotline Lease'.  When found, crop a fixed region
     above/below the anchor and run the CV extractors on the crop.

  2. **Full-page CV** (fallback): run all six extractors on the entire
     page image and pick the best 4-vertex candidate.

Pages are iterated in FORWARD order (page 1 first) for BOTH passes.
Rationale: the vast majority of forms (Collections 1–13) place the grid on
the FIRST page — bottom-left for early T1/T2 forms, top-left for T3 forms,
top-center/right for mid/late forms.  Only a small subset of Collection 8+
multi-page forms have the traditional grid on page 3; forward order still
finds it (pages 1, 2, 3 scanned in order) while avoiding false-positive
detections on back-page content before reaching the correct front page.
"""

import logging
import os
import signal
from pathlib import Path

try:
    import cv2
except ImportError as _cv2_err:
    raise ImportError(
        "OpenCV (cv2) is required for grid detection. "
        "Install it with: pip install opencv-python-headless"
    ) from _cv2_err

from config import (
    GRID_H_LOOSE, GRID_H_STRICT,
    GRID_W_LOOSE, GRID_W_STRICT,
)
from grid.anchors import ANCHOR_CROP_BY_TIER, crop_box_from_anchor, find_grid_anchor
from grid.form_classifier import classify_form_type
from grid.extractors import (
    extract_grid_region_adaptive,
    extract_grid_region_canny,
    extract_grid_region_corners,
    extract_grid_region_hough,
    extract_grid_region_otsu,
    extract_grid_region_rotated,
)
from grid.filters import is_valid_candidate
from ocr.vision_api import get_page_annotations
from pdf.pdf_manager import PDFDocumentManager

_METHODS = [
    ("adaptive", extract_grid_region_adaptive),
    ("otsu",     extract_grid_region_otsu),
    ("canny",    extract_grid_region_canny),
    ("hough",    extract_grid_region_hough),
    ("rotated",  extract_grid_region_rotated),
    ("corners",  extract_grid_region_corners),
]

# Aspect-ratio bounds for the detected grid bbox (width / height).
# Inspection data shows T3 small grids (Colls 4-9) are portrait rectangles:
#   W≈147-159px, H≈253-269px  →  AR≈0.58-0.63  (taller than wide)
# Coll 10-11 grids: AR≈0.56-0.70.  Only the older T2/T1 grids are landscape (AR>1).
# Lowered AR_MIN from 0.78→0.50 to accept portrait grids; raised AR_MAX slightly.
_AR_MIN, _AR_MAX    = 0.50, 1.60
_MIN_LINE_DENSITY   = 0.15         # fraction of H/V-line pixels that a real grid must have

# Per-page wall-clock timeout.  Tesseract on noisy 1900s handwriting can spin
# for many minutes; SIGALRM gives us a hard ceiling.  No-op on Windows.
_PAGE_TIMEOUT_S = int(os.environ.get("GRID_PAGE_TIMEOUT_S", "45"))
_HAS_SIGALRM    = hasattr(signal, "SIGALRM")


class _GridPageTimeout(Exception):
    """Raised by the SIGALRM handler when a page takes too long."""


def _sigalrm_handler(signum, frame):
    raise _GridPageTimeout(f"grid page timed out after {_PAGE_TIMEOUT_S}s")


def _line_density_score(region_bgr) -> float:
    """
    Measures how much of a candidate region consists of horizontal and
    vertical line structure — the hallmark of a printed grid.

    Text bands score near 0.  A proper H×V grid scores 0.15+.
    Returns a float in [0, 1].

    Algorithm: Otsu-binarise → morphological OPEN with wide H kernel
    (extracts horizontal runs) + tall V kernel (extracts vertical runs).
    Sum both, normalise by total pixels.
    """
    gray = cv2.cvtColor(region_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    H, W = binary.shape
    if H < 10 or W < 10:
        return 0.0

    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, W // 8), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, H // 8)))
    hl = cv2.morphologyEx(binary, cv2.MORPH_OPEN, hk)
    vl = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vk)

    total_pixels = 255 * H * W + 1          # +1 avoids div-by-zero
    h_pct = hl.sum() / total_pixels
    v_pct = vl.sum() / total_pixels
    return float(min(1.0, (h_pct + v_pct) * 20))


def _extract_best_candidate(cv_image, w_min, w_max, h_min, h_max,
                            ar_min=None, ar_max=None):
    """
    Run all six CV extractors against `cv_image`. Keep candidates whose
    width/height fall in [w_min..w_max] x [h_min..h_max], whose aspect
    ratio is in [ar_min..ar_max] (defaults to _AR_MIN.._AR_MAX), and whose
    line-density score is at least _MIN_LINE_DENSITY (rejects text bands that
    look rectangular but contain no grid lines).

    ar_min / ar_max override the module-level defaults when supplied.  This
    lets tier-aware callers restrict the AR window so landscape data-tables on
    mid/late-era forms (AR ≈ 1.2-1.6) are not mistaken for portrait PLSS grids
    (AR ≈ 0.55-0.90 for LATE tier, 0.45-0.75 for MID).

    Ranking: density first (morphological H/V line fraction), then smallest area
    as tiebreaker — preferring the tighter PLSS grid candidate over large
    data tables that may sneak through the AR filter.
    Returns (grid_img, (x, y, w, h), method_name) or (None, None, None).
    """
    _ar_lo = ar_min if ar_min is not None else _AR_MIN
    _ar_hi = ar_max if ar_max is not None else _AR_MAX
    candidates = []
    for name, func in _METHODS:
        try:
            grid_img, bbox = func(cv_image)
        except Exception:
            continue
        if grid_img is None or bbox is None or grid_img.size == 0:
            continue
        x, y, w, h = bbox
        if not is_valid_candidate(bbox, cv_image):
            continue
        ar   = w / h if h > 0 else 0
        area = w * h
        if not (_ar_lo <= ar <= _ar_hi
                and w_min <= w <= w_max
                and h_min <= h <= h_max
                and area >= w_min * h_min):
            continue
        density = _line_density_score(grid_img)
        if density < _MIN_LINE_DENSITY:
            continue
        candidates.append({"method": name, "region": grid_img,
                           "bbox": bbox, "area": area, "density": density})
    if not candidates:
        return None, None, None
    # Density is the primary signal; use smallest area as tiebreaker so we
    # prefer the compact PLSS grid over larger data-table false positives.
    best = max(candidates, key=lambda c: (c["density"], -c["area"]))
    return best["region"], best["bbox"], best["method"]


def extract_grid_region_combined(cv_image):
    """Strict-band convenience wrapper (kept for backward compatibility)."""
    return _extract_best_candidate(cv_image, *GRID_W_STRICT, *GRID_H_STRICT)


def _try_anchor_on_page(manager, page_num_1indexed, cv_img,
                        w_min, w_max, h_min, h_max, log,
                        ar_min=None, ar_max=None,
                        anchor_crop_params: dict | None = None):
    """
    Try the structural-anchor strategy on a single page.

    Returns (grid_img, full_page_bbox, method_label, anchor_phrase) or
    (None, None, None, None).  ``anchor_phrase`` is the matched text
    (e.g. 'Spot Well Correctly') forwarded to classify_form_type().
    ``method_label`` is prefixed with 'anchor_<position>_' so the summary
    CSV can tell anchor hits apart from full-page CV hits.

    ar_min / ar_max are forwarded to _extract_best_candidate for tier-aware
    aspect-ratio filtering (see its docstring).

    anchor_crop_params: optional dict with keys look_above, look_below,
    side_padding — overrides the defaults in crop_box_from_anchor().
    When None the crop_box_from_anchor() defaults are used.
    """
    try:
        annotations, _ = get_page_annotations(
            manager=manager, page_num=page_num_1indexed - 1,
        )
    except Exception as exc:
        log.debug("Anchor OCR failed page %d: %s", page_num_1indexed, exc)
        return None, None, None, None
    if not annotations:
        return None, None, None, None

    anchor_bbox, pos, phrase = find_grid_anchor(annotations)
    if anchor_bbox is None:
        return None, None, None, None

    ph, pw = cv_img.shape[:2]
    _cp = anchor_crop_params or {}
    crop_box = crop_box_from_anchor(
        anchor_bbox, pos, pw, ph,
        look_above=_cp.get("look_above", 700),
        look_below=_cp.get("look_below", 1100),
        side_padding=_cp.get("side_padding", 450),
    )
    if crop_box is None:
        return None, None, None, None

    cx0, cy0, cx1, cy1 = crop_box
    region = cv_img[cy0:cy1, cx0:cx1]
    log.debug("Anchor crop: pos=%s bbox=%s crop=(%d,%d,%d,%d) size=%dx%d",
              pos, anchor_bbox, cx0, cy0, cx1, cy1,
              cx1 - cx0, cy1 - cy0)
    grid_img, bbox, method = _extract_best_candidate(
        region, w_min, w_max, h_min, h_max,
        ar_min=ar_min, ar_max=ar_max,
    )
    if grid_img is None:
        return None, None, None, None

    # Translate the bbox back into full-page coordinates.
    bx, by, bw, bh = bbox
    full_bbox = (bx + cx0, by + cy0, bw, bh)
    log.info("Grid via anchor %r on page %d (%s of anchor)",
             phrase, page_num_1indexed, pos)
    # Return phrase so the caller can pass it to classify_form_type().
    return grid_img, full_bbox, f"anchor_{pos}_{method}", phrase


def process_single_grid(
    manager: PDFDocumentManager,
    output_dir: Path,
    pdf_stem: str,
    logger: logging.Logger | None = None,
    relaxed: bool = False,
    skip_anchor: bool = False,
    collection_num: int | None = None,
) -> dict:
    """
    Detect the section-township-range grid box on one of the PDF's pages.

    `relaxed=False`    -- strict size band, FORWARD page order (page 1 first).
    `relaxed=True`     -- loose size band, FORWARD page order (retry path).
    `skip_anchor=False` (always) -- attempt the structural-anchor OCR pass
                          first; falls through to full-page CV if no phrase
                          is found.  Anchor phrases are PRINTED text on all
                          collection tiers (confirmed Jun 2025 inspection of
                          Colls 1–13).
    `collection_num`   -- used to look up tier-appropriate aspect-ratio bounds
                          from config.TIER_GRID_AR.  When supplied, prevents
                          false positives such as landscape data-tables on LATE
                          forms (C11-C12) being mistaken for the PLSS grid
                          (which should be portrait, AR ≈ 0.55-0.90).

    Each page is tried first with the structural anchor (unless skip_anchor),
    then falls back to the full-page CV extractors.
    """
    from config import TIER_GRID_AR, TIER_GRID_W_MAX, tier_for as _tier_for
    log = logger or logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Tier-aware AR bounds and anchor crop params.
    _tier = _tier_for(collection_num)
    _tier_ar = TIER_GRID_AR.get(_tier, (_AR_MIN, _AR_MAX))
    ar_min_tier, ar_max_tier = _tier_ar
    _anchor_crop = ANCHOR_CROP_BY_TIER.get(_tier)
    log.debug("Grid tier=%s (coll=%s): AR=%.2f–%.2f  anchor_crop=%s",
              _tier, collection_num, ar_min_tier, ar_max_tier,
              _anchor_crop)

    if relaxed:
        w_min, w_max = GRID_W_LOOSE
        h_min, h_max = GRID_H_LOOSE
        mode         = "loose"
    else:
        w_min, w_max = GRID_W_STRICT
        h_min, h_max = GRID_H_STRICT
        mode         = "strict"

    # Tier width cap: early/transition grids never exceed ~558 px (p99=411,
    # measured from 1,000 dot-verified detections), while the mid-page casing/
    # water-sands tables that fool the detector are 577-812 px.  Capping w_max
    # rejects the table at candidate level so the real grid can win.
    _w_cap = TIER_GRID_W_MAX.get(_tier)
    if _w_cap:
        w_max = min(w_max, _w_cap)

    # Always iterate FORWARD (page 1 first).  Grid is on the front/first page
    # for the vast majority of forms across all 13 collections.  Reverse order
    # caused false-positive detections on back-page elements before reaching
    # the real grid on page 1.
    page_order = lambda pages: pages

    result = {"detected": False, "page": None, "bbox": None,
              "method": None, "confidence": 0, "image_path": None,
              # Form-type classification fields (populated after detection).
              "form_type": None, "grid_zone": None,
              "str_zone": None, "county_format_hint": None,
              "str_strategy_hint": None, "anchor_phrase": None}

    try:
        pages = list(manager.iter_cv2_pages())
        for page_num, cv_img in page_order(pages):
            # Arm per-page timeout (Linux only; no-op on Windows).
            if _HAS_SIGALRM:
                _old_handler = signal.signal(signal.SIGALRM, _sigalrm_handler)
                signal.alarm(_PAGE_TIMEOUT_S)
            try:
                anchor_phrase_found = None
                # Strategy 1: structural anchor + crop.
                # Skipped for early/transition tier: handwritten docs have no
                # printed anchor phrase and Tesseract just spins uselessly.
                if not skip_anchor:
                    grid_img, bbox, method, anchor_phrase_found = _try_anchor_on_page(
                        manager, page_num, cv_img, w_min, w_max, h_min, h_max, log,
                        ar_min=ar_min_tier, ar_max=ar_max_tier,
                        anchor_crop_params=_anchor_crop,
                    )
                else:
                    grid_img = bbox = method = None

                # Strategy 2: hand-measured grid-envelope crop.
                # The user's manual review drew 2,269 grid boxes across all
                # collections (location/recipes.py GRID_ENVELOPES).  On forms
                # like C8 the grid is NESTED inside a wider form-section box:
                # full-crop extractors return the 600px-wide outer rectangle
                # (AR-fail) and never isolate the 140px grid.  Cropping to the
                # measured envelope makes the grid the dominant rectangle.
                if grid_img is None and collection_num:
                    from location.recipes import GRID_ENVELOPES, PAD
                    _env = GRID_ENVELOPES.get(collection_num)
                    if _env is not None:
                        _ph, _pw = cv_img.shape[:2]
                        ex0 = max(0,   int((_env[0] - PAD) * _pw))
                        ey0 = max(0,   int((_env[1] - PAD) * _ph))
                        ex1 = min(_pw, int((_env[2] + PAD) * _pw))
                        ey1 = min(_ph, int((_env[3] + PAD) * _ph))
                        if ex1 - ex0 > 50 and ey1 - ey0 > 50:
                            g2, b2, m2 = _extract_best_candidate(
                                cv_img[ey0:ey1, ex0:ex1],
                                w_min, w_max, h_min, h_max,
                                ar_min=ar_min_tier, ar_max=ar_max_tier,
                            )
                            if g2 is not None:
                                bx, by, bw_, bh_ = b2
                                grid_img = g2
                                bbox     = (bx + ex0, by + ey0, bw_, bh_)
                                method   = f"envelope_{m2}"

                # Strategy 3: full-page CV scan (fallback).
                if grid_img is None:
                    grid_img, bbox, method = _extract_best_candidate(
                        cv_img, w_min, w_max, h_min, h_max,
                        ar_min=ar_min_tier, ar_max=ar_max_tier,
                    )
            except _GridPageTimeout:
                log.warning("Grid page %d timed out after %ds — skipping",
                            page_num, _PAGE_TIMEOUT_S)
                grid_img = None
            finally:
                if _HAS_SIGALRM:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, _old_handler)

            if grid_img is None:
                log.debug("Page %d -- no grid (%s)", page_num, mode)
                continue

            out_path = output_dir / f"{pdf_stem}_page_{page_num:02d}_grid.png"
            cv2.imwrite(str(out_path), grid_img)

            # Confidence based on morphological line-density score (0–1 → 0–100).
            # Line density is a much stronger signal than aspect-ratio deviation:
            # text blocks score ~0, proper H×V grids score 0.15+.
            density = _line_density_score(grid_img)
            conf    = min(100, int(density * 100))

            log.info("Grid -- page %d  method=%s  bbox=%s  density=%.3f  conf=%d  mode=%s",
                     page_num, method, bbox, density, conf, mode)

            # Classify the form type so downstream stages (location, county)
            # can restrict their search to the expected page region.
            ph, pw = cv_img.shape[:2]
            bx, by, bw, bh = bbox
            ar_detected = bw / bh if bh > 0 else 1.0
            form_info = classify_form_type(
                grid_bbox=(bx, by, bw, bh),
                page_w=pw, page_h=ph,
                anchor_phrase=anchor_phrase_found,
                grid_ar=ar_detected,
                tier=_tier,
            )
            log.info("Form classifier: %s  zone=%s  str_zone=%s  ar=%.2f",
                     form_info["form_type"], form_info["grid_zone"],
                     form_info["str_zone"], ar_detected)

            result.update(
                detected=True, page=page_num, bbox=list(bbox),
                method=method, confidence=conf,
                image_path=str(out_path),
                # Form-type classification — consumed by location + county dispatch
                form_type=form_info["form_type"],
                grid_zone=form_info["grid_zone"],
                str_zone=form_info["str_zone"],
                county_format_hint=form_info["county_format_hint"],
                str_strategy_hint=form_info["str_strategy_hint"],
                anchor_phrase=anchor_phrase_found,
            )
            return result

    except Exception as exc:
        log.error("Grid extraction error: %s", exc, exc_info=True)
        result["error"] = str(exc)
        return result

    log.warning("No grid detected (%s mode)", mode)
    result["error"] = "not_detected"
    return result

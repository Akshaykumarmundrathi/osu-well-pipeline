"""
Grid extraction pipeline.

process_single_grid(manager, output_dir, pdf_stem, logger, relaxed=False) -> dict
  {detected, page, bbox, method, confidence, image_path, error?}

Strategy
--------
Each page is tried in this order:

  1. **Structural anchor**: OCR the page (Vision API; cached on the
     manager), look for one of the printed phrases that always sits
     beside the STR grid ('spot well located' / 'Locate Well' /
     'Locate Well correctly'). When found, crop a fixed region above
     or below the anchor and run the CV extractors on the crop. This
     succeeds even when the grid's overall size is unusual, because
     we've already isolated its location.

  2. **Full-page CV** (current fallback): run all six extractors on
     the entire page image and pick the best 4-vertex candidate.

Pages are iterated in REVERSE for `relaxed=False` (grid is usually on
the back page of well-record sheets) and FORWARD for `relaxed=True`
(retry path, looser size band).
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
from grid.anchors import crop_box_from_anchor, find_grid_anchor
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

_AR_MIN, _AR_MAX    = 0.78, 1.30   # accept slightly landscape / portrait grids
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


def _extract_best_candidate(cv_image, w_min, w_max, h_min, h_max):
    """
    Run all six CV extractors against `cv_image`. Keep candidates whose
    width/height fall in [w_min..w_max] x [h_min..h_max], whose aspect
    ratio is in [_AR_MIN.._AR_MAX], and whose line-density score is at
    least _MIN_LINE_DENSITY (rejects text bands that look rectangular but
    contain no grid lines).

    Ranking: density first (morphological H/V line fraction), area as
    tiebreaker.  Returns (grid_img, (x, y, w, h), method_name) or
    (None, None, None).
    """
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
        if not (_AR_MIN <= ar <= _AR_MAX
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
    best = max(candidates, key=lambda c: (c["density"], c["area"]))
    return best["region"], best["bbox"], best["method"]


def extract_grid_region_combined(cv_image):
    """Strict-band convenience wrapper (kept for backward compatibility)."""
    return _extract_best_candidate(cv_image, *GRID_W_STRICT, *GRID_H_STRICT)


def _try_anchor_on_page(manager, page_num_1indexed, cv_img,
                        w_min, w_max, h_min, h_max, log):
    """
    Try the structural-anchor strategy on a single page.

    Returns (grid_img, full_page_bbox, method_label) or (None, None, None).
    `method_label` is prefixed with 'anchor_<position>_' so the summary
    CSV can tell anchor hits apart from full-page CV hits.
    """
    try:
        annotations, _ = get_page_annotations(
            manager=manager, page_num=page_num_1indexed - 1,
        )
    except Exception as exc:
        log.debug("Anchor OCR failed page %d: %s", page_num_1indexed, exc)
        return None, None, None
    if not annotations:
        return None, None, None

    anchor_bbox, pos, phrase = find_grid_anchor(annotations)
    if anchor_bbox is None:
        return None, None, None

    ph, pw = cv_img.shape[:2]
    crop_box = crop_box_from_anchor(anchor_bbox, pos, pw, ph)
    if crop_box is None:
        return None, None, None

    cx0, cy0, cx1, cy1 = crop_box
    region = cv_img[cy0:cy1, cx0:cx1]
    grid_img, bbox, method = _extract_best_candidate(
        region, w_min, w_max, h_min, h_max,
    )
    if grid_img is None:
        return None, None, None

    # Translate the bbox back into full-page coordinates.
    bx, by, bw, bh = bbox
    full_bbox = (bx + cx0, by + cy0, bw, bh)
    log.info("Grid via anchor %r on page %d (%s of anchor)",
             phrase, page_num_1indexed, pos)
    return grid_img, full_bbox, f"anchor_{pos}_{method}"


def process_single_grid(
    manager: PDFDocumentManager,
    output_dir: Path,
    pdf_stem: str,
    logger: logging.Logger | None = None,
    relaxed: bool = False,
    skip_anchor: bool = False,
) -> dict:
    """
    Detect the section-township-range grid box on one of the PDF's pages.

    `relaxed=False`    -- strict size band, REVERSE page order (back-page first).
    `relaxed=True`     -- loose size band, FORWARD page order (retry path).
    `skip_anchor=True` -- skip the structural-anchor OCR pass and go straight
                          to full-page CV.  Use for early/transition tiers
                          (handwritten 1911–1950s PDFs) where there is no
                          printable anchor phrase and Tesseract just spins.

    Each page is tried first with the structural anchor (unless skip_anchor),
    then falls back to the full-page CV extractors.
    """
    log = logger or logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)

    if relaxed:
        w_min, w_max = GRID_W_LOOSE
        h_min, h_max = GRID_H_LOOSE
        page_order   = lambda pages: pages
        mode         = "loose"
    else:
        w_min, w_max = GRID_W_STRICT
        h_min, h_max = GRID_H_STRICT
        page_order   = reversed
        mode         = "strict"

    result = {"detected": False, "page": None, "bbox": None,
              "method": None, "confidence": 0, "image_path": None}

    try:
        pages = list(manager.iter_cv2_pages())
        for page_num, cv_img in page_order(pages):
            # Arm per-page timeout (Linux only; no-op on Windows).
            if _HAS_SIGALRM:
                _old_handler = signal.signal(signal.SIGALRM, _sigalrm_handler)
                signal.alarm(_PAGE_TIMEOUT_S)
            try:
                # Strategy 1: structural anchor + crop.
                # Skipped for early/transition tier: handwritten docs have no
                # printed anchor phrase and Tesseract just spins uselessly.
                if not skip_anchor:
                    grid_img, bbox, method = _try_anchor_on_page(
                        manager, page_num, cv_img, w_min, w_max, h_min, h_max, log,
                    )
                else:
                    grid_img = bbox = method = None

                # Strategy 2: full-page CV scan (fallback).
                if grid_img is None:
                    grid_img, bbox, method = _extract_best_candidate(
                        cv_img, w_min, w_max, h_min, h_max,
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
            result.update(detected=True, page=page_num, bbox=list(bbox),
                          method=method, confidence=conf,
                          image_path=str(out_path))
            return result

    except Exception as exc:
        log.error("Grid extraction error: %s", exc, exc_info=True)
        result["error"] = str(exc)
        return result

    log.warning("No grid detected (%s mode)", mode)
    result["error"] = "not_detected"
    return result

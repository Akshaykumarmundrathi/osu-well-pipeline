"""
Grid extraction pipeline.

process_single_grid(manager, output_dir, pdf_stem, logger, relaxed=False) -> dict
  {detected, page, bbox, method, confidence, image_path, error?}

Two-pass strategy:
  - First call uses the strict size band (GRID_W/H_STRICT). Iterates pages
    in reverse (grid is virtually always on the back page of well-record
    forms). Returns on the first detected page.
  - Retry call (`relaxed=True`) widens the size band to GRID_W/H_LOOSE
    and tries ALL pages forward, catching grids that fall outside the
    common size range.
"""

import logging
from pathlib import Path

import cv2

from config import (
    GRID_H_LOOSE, GRID_H_STRICT,
    GRID_W_LOOSE, GRID_W_STRICT,
)
from grid.extractors import (
    extract_grid_region_adaptive,
    extract_grid_region_canny,
    extract_grid_region_corners,
    extract_grid_region_hough,
    extract_grid_region_otsu,
    extract_grid_region_rotated,
)
from grid.filters import is_valid_candidate
from pdf.pdf_manager import PDFDocumentManager

_METHODS = [
    extract_grid_region_adaptive,
    extract_grid_region_otsu,
    extract_grid_region_canny,
    extract_grid_region_hough,
    extract_grid_region_rotated,
    extract_grid_region_corners,
]

_AR_MIN, _AR_MAX = 0.85, 1.15   # square-ish aspect ratio


def _extract_best_candidate(cv_image, w_min, w_max, h_min, h_max):
    """
    Run all six extractors against `cv_image`. Among returned candidates,
    pick the one with the largest area satisfying the aspect-ratio band
    and the supplied (w_min..w_max, h_min..h_max) constraints.
    Returns (grid_img, bbox, method_name) or (None, None, None).
    """
    candidates = []
    for func in _METHODS:
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
        if (_AR_MIN <= ar <= _AR_MAX
                and w_min <= w <= w_max
                and h_min <= h <= h_max
                and area >= w_min * h_min):
            candidates.append({"method": func.__name__, "region": grid_img,
                               "bbox": bbox, "area": area})
    if not candidates:
        return None, None, None
    best = max(candidates, key=lambda c: c["area"])
    return best["region"], best["bbox"], best["method"]


def extract_grid_region_combined(cv_image):
    """Strict-band convenience wrapper (kept for backward compatibility)."""
    return _extract_best_candidate(
        cv_image, *GRID_W_STRICT, *GRID_H_STRICT,
    )


def process_single_grid(
    manager: PDFDocumentManager,
    output_dir: Path,
    pdf_stem: str,
    logger: logging.Logger | None = None,
    relaxed: bool = False,
) -> dict:
    """
    Detect the section-township-range grid box on one of the PDF's pages.

    `relaxed=False` (default): strict size band, iterate pages REVERSED
      so 2-page docs (grid on back page) hit on the first try.
    `relaxed=True` (retry path): loose size band, iterate pages FORWARD
      so out-of-range grids on early pages are caught.
    """
    log = logger or logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)

    if relaxed:
        w_min, w_max = GRID_W_LOOSE
        h_min, h_max = GRID_H_LOOSE
        page_order   = lambda pages: pages          # forward
        mode         = "loose"
    else:
        w_min, w_max = GRID_W_STRICT
        h_min, h_max = GRID_H_STRICT
        page_order   = reversed                     # back-page first
        mode         = "strict"

    result = {"detected": False, "page": None, "bbox": None,
              "method": None, "confidence": 0, "image_path": None}

    try:
        pages = list(manager.iter_cv2_pages())
        for page_num, cv_img in page_order(pages):
            grid_img, bbox, method = _extract_best_candidate(
                cv_img, w_min, w_max, h_min, h_max,
            )
            if grid_img is None:
                log.debug("Page %d -- no grid (%s)", page_num, mode)
                continue

            out_path = output_dir / f"{pdf_stem}_page_{page_num:02d}_grid.png"
            cv2.imwrite(str(out_path), grid_img)

            x, y, w, h = bbox
            ar   = w / h if h > 0 else 0
            conf = max(0, 100 - int(abs(1.0 - ar) * 100))

            log.info("Grid -- page %d  method=%s  bbox=%s  conf=%d  mode=%s",
                     page_num, method, bbox, conf, mode)
            result.update(detected=True, page=page_num, bbox=list(bbox),
                          method=method, confidence=conf,
                          image_path=str(out_path))
            return result

    except Exception as exc:
        log.error("Grid extraction error: %s", exc, exc_info=True)
        result["error"] = str(exc)
        return result

    # No grid found across all pages — flag as a retriable failure so the
    # dispatcher knows to attempt the relaxed-band retry.
    log.warning("No grid detected (%s mode)", mode)
    result["error"] = "not_detected"
    return result

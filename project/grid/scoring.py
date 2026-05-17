"""
Grid extraction pipeline.

process_single_grid(manager, output_dir, logger) -> dict
  {detected, page, bbox, method, confidence, image_path}

For 2-page PDFs: scans every page, stops at first successful detection.
"""

import logging
from pathlib import Path

import cv2

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

_AR_MIN, _AR_MAX = 0.85, 1.15
_W_MIN,  _W_MAX  = 280,  850
_H_MIN,  _H_MAX  = 280,  850


def extract_grid_region_combined(cv_image):
    """Try all methods, return (grid_img, bbox, method_name) for the best candidate."""
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
                and _W_MIN <= w <= _W_MAX
                and _H_MIN <= h <= _H_MAX
                and area >= _W_MIN * _H_MIN):
            candidates.append({"method": func.__name__, "region": grid_img,
                                "bbox": bbox, "area": area})
    if not candidates:
        return None, None, None
    best = max(candidates, key=lambda c: c["area"])
    return best["region"], best["bbox"], best["method"]


def process_single_grid(
    manager: PDFDocumentManager,
    output_dir: Path,
    pdf_stem: str,
    logger: logging.Logger | None = None,
) -> dict:
    """
    Scans every page of the PDF (expected ≤2 pages).
    Saves the first detected grid image, returns metadata dict.
    """
    log = logger or logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = {"detected": False, "page": None, "bbox": None,
              "method": None, "confidence": 0, "image_path": None}

    try:
        # Grid is almost always on the LAST page (back of well-record sheet).
        # Iterate in reverse so the common case hits on the first try.
        pages = list(manager.iter_cv2_pages())
        for page_num, cv_img in reversed(pages):
            grid_img, bbox, method = extract_grid_region_combined(cv_img)
            if grid_img is None:
                log.debug("Page %d — no grid", page_num)
                continue

            out_path = output_dir / f"{pdf_stem}_page_{page_num:02d}_grid.png"
            cv2.imwrite(str(out_path), grid_img)

            x, y, w, h = bbox
            ar   = w / h if h > 0 else 0
            conf = max(0, 100 - int(abs(1.0 - ar) * 100))

            log.info("Grid — page %d  method=%s  bbox=%s  conf=%d",
                     page_num, method, bbox, conf)
            result.update(detected=True, page=page_num, bbox=list(bbox),
                          method=method, confidence=conf,
                          image_path=str(out_path))
            return result   # stop after first detected page

    except Exception as exc:
        log.error("Grid extraction error: %s", exc, exc_info=True)
        result["error"] = str(exc)

    log.warning("No grid detected")
    return result

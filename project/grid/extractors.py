"""
Grid-region detection methods.

Each extractor binarizes the page in a different way, then hands the binary
mask to `_largest_quad_bbox()` which returns the largest 4-vertex contour.
Hough / corners / minAreaRect use their own bbox derivation since they
don't produce a clean binary mask.

All functions return (cropped_image | None, (x, y, w, h) | None).
"""

import cv2
import numpy as np

_BUFFER = 5   # pixels added around bbox when cropping


def _largest_quad_bbox(binary, cv_image, min_side: int = 50):
    """
    Find the largest 4-vertex contour in `binary` whose width and height
    each exceed `min_side` pixels. Returns (cropped_region, (x,y,w,h))
    from the original `cv_image`. Returns (None, None) if no candidate
    found.
    """
    contours, _ = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
    )
    best = None
    max_area = 0
    for cnt in contours:
        peri   = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) != 4:
            continue
        x, y, w, h = cv2.boundingRect(approx)
        area = w * h
        if area > max_area and w > min_side and h > min_side:
            max_area, best = area, (x, y, w, h)

    if best is None:
        return None, None
    return _crop_with_buffer(cv_image, best), best


def _crop_with_buffer(cv_image, bbox):
    """Crop `cv_image` to bbox plus a small buffer; return None if degenerate."""
    x, y, w, h = bbox
    y0 = max(0, y - _BUFFER)
    y1 = min(cv_image.shape[0], y + h + _BUFFER)
    x0 = max(0, x - _BUFFER)
    x1 = min(cv_image.shape[1], x + w + _BUFFER)
    if y1 <= y0 or x1 <= x0:
        return None
    return cv_image[y0:y1, x0:x1]


def extract_grid_region_adaptive(cv_image):
    """Adaptive thresholding + morphology to isolate horizontal+vertical rules."""
    gray   = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    gray   = cv2.GaussianBlur(gray, (5, 5), 0)
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV, blockSize=15, C=8,
    )
    h_kern = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    v_kern = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    h_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kern, iterations=2)
    v_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kern, iterations=2)
    grid    = cv2.addWeighted(h_lines, 0.5, v_lines, 0.5, 0.0)
    grid    = cv2.dilate(grid, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
                         iterations=2)
    return _largest_quad_bbox(grid, cv_image)


def extract_grid_region_otsu(cv_image):
    """Otsu thresholding + dilation; cheapest of the binary-mask methods."""
    gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(gray, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    dilated = cv2.dilate(binary,
                         cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
                         iterations=3)
    return _largest_quad_bbox(dilated, cv_image)


def extract_grid_region_canny(cv_image):
    """Canny edges + dilation; good when grid lines are thin/faint."""
    gray  = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    blur  = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150)
    dilated = cv2.dilate(edges,
                         cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
                         iterations=2)
    return _largest_quad_bbox(dilated, cv_image)


def extract_grid_region_hough(cv_image):
    """
    Probabilistic Hough transform: require ≥5 horizontal AND ≥5 vertical
    lines, each spanning ≥10% of the corresponding image dimension. The
    bbox is the union of all qualifying line endpoints.
    """
    gray  = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    blur  = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180,
                            threshold=50, minLineLength=50, maxLineGap=10)
    if lines is None:
        return None, None

    H, W = cv_image.shape[:2]
    h_lines, v_lines = [], []
    for x1, y1, x2, y2 in (ln[0] for ln in lines):
        angle  = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        length = np.hypot(x2 - x1, y2 - y1)
        if abs(angle) < 5 and length > W * 0.1:
            h_lines.append((x1, y1, x2, y2))
        elif abs(abs(angle) - 90) < 5 and length > H * 0.1:
            v_lines.append((x1, y1, x2, y2))

    if len(h_lines) < 5 or len(v_lines) < 5:
        return None, None

    xs = [p for ln in h_lines + v_lines for p in (ln[0], ln[2])]
    ys = [p for ln in h_lines + v_lines for p in (ln[1], ln[3])]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    w, h = x_max - x_min, y_max - y_min
    if w <= 10 or h <= 10:
        return None, None

    bbox = (x_min, y_min, w, h)
    return _crop_with_buffer(cv_image, bbox), bbox


def extract_grid_region_rotated(cv_image):
    """
    Otsu + minAreaRect: handles slightly rotated grids. Returns the
    axis-aligned bounding box of the contour whose minAreaRect has the
    largest area (no deskewing applied).
    """
    gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(gray, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    dilated = cv2.dilate(binary,
                         cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
                         iterations=2)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    best_cnt, max_area = None, 0
    for cnt in contours:
        if cv2.contourArea(cnt) < 2500:
            continue
        rect = cv2.minAreaRect(cnt)
        w, h = rect[1]
        area = w * h
        if area > max_area and w > 10 and h > 10:
            max_area, best_cnt = area, cnt

    if best_cnt is None:
        return None, None
    bbox = cv2.boundingRect(best_cnt)
    return _crop_with_buffer(cv_image, bbox), bbox


def extract_grid_region_corners(cv_image):
    """
    Shi-Tomasi corner detection: bbox spans the detected corner cloud.
    Useful when interior cell corners are visually prominent.
    """
    gray    = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    corners = cv2.goodFeaturesToTrack(
        blurred, maxCorners=150, qualityLevel=0.01, minDistance=10,
    )
    if corners is None or len(corners) < 10:
        return None, None

    corners = np.int32(corners).reshape(-1, 2)
    x_min, y_min = np.min(corners, axis=0)
    x_max, y_max = np.max(corners, axis=0)
    w, h = x_max - x_min, y_max - y_min
    if w < 50 or h < 50:
        return None, None

    bbox = (x_min, y_min, w, h)
    return _crop_with_buffer(cv_image, bbox), bbox

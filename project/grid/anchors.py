"""
Structural grid-anchor detection.

Across decades, well-record forms place the STR grid relative to one of a
few printed phrases:

  - "Spot well located"        -> grid is immediately BELOW
  - "Spot well"                -> grid is BELOW (variant)
  - "Locate Well correctly"    -> grid is immediately ABOVE
  - "Locate Well"              -> grid is ABOVE (older variant)

This module finds the anchor in OCR annotations and returns the
page-pixel region to crop before running CV-based grid extractors.
Cropping shrinks the search space and removes false-positive
rectangles elsewhere on the form.
"""

import re

# Order matters: more specific phrases tried first so "Locate Well correctly"
# matches before the looser "Locate Well".
_ANCHORS = [
    (re.compile(r"spot\s+well\s+located", re.I), "below"),
    (re.compile(r"locate\s+well\s+correctly", re.I), "above"),
    (re.compile(r"locate\s+well\b", re.I), "above"),
    (re.compile(r"spot\s+well\b",   re.I), "below"),
]


def find_grid_anchor(annotations):
    """
    Scan OCR annotations for a grid-anchor phrase.

    Returns (anchor_bbox, position, matched_phrase) where:
      anchor_bbox     = (x_min, y_min, x_max, y_max) in page pixels
      position        = 'below' | 'above'  -- where the grid sits relative
                        to the anchor
      matched_phrase  = the lowercase phrase matched (for telemetry)

    Returns (None, None, None) when no anchor is present.
    """
    if not annotations or len(annotations) < 2:
        return None, None, None

    full_text = annotations[0].description or ""
    for pat, pos in _ANCHORS:
        m = pat.search(full_text)
        if not m:
            continue
        phrase = m.group(0).lower()
        bbox = _phrase_bbox(annotations[1:], phrase.split())
        if bbox:
            return bbox, pos, phrase
    return None, None, None


def _phrase_bbox(token_anns, words):
    """
    Find consecutive token annotations whose lowercased text matches each
    of `words` in order; return their union bounding box.
    """
    n = len(words)
    if n == 0:
        return None
    descs = [(ann.description or "").lower().strip(".,:") for ann in token_anns]
    for i in range(len(descs) - n + 1):
        if all(words[k] in descs[i + k] for k in range(n)):
            return _union_bbox(token_anns[i:i + n])
    return None


def _union_bbox(anns):
    """Union bbox of a list of annotations; None if any is malformed."""
    xs, ys = [], []
    for a in anns:
        if not a.bounding_poly or not a.bounding_poly.vertices:
            continue
        xs.extend(v.x for v in a.bounding_poly.vertices)
        ys.extend(v.y for v in a.bounding_poly.vertices)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def crop_box_from_anchor(anchor_bbox, position: str,
                         page_w: int, page_h: int,
                         crop_height: int = 1100,
                         side_padding: int = 250):
    """
    Compute a page region to crop before running grid extractors.

    `position='below'`: region spans from anchor.y_max down to either
       anchor.y_max + crop_height or page bottom, whichever smaller.
    `position='above'`: region spans from max(0, anchor.y_min - crop_height)
       up to anchor.y_min.

    Sides are extended by `side_padding` pixels to capture the full grid
    even when the anchor text is narrower than the grid box.

    Returns (x0, y0, x1, y1) or None when the computed region is empty.
    """
    if anchor_bbox is None:
        return None
    x0, y0, x1, y1 = anchor_bbox

    cx0 = max(0,      x0 - side_padding)
    cx1 = min(page_w, x1 + side_padding)

    if position == "below":
        cy0 = y1
        cy1 = min(page_h, y1 + crop_height)
    elif position == "above":
        cy1 = y0
        cy0 = max(0, y0 - crop_height)
    else:
        return None

    if cy1 <= cy0 or cx1 <= cx0:
        return None
    return (int(cx0), int(cy0), int(cx1), int(cy1))

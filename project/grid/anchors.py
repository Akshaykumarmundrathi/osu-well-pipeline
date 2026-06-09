"""
Structural grid-anchor detection.

Well-record forms across all collections (1911-2024) carry one of these
printed phrases near the dot-grid image:

  Phrase                          Seen in         Grid position relative to text
  ─────────────────────────────── ─────────────── ───────────────────────────────
  "Spot Well Correctly"           Colls 1-3       Grid is BELOW the text
  "Spot Well Located"             Colls 1-2       Grid is BELOW the text
  "Spot Well"                     Colls 1-6       Grid is BELOW the text
  "Locate Well Correctly"         Colls 1-4       Grid is ABOVE the text label
  "Locate Well And Dotline Lease" Coll 9          Grid is ABOVE the text label
  "Locate Well" / "LOCATE WELL"  Colls 5-11      Grid is ABOVE the text label

The crop produced by crop_box_from_anchor() is intentionally BIDIRECTIONAL
(extends both above and below the anchor) so that positional uncertainty —
the grid label can sit inside the grid box, just below it, or just above it —
is tolerated without requiring the caller to know the exact orientation.
"""

import re

# Order matters: most specific patterns first.
_ANCHORS = [
    (re.compile(r"spot\s+well\s+located",          re.I), "below"),
    (re.compile(r"locate\s+well\s+and\s+dotline",  re.I), "above"),   # Coll 9 variant
    (re.compile(r"locate\s+well\s+correctly",       re.I), "above"),
    (re.compile(r"locate\s+well\b",                 re.I), "above"),
    (re.compile(r"spot\s+well\b",                   re.I), "below"),
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
                         look_above: int = 700,
                         look_below: int = 1100,
                         side_padding: int = 450):
    """
    Compute a page region to crop before running grid extractors.

    The crop extends `look_above` pixels above the anchor and `look_below`
    pixels below it.  `position` is used to bias the vertical extent:

      position='above'  → grid is ABOVE the anchor text.
                          Use the caller-supplied look_above (or default 700).
                          Clip look_below to max(100, look_below // 6) so we
                          don't accidentally grab large data tables below.

      position='below'  → grid is BELOW the anchor text.
                          Use the caller-supplied look_below (or default 1100).
                          Clip look_above to max(100, look_above // 6).

    When the caller passes explicit look_above / look_below values the clipping
    is skipped (the caller already knows what it wants).

    `side_padding` controls the horizontal extent around the anchor x-range.
    Generous defaults capture grids centred on the page even when the anchor
    phrase appears on the left margin; tighter values (e.g. 280) help on
    LATE-era forms where 'LOCATE WELL' sits directly below the PLSS grid and
    wide crops would include unrelated data tables to the right.

    Returns (x0, y0, x1, y1) or None when the computed region is degenerate.
    """
    if anchor_bbox is None:
        return None
    x0, y0, x1, y1 = anchor_bbox

    cx0 = max(0,      x0 - side_padding)
    cx1 = min(page_w, x1 + side_padding)

    cy0 = max(0,      y0 - look_above)
    cy1 = min(page_h, y1 + look_below)

    if cy1 <= cy0 or cx1 <= cx0:
        return None
    return (int(cx0), int(cy0), int(cx1), int(cy1))


# Per-tier anchor crop params — tight crops for LATE/MODERN so that 'LOCATE WELL'
# (which always sits directly below the PLSS grid on C11-C12 COMPLETION REPORTs)
# doesn't result in a crop that spans the full page and includes data tables.
#
# Calibrated from visual inspection of C11 (2001-2018) forms at 2× resolution:
#   • PLSS grid is ≈ 150–250 px tall and sits ≈ 80–300 px above 'LOCATE WELL'.
#   • Using look_above=400 / look_below=80 / side_padding=300 captures the grid
#     while excluding the formations / casing tables that fill the right half.
#
# For EARLY / TRANSITION / MID forms the original generous defaults are kept —
# those forms have more layout variation and can tolerate extra crop area.
ANCHOR_CROP_BY_TIER: dict[str, dict] = {
    "early":      {"look_above": 700,  "look_below": 1100, "side_padding": 450},
    "transition": {"look_above": 700,  "look_below": 800,  "side_padding": 400},
    "mid":        {"look_above": 600,  "look_below": 600,  "side_padding": 380},
    # LATE (C11-C12): "LOCATE WELL" sits DIRECTLY below the PLSS grid with
    # approximately the same horizontal extent.  Calibrated from C11 2001
    # COMPLETION REPORTs at 2× resolution:
    #   • Grid is ≈ 315×292 px; top is ≈ 340 px above anchor y.
    #   • Grid is centered ≈ ±157 px around the anchor x-centre.
    #   • look_above=380 captures grid top with ≈40 px margin.
    #   • side_padding=200 keeps the crop within the PLSS grid's x-extent and
    #     excludes the formation/casing tables that appear further right.
    "late":       {"look_above": 380,  "look_below": 30,   "side_padding": 200},
    "modern":     {"look_above": 350,  "look_below": 50,   "side_padding": 250},
}

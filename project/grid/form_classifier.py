"""
Form-type classifier for Oklahoma well-record PDFs.

After the grid is detected, ``classify_form_type()`` analyses the grid's
position on the page, its aspect ratio, and the matched anchor phrase to
determine which of the known form layouts it belongs to.  The classification
drives two key decisions downstream:

  * **WHERE** to search for the Section / Township / Range block.
  * **WHAT** county format (name-before, colon-after, or stacked) to expect.

Form-type map
─────────────
┌──────────────┬─────────────────────────┬──────────────────────────────────────┐
│ Form type    │ Grid position / AR       │ Key collections                      │
├──────────────┼─────────────────────────┼──────────────────────────────────────┤
│ T1_LARGE     │ bottom-left, AR≈1.21+   │ Colls 1–2  (~1911–1925)             │
│ T2_MED       │ top-left,   AR≈1.21+    │ Colls 1–4  (~1925–1950) landscape   │
│ T3_SMALL     │ top-left,   AR≈0.58–0.63│ Colls 4–9  (~1950–1987) portrait    │
│ T4_NOANCHOR  │ top-left,   any AR       │ Occasional variant, no std anchor   │
│ MID          │ top-center, AR≈0.56      │ Colls 9–10 (~1983–2000)            │
│ LATE         │ top-center/right, AR≈0.70│ Colls 11–12 (~2001–2018)          │
│ UNKNOWN      │ —                        │ Does not match any known pattern    │
└──────────────┴─────────────────────────┴──────────────────────────────────────┘

STR location by form type
──────────────────────────
  T1_LARGE    → STR in the header area at the very top of the page
                (Section / Township / Range printed in the letterhead above the grid)
  T2_MED      → STR to the right of the grid or in the upper-right text block
  T3_SMALL    → STR in the upper-right quadrant; sometimes upper-center
  T4_NOANCHOR → STR upper-right; may use vertical label-over-value layout
  MID         → STR to the LEFT of the grid (labels + values on same horizontal row)
  LATE        → STR to the LEFT in a vertical stacked layout:
                  County  SEC   TWP   RGE
                  Garvin  15    23N   10W

County format by form type
──────────────────────────
  T1_LARGE / T2_MED  →  "<name> County"  (name precedes the word County)
  T3_SMALL / MID     →  either "<name> County" OR "County: <name>"
  LATE               →  stacked (label on one line, value directly below)
"""

# ---------------------------------------------------------------------------
# Form-type constants
# ---------------------------------------------------------------------------
FORM_T1_LARGE    = "T1_LARGE"     # early, bottom-left, landscape
FORM_T2_MED      = "T2_MED"       # early/transition, top-left, landscape
FORM_T3_SMALL    = "T3_SMALL"     # transition/mid, top-left, portrait
FORM_T4_NOANCHOR = "T4_NOANCHOR"  # top-left, no recognised anchor phrase
FORM_MID         = "MID"          # mid-era, top-center
FORM_LATE        = "LATE"         # late-era, top-center or top-right
FORM_UNKNOWN     = "UNKNOWN"

# ---------------------------------------------------------------------------
# Grid-zone labels  (normalised centre of detected bbox relative to page)
# ---------------------------------------------------------------------------
ZONE_BOTTOM_LEFT = "bottom_left"
ZONE_TOP_LEFT    = "top_left"
ZONE_TOP_CENTER  = "top_center"
ZONE_TOP_RIGHT   = "top_right"
ZONE_UNKNOWN     = "unknown"

# ---------------------------------------------------------------------------
# STR search-region hints
# Used by process_single_location() / process_single_location_keyword()
# to restrict or bias their token-search to the most likely page region.
# ---------------------------------------------------------------------------
STR_TOP_HEADER      = "top_header"       # top 40 % of page, full width
STR_RIGHT_OF_GRID   = "right_of_grid"    # same Y-band as grid, right 60 %
STR_LEFT_OF_GRID    = "left_of_grid"     # same Y-band as grid, left 40 %
STR_UPPER_RIGHT     = "upper_right"      # y < 40 %, x > 40 %
STR_VERTICAL_RIGHT  = "vertical_right"   # vertical label-over-value, right side
STR_VERTICAL_LEFT   = "vertical_left"    # vertical label-over-value, left side
STR_ANY             = "any"              # no constraint — full-page scan

# ---------------------------------------------------------------------------
# County format hints
# ---------------------------------------------------------------------------
COUNTY_NAME_THEN_WORD = "name_county"   # "Oklahoma County"
COUNTY_COLON_AFTER    = "county_colon"  # "County: Oklahoma"
COUNTY_STACKED        = "stacked"       # label on one line, name directly below
COUNTY_ANY            = "any"           # try all three strategies

# ---------------------------------------------------------------------------
# STR extraction strategy hints
# ---------------------------------------------------------------------------
STR_STRAT_HGROUP = "horizontal_group"  # current group-keywords / regex method
STR_STRAT_VSTACK = "vertical_stack"    # new vertical label-over-value method
STR_STRAT_ANY    = "any"               # try both, horizontal first


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _grid_zone(gx: int, gy: int, gw: int, gh: int,
               page_w: int, page_h: int) -> str:
    """
    Classify the grid's position by its normalised centre (0–1 range).

    Thresholds calibrated from 700-sample manual inspection (Jun 2025):
      bottom-left  cy > 0.50 and cx < 0.45
      top-left     cy ≤ 0.50 and cx < 0.30
      top-center   cy ≤ 0.50 and 0.30 ≤ cx ≤ 0.58
      top-right    cy ≤ 0.50 and cx > 0.58
    """
    if page_w <= 0 or page_h <= 0 or gw <= 0 or gh <= 0:
        return ZONE_UNKNOWN
    cx = (gx + gw / 2.0) / page_w
    cy = (gy + gh / 2.0) / page_h

    if cy > 0.50:
        return ZONE_BOTTOM_LEFT if cx < 0.45 else ZONE_UNKNOWN
    # Upper half of page
    if cx < 0.30:
        return ZONE_TOP_LEFT
    if cx > 0.58:
        return ZONE_TOP_RIGHT
    return ZONE_TOP_CENTER


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_form_type(
    grid_bbox,
    page_w: int,
    page_h: int,
    anchor_phrase: str | None = None,
    grid_ar: float | None = None,
    tier: str | None = None,
) -> dict:
    """
    Classify the PDF form type from the detected grid's geometry + anchor.

    Parameters
    ----------
    grid_bbox     : (x, y, w, h) in full-page pixels as returned by
                    ``process_single_grid()``.  Pass None when the grid
                    stage failed — returns FORM_UNKNOWN / STR_ANY hints.
    page_w / page_h : page dimensions at the rendered resolution
                      (same pixel units as grid_bbox).
    anchor_phrase : lowercase matched anchor phrase returned by
                    ``find_grid_anchor()``, e.g. ``"locate well correctly"``.
                    Pass None or '' when no anchor was found.
    grid_ar       : width / height aspect ratio of the detected grid.
                    Computed from grid_bbox when omitted.

    Returns
    -------
    dict with keys:
        form_type          – one of FORM_T1_LARGE … FORM_UNKNOWN
        grid_zone          – one of ZONE_BOTTOM_LEFT … ZONE_UNKNOWN
        str_zone           – STR_* hint for the location extractor
        county_format_hint – COUNTY_* hint for the county extractor
        str_strategy_hint  – STR_STRAT_* hint for the location extractor
    """
    _unknown: dict = {
        "form_type":          FORM_UNKNOWN,
        "grid_zone":          ZONE_UNKNOWN,
        "str_zone":           STR_ANY,
        "county_format_hint": COUNTY_ANY,
        "str_strategy_hint":  STR_STRAT_ANY,
    }

    if grid_bbox is None:
        return _unknown

    try:
        gx = int(grid_bbox[0])
        gy = int(grid_bbox[1])
        gw = int(grid_bbox[2])
        gh = int(grid_bbox[3])
    except (TypeError, IndexError, ValueError):
        return _unknown

    zone       = _grid_zone(gx, gy, gw, gh, page_w, page_h)
    anchor_low = (anchor_phrase or "").lower().strip()
    ar         = grid_ar if grid_ar is not None else (gw / gh if gh > 0 else 1.0)

    # ── Bottom-left zone ──────────────────────────────────────────────────
    # Classic T1 large landscape forms (Collections 1–3, ~1911–1940).
    # The STR / Section-Township-Range block appears in the HEADER area at
    # the very top of the page (printed letterhead), above the dot-grid.
    # County is almost always written as "<name> County" (name precedes word).
    if zone == ZONE_BOTTOM_LEFT:
        return {
            "form_type":          FORM_T1_LARGE,
            "grid_zone":          zone,
            "str_zone":           STR_TOP_HEADER,
            "county_format_hint": COUNTY_NAME_THEN_WORD,
            "str_strategy_hint":  STR_STRAT_HGROUP,
        }

    # ── Top-left zone ─────────────────────────────────────────────────────
    if zone == ZONE_TOP_LEFT:
        if "spot well" in anchor_low:
            # "Spot Well Correctly/Located" variant in top-left position
            # (clipped or reformatted version of the older T1 layout).
            return {
                "form_type":          FORM_T2_MED,
                "grid_zone":          zone,
                "str_zone":           STR_RIGHT_OF_GRID,
                "county_format_hint": COUNTY_NAME_THEN_WORD,
                "str_strategy_hint":  STR_STRAT_HGROUP,
            }

        if "locate well correctly" in anchor_low:
            # T2 medium landscape grid (Collections 1–4, ~1925–1950).
            # STR to the right of the grid or in the upper-right text block.
            return {
                "form_type":          FORM_T2_MED,
                "grid_zone":          zone,
                "str_zone":           STR_RIGHT_OF_GRID,
                "county_format_hint": COUNTY_NAME_THEN_WORD,
                "str_strategy_hint":  STR_STRAT_HGROUP,
            }

        if "locate well" in anchor_low:
            # Two sub-cases distinguish by aspect ratio:
            #   AR < 0.75  → portrait grid  → T3 small (Collections 4–9)
            #   AR ≥ 0.75  → landscape grid → T2 medium (Collections 1–4)
            if ar < 0.75:
                return {
                    "form_type":          FORM_T3_SMALL,
                    "grid_zone":          zone,
                    "str_zone":           STR_UPPER_RIGHT,
                    "county_format_hint": COUNTY_ANY,
                    "str_strategy_hint":  STR_STRAT_HGROUP,
                }
            return {
                "form_type":          FORM_T2_MED,
                "grid_zone":          zone,
                "str_zone":           STR_RIGHT_OF_GRID,
                "county_format_hint": COUNTY_NAME_THEN_WORD,
                "str_strategy_hint":  STR_STRAT_HGROUP,
            }

        # No recognised anchor phrase: T4 no-anchor variant (rare).
        # Spec shows this form uses a VERTICAL label-over-value layout at
        # upper-right:  SEC / TWP / RGE printed as separate label lines with
        # values directly below each.  Use VSTACK (not HGROUP) so the vertical
        # extractor runs first rather than as a last-resort fallback.
        return {
            "form_type":          FORM_T4_NOANCHOR,
            "grid_zone":          zone,
            "str_zone":           STR_UPPER_RIGHT,
            "county_format_hint": COUNTY_ANY,
            "str_strategy_hint":  STR_STRAT_VSTACK,
        }

    # ── Top-center zone ───────────────────────────────────────────────────
    # Mid-era forms (Collections 9–10, ~1983–2000).
    # STR label block sits to the LEFT of the grid on the same horizontal
    # band.  County can be in any of the three formats.
    #
    # GUARD: MID forms do not exist before ~1983.  On early/transition tier
    # records a top-center "grid" is almost always a casing/water-sands TABLE
    # false positive (449 such records in the Jun 2026 C2-C5 sample had 1%
    # location success because the MID hints pointed every stage at the wrong
    # page regions).  Return open ANY hints so the location/county extractors
    # search the whole page instead of trusting a wrong zone.
    if zone == ZONE_TOP_CENTER:
        if tier in ("early", "transition"):
            return {
                "form_type":          FORM_UNKNOWN,
                "grid_zone":          zone,
                "str_zone":           STR_ANY,
                "county_format_hint": COUNTY_ANY,
                "str_strategy_hint":  STR_STRAT_ANY,
            }
        return {
            "form_type":          FORM_MID,
            "grid_zone":          zone,
            "str_zone":           STR_LEFT_OF_GRID,
            "county_format_hint": COUNTY_ANY,
            "str_strategy_hint":  STR_STRAT_HGROUP,
        }

    # ── Top-right zone ────────────────────────────────────────────────────
    # Late-era forms (Collections 11–12, ~2001–2018).
    # The County + SEC/TWP/RGE block appears to the LEFT of the grid in a
    # vertical stacked layout: label on one line, value directly below it.
    # Example:
    #   County    SEC    TWP    RGE
    #   Garvin    15     23N    10W
    if zone == ZONE_TOP_RIGHT:
        # Same guard as top-center: LATE forms start ~2001, so on early/
        # transition tiers this is a false-positive zone — use open hints.
        if tier in ("early", "transition"):
            return {
                "form_type":          FORM_UNKNOWN,
                "grid_zone":          zone,
                "str_zone":           STR_ANY,
                "county_format_hint": COUNTY_ANY,
                "str_strategy_hint":  STR_STRAT_ANY,
            }
        return {
            "form_type":          FORM_LATE,
            "grid_zone":          zone,
            "str_zone":           STR_LEFT_OF_GRID,
            "county_format_hint": COUNTY_STACKED,
            "str_strategy_hint":  STR_STRAT_VSTACK,
        }

    return _unknown

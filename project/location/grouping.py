"""
Keyword-based SEC/TWP/RGE grouping for location extraction.

find_keywords_lists()
    Scan OCR token annotations for section / township / range label tokens.
    Returns three bbox lists, one per field type.

choose_group()
    From those three lists, pick the best aligned triplet (horizontal layout
    assumption: SEC to the left of TWP to the left of RGE on the same line).

get_unified_bounding_box()
    Merge the triplet into one crop-box that encompasses all three labels plus
    a right extension large enough to capture the printed value.

Token-matching strategy
───────────────────────
Each OCR annotation is a *single token*.  The matching test uses **exact
comparison** after stripping trailing punctuation (period, comma, colon,
semicolon) from both the token and the keyword variant.  This prevents
substring false-positives such as:

  "secondary"  matching keyword "sec"
  "arrangement" matching keyword "range"
  "township15"  matching keyword "twp" (combined OCR artefact)

Uppercase is normalised to lower-case before comparison.

The keyword lists in config.LOCATION_KEYWORDS must therefore be exhaustive:
every observed abbreviation variant is listed explicitly there.  Bare single-
letter variants "T" and "R" (Variant-D dash-separated forms on early records)
are NOT in the keyword list because a single-character exact match would
produce massive false-positives; they are handled instead by the _TWP_RE /
_RNG_RE regex in location/location_extractor.py.
"""

import re as _re

# Strip trailing punctuation for token normalisation.
_PUNCT_RE = _re.compile(r"[.\s,;:\-]+$")


def _normalise_token(tok: str) -> str:
    """Lower-case + strip trailing punctuation."""
    return _PUNCT_RE.sub("", tok.lower())


def find_keywords_lists(text_annotations, keywords,
                        extend_right=400, padding_height=50):
    """
    Scan OCR token annotations (skipping the first full-page blob at index 0)
    and collect bounding boxes for each keyword match.

    Returns three lists: ``(sections, townships, ranges)``.

    Each box is ``[x_min, y_min, x_max + extend_right, y_max]`` with
    ``y_min / y_max`` padded by ``padding_height`` so that a value token
    sitting one line above or below the keyword still overlaps the box.

    Matching is **exact** (after punctuation-stripping + lower-case) to avoid
    the substring false-positives documented in the module header.
    """
    # Pre-normalise all keyword variants once.
    sec_variants  = [_normalise_token(v) for v in keywords["section"]]
    twp_variants  = [_normalise_token(v) for v in keywords["township"]]
    rng_variants  = [_normalise_token(v) for v in keywords["range"]]

    sections  = []
    townships = []
    ranges    = []

    for annotation in text_annotations[1:]:
        raw_text = annotation.description or ""
        tok = _normalise_token(raw_text)
        if not tok:
            continue

        poly = annotation.bounding_poly
        if not poly or not poly.vertices:
            continue

        try:
            x_min = min(v.x for v in poly.vertices)
            y_min = min(v.y for v in poly.vertices) - padding_height
            x_max = max(v.x for v in poly.vertices)
            y_max = max(v.y for v in poly.vertices) + padding_height
        except Exception:
            continue

        bbox = [x_min, y_min, x_max + extend_right, y_max]

        if tok in sec_variants:
            sections.append(bbox)
        elif tok in twp_variants:
            townships.append(bbox)
        elif tok in rng_variants:
            ranges.append(bbox)

    return sections, townships, ranges


def vertical_overlap(y1_min, y1_max, y2_min, y2_max):
    """
    Fractional vertical overlap between two y-ranges, normalised by the
    smaller height.  Returns 0..1 (0 if disjoint, 1 if one contains the other).
    """
    overlap = max(0, min(y1_max, y2_max) - max(y1_min, y2_min))
    height1 = y1_max - y1_min
    height2 = y2_max - y2_min
    return overlap / min(height1, height2) if min(height1, height2) > 0 else 0


def choose_group(sections, townships, ranges, min_overlap=0.5):
    """
    Attempt to choose a horizontal triplet where:
      • township starts to the RIGHT of section  (t_x_min > s_x_max)
      • range starts to the RIGHT of section
      • all three boxes share sufficient vertical overlap (≥ min_overlap)

    If no section-anchored triplet is found but township AND range boxes exist,
    return a fallback unified box so at least TWP + RGE can anchor the crop.

    Returns a dict or None.
    """
    # NOTE: keyword boxes arrive PRE-EXTENDED (+extend_right, typically 500px,
    # on x_max) from find_keywords_lists().  Ordering must therefore compare
    # x_min values: requiring t_x_min > s_x_max meant "township starts more
    # than 500px right of section", which never fires on compact same-line
    # headers (1940s forms space SEC/TWP/RGE ~150-220px apart) and silently
    # disabled the triplet path for those layouts.
    for s in sections:
        s_x_min, s_y_min, s_x_max, s_y_max = s
        candidate_t = None
        candidate_r = None

        for t in townships:
            t_x_min, t_y_min, t_x_max, t_y_max = t
            if (
                t_x_min > s_x_min
                and vertical_overlap(s_y_min, s_y_max,
                                     t_y_min, t_y_max) >= min_overlap
            ):
                candidate_t = t
                break

        for r in ranges:
            r_x_min, r_y_min, r_x_max, r_y_max = r
            if (
                r_x_min > s_x_min
                and vertical_overlap(s_y_min, s_y_max,
                                     r_y_min, r_y_max) >= min_overlap
            ):
                candidate_r = r
                break

        if candidate_t and candidate_r:
            return {
                "section":  s,
                "township": candidate_t,
                "range":    candidate_r,
            }

    # Fallback: if no section found but township + range exist,
    # return a unified box so the crop can still be extracted.
    if townships and ranges:
        union_boxes = townships + ranges
        union_x_min = min(box[0] for box in union_boxes)
        union_y_min = min(box[1] for box in union_boxes)
        union_x_max = max(box[2] for box in union_boxes)
        union_y_max = max(box[3] for box in union_boxes)

        return {
            "section":  None,
            "township": None,
            "range":    None,
            "unified": [
                union_x_min - 300,
                union_y_min - 80,
                union_x_max + 180,
                union_y_max + 80,
            ],
        }

    # Fallback: section + township but NO range keyword.  At 2x render the
    # small printed "RGE" label is frequently below Vision OCR's resolution
    # floor (verified on 1940s C3 headers: "RGE" vanishes at 2x, reads fine
    # at 2.5x) — this was the single biggest source of missing-range partials.
    # Build a unified box over sec+twp keywords extended right, so the range
    # VALUE ("17 E") is still inside the crop even though its label is gone.
    if sections and townships:
        for s in sections:
            for t in townships:
                if (t[0] > s[0]
                        and vertical_overlap(s[1], s[3], t[1], t[3]) >= min_overlap):
                    return {
                        "section":  None,
                        "township": None,
                        "range":    None,
                        "unified": [
                            s[0] - 100,
                            min(s[1], t[1]) - 80,
                            t[2] + 500,
                            max(s[3], t[3]) + 80,
                        ],
                    }

    return None


def get_unified_bounding_box(group, section_right_extension=200):
    """
    Return a unified bounding box from a group dictionary.

    Spans ALL three keyword boxes (section + township + range) so that
    the crop and the Gemini image always cover every field-value zone, not
    just the section area.  Each keyword bbox already has ``extend_right``
    (typically 500 px) added on the right by ``find_keywords_lists()``, so
    taking the rightmost box's x_max and adding ``section_right_extension``
    (200 px more) gives enough room past the Range value without over-cropping.

    Falls back to the pre-computed ``unified`` box when the group was built
    without a section anchor (township + range only).
    """
    if group is None:
        return None

    # Fallback group has no individual keyword boxes — use its precomputed box.
    if "unified" in group:
        return group["unified"]

    # Normal group: span section + township + range to capture all field values.
    boxes = [group[k] for k in ("section", "township", "range")
             if group.get(k) is not None]
    if not boxes:
        return None

    x_min = min(b[0] for b in boxes)
    y_min = min(b[1] for b in boxes)
    # b[2] already includes extend_right from find_keywords_lists (500 px by default);
    # add section_right_extension (200 px) past the rightmost keyword's value zone.
    x_max = max(b[2] for b in boxes) + section_right_extension
    y_max = max(b[3] for b in boxes)
    return [x_min, y_min, x_max, y_max]

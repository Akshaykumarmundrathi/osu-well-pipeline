"""
Form 1002A / Oklahoma Corporation Commission Location-block parser.

Parses the structured header that appears on the first page of modern
Oklahoma well records (post ~1995):

    Location:
    ALFALFA 36 28N 11W
    C SW SW
    660 FSL 660 FWL of 1/4 SEC
    Latitude: 36.8569153 Longitude: -98.339600497

Also handles the shorter "Locate Well" variant seen on older forms:
    LOCATE WELL
    SEC 14 T5N R9W
    NW NE NW
    330 FNL 330 FEL of 1/4 SEC

Public API
----------
parse_location_block(full_page_text) -> dict
    {
      "found"           : bool,
      "form_type"       : "form_1002a" | "locate_well" | "unknown",
      "county"          : str,         # e.g. "ALFALFA"
      "section"         : str,         # e.g. "36"
      "township"        : str,         # e.g. "28N"
      "range"           : str,         # e.g. "11W"
      "quadrant_raw"    : str,         # e.g. "C SW SW" or "NW NE NW"
      "quadrant_type"   : str,         # "center" | "three_level" | "two_level" | "one_level" | ""
      "quadrant_db"     : str,         # DB coarse→fine label if fully decoded, else ""
      "quadrant_row"    : int | None,  # 1-8 grid row if decoded
      "quadrant_col"    : int | None,  # 1-8 grid col if decoded
      "feet_fsl"        : int | None,  # feet from south line
      "feet_fnl"        : int | None,  # feet from north line
      "feet_fwl"        : int | None,  # feet from west line
      "feet_fel"        : int | None,  # feet from east line
      "feet_ref"        : str,         # "SEC" | "1/4 SEC" | "1/16 SEC" | ""
      "rel_x_from_feet" : float | None, # 0-1 within section (E-W), from feet
      "rel_y_from_feet" : float | None, # 0-1 within section (N-S top), from feet
    }

parse_quadrant_notation(text) -> dict
    Parses "C SW SW", "NW NE NW", "SW" etc.
    Returns the same quadrant-specific sub-dict.
"""

import re

# ---------------------------------------------------------------------------
# Direction constants
# ---------------------------------------------------------------------------
_DIR4 = {"NW", "NE", "SW", "SE"}
_FEET_LABELS = {
    "FSL": "fsl", "FNL": "fnl", "FWL": "fwl", "FEL": "fel",
    # OCR variants
    "F.S.L": "fsl", "F.N.L": "fnl", "F.W.L": "fwl", "F.E.L": "fel",
    "FSOL": "fsl", "FNOL": "fnl",
}

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# "Location:" or "Locate Well" header trigger
_RE_LOCATION_TRIGGER = re.compile(
    r"(?:Location\s*[:]\s*|Locate\s+Well\b)",
    re.I,
)

# County + section + township + range on a single line (OCR may squash spaces)
# Examples: "ALFALFA 36 28N 11W"  "COMANCHE  14  5N  9W"  "ROGER MILLS 22 12N 26W"
_RE_STR_LINE = re.compile(
    r"^(?P<county>[A-Z][A-Z\s]{2,24}?)\s+"
    r"(?P<section>\d{1,2})\s+"
    r"(?P<township>\d{1,2}\s*[NS])\s+"
    r"(?P<range>\d{1,2}\s*[EW])\s*$",
    re.I | re.M,
)

# Also handle "SEC 14 T5N R9W" form
_RE_STR_LINE2 = re.compile(
    r"SEC\s*(?P<section>\d{1,2})\s+"
    r"T\s*(?P<township>\d{1,2}\s*[NS])\s+"
    r"R\s*(?P<range>\d{1,2}\s*[EW])",
    re.I,
)

# Quadrant line: "C SW SW" | "NW NE NW" | "SW SW" | "NE"
# "C" = center indicator
_RE_QUAD_LINE = re.compile(
    r"^(?P<center>C\b\s*)?(?P<d1>[NS][EW])\s+(?P<d2>[NS][EW])"
    r"(?:\s+(?P<d3>[NS][EW]))?",
    re.I | re.M,
)

# Standalone one-level: just a single direction as its own token
_RE_QUAD_ONE = re.compile(r"^(?P<d1>[NS][EW])\s*$", re.I | re.M)

# Feet from line: "660 FSL" "660 FWL" "330 FNL" etc.
_RE_FEET = re.compile(
    r"(\d{1,5})\s+"
    r"(F[SNEW]L|F\.[SNEW]\.L|FSOL|FNOL)",
    re.I,
)

# Reference subdivision: "of SEC" | "of 1/4 SEC" | "of 1/16 SEC"
_RE_FEET_REF = re.compile(
    r"of\s+(1/16|1/4|)\s*SEC",
    re.I,
)

# ---------------------------------------------------------------------------
# Quadrant decoder (handles "C SW SW" and the usual 3-level, 2-level forms)
# ---------------------------------------------------------------------------

def parse_quadrant_notation(text: str) -> dict:
    """
    Parse a quadrant description string into structured data.

    Handles:
      "C SW SW"      → center of SW-SW (two-level with center)
      "NW NE NW"     → three-level, maps to DB label (PDF fine→coarse reversed)
      "C NE"         → center of NE quarter (one-level with center)
      "SW SW"        → two-level (less precise; 4 cells)
      "NW"           → one-level (16 cells)
    """
    out = {
        "quadrant_raw":    text.strip(),
        "quadrant_type":   "",
        "quadrant_db":     "",
        "quadrant_row":    None,
        "quadrant_col":    None,
        "is_center":       False,
    }
    if not text:
        return out

    s = text.strip().upper()
    is_center = s.startswith("C ")
    if is_center:
        s = s[2:].strip()
        out["is_center"] = True

    parts = s.split()
    dirs  = [p for p in parts if p in _DIR4]

    if len(dirs) == 3:
        # Three-level: PDF order is fine→coarse → reverse for DB
        from ocr.quadrant_extractor import label_to_row_col
        db_label = "-".join(reversed(dirs))
        row, col = label_to_row_col(db_label)
        out.update(
            quadrant_type="center_three_level" if is_center else "three_level",
            quadrant_db=db_label,
            quadrant_row=row,
            quadrant_col=col,
        )
    elif len(dirs) == 2:
        # Two-level (2×2 cells): compute center row/col of the 4-cell block
        d1, d2 = dirs[0], dirs[1]      # fine → coarse
        # Center of the 4-cell block:
        # coarse direction = d2, fine = d1
        # Pick the center quadrant_label candidates
        from ocr.quadrant_extractor import label_to_row_col, row_col_to_label
        # 4 possible DB labels: d2-d1-{NW,NE,SW,SE}
        cells = []
        for d3 in ("NW", "NE", "SW", "SE"):
            db_label = f"{d2}-{d1}-{d3}"
            r, c = label_to_row_col(db_label)
            if r is not None:
                cells.append((r, c))
        if cells:
            avg_r = sum(r for r, _ in cells) / len(cells)
            avg_c = sum(c for _, c in cells) / len(cells)
            # Pick cell closest to 2×2 block center
            best = min(cells, key=lambda rc: abs(rc[0]-avg_r) + abs(rc[1]-avg_c))
            out.update(
                quadrant_type="center_two_level" if is_center else "two_level",
                quadrant_db=f"{d2}-{d1}",      # 2-part, non-queryable
                quadrant_row=best[0],
                quadrant_col=best[1],
            )
    elif len(dirs) == 1:
        out.update(
            quadrant_type="center_one_level" if is_center else "one_level",
            quadrant_db=dirs[0],
        )

    return out


# ---------------------------------------------------------------------------
# Feet → section-normalised (rel_x, rel_y)
# ---------------------------------------------------------------------------

# PLSS section ≈ 5280 feet per side; 1/4 section ≈ 2640 ft
_SECTION_FT  = 5280.0
_QTR_FT      = 2640.0
_SIXTEENTH_FT = 1320.0

def feet_to_rel(feet_fsl, feet_fnl, feet_fwl, feet_fel, feet_ref) -> tuple:
    """
    Convert feet-from-line measurements to section-normalised (rel_x, rel_y).

    rel_x: 0 = west edge, 1 = east edge of section
    rel_y: 0 = north edge, 1 = south edge  (matches our bilinear convention)

    Returns (rel_x, rel_y) or (None, None) if insufficient data.
    """
    ref_ft = _SECTION_FT
    if feet_ref:
        r = feet_ref.lower()
        if "1/16" in r:
            ref_ft = _SIXTEENTH_FT
        elif "1/4" in r:
            ref_ft = _QTR_FT

    rel_x = rel_y = None

    # East-West (rel_x)
    if feet_fwl is not None:
        rel_x = feet_fwl / ref_ft
    elif feet_fel is not None:
        rel_x = 1.0 - feet_fel / ref_ft

    # North-South (rel_y: 0=north, 1=south)
    if feet_fsl is not None:
        rel_y = 1.0 - feet_fsl / ref_ft    # FSL counts up from south
    elif feet_fnl is not None:
        rel_y = feet_fnl / ref_ft           # FNL counts down from north

    # Clamp to [0, 1]
    if rel_x is not None:
        rel_x = max(0.0, min(1.0, rel_x))
    if rel_y is not None:
        rel_y = max(0.0, min(1.0, rel_y))

    return rel_x, rel_y


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_location_block(full_page_text: str) -> dict:
    """
    Scan `full_page_text` for the Location: block and extract all fields.
    Returns a structured dict (always — check `found` key).
    """
    empty = {
        "found": False, "form_type": "unknown",
        "county": "", "section": "", "township": "", "range": "",
        "quadrant_raw": "", "quadrant_type": "",
        "quadrant_db": "", "quadrant_row": None, "quadrant_col": None,
        "feet_fsl": None, "feet_fnl": None,
        "feet_fwl": None, "feet_fel": None,
        "feet_ref": "",
        "rel_x_from_feet": None, "rel_y_from_feet": None,
    }

    if not full_page_text:
        return empty

    # Detect trigger
    trig = _RE_LOCATION_TRIGGER.search(full_page_text)
    if not trig:
        return empty

    form_type = ("locate_well"
                 if "locate" in trig.group(0).lower()
                 else "form_1002a")

    # Work with the text from the trigger onwards (max 800 chars)
    snippet = full_page_text[trig.start(): trig.start() + 800]

    # -- County + STR line ------------------------------------------------
    county = section = township = rng = ""

    m = _RE_STR_LINE.search(snippet)
    if m:
        county   = m.group("county").strip().title()
        section  = m.group("section").strip()
        township = re.sub(r"\s+", "", m.group("township")).upper()
        rng      = re.sub(r"\s+", "", m.group("range")).upper()
    else:
        m2 = _RE_STR_LINE2.search(snippet)
        if m2:
            section  = m2.group("section").strip()
            township = re.sub(r"\s+", "", m2.group("township")).upper()
            rng      = re.sub(r"\s+", "", m2.group("range")).upper()

    # -- Quadrant line ----------------------------------------------------
    quad_raw = ""
    qm = _RE_QUAD_LINE.search(snippet)
    if qm:
        parts = filter(None, [
            "C " if qm.group("center") else "",
            qm.group("d1"),
            " " + qm.group("d2"),
            (" " + qm.group("d3")) if qm.group("d3") else "",
        ])
        quad_raw = "".join(parts).strip().upper()
    else:
        qm1 = _RE_QUAD_ONE.search(snippet)
        if qm1:
            quad_raw = qm1.group("d1").upper()

    quad_info = parse_quadrant_notation(quad_raw)

    # -- Feet from line ---------------------------------------------------
    feet: dict = {}
    for fm in _RE_FEET.finditer(snippet):
        n     = int(fm.group(1))
        label = fm.group(2).upper().replace(".", "").replace("O", "0")
        norm  = _FEET_LABELS.get(label, label.lower())
        feet[norm] = n

    ref_m   = _RE_FEET_REF.search(snippet)
    feet_ref = ref_m.group(1).strip() + " SEC" if ref_m else ""

    fsl = feet.get("fsl")
    fnl = feet.get("fnl")
    fwl = feet.get("fwl")
    fel = feet.get("fel")

    rel_x, rel_y = feet_to_rel(fsl, fnl, fwl, fel, feet_ref)

    return {
        "found":     bool(county or section),
        "form_type": form_type,
        "county":    county,
        "section":   section,
        "township":  township,
        "range":     rng,
        "quadrant_raw":  quad_raw,
        "quadrant_type": quad_info["quadrant_type"],
        "quadrant_db":   quad_info["quadrant_db"],
        "quadrant_row":  quad_info["quadrant_row"],
        "quadrant_col":  quad_info["quadrant_col"],
        "feet_fsl":  fsl, "feet_fnl": fnl,
        "feet_fwl":  fwl, "feet_fel": fel,
        "feet_ref":  feet_ref,
        "rel_x_from_feet": rel_x,
        "rel_y_from_feet": rel_y,
    }

"""
field_normalizer.py  — canonical field normalisation for the OSU pipeline
=========================================================================

All public functions return a clean string (never None) in the canonical
format expected by plss_grid (RDS) and dot_coordinates.csv.

  section          plain integer string, e.g. "5"  (no leading zeros, no .0)
  township_str     "25N" | "25S" | "25"  (no space, uppercase direction)
  range_str        "14W" | "13E" | "14"  (no space, uppercase direction)
  north_south      "N" | "S" | ""
  east_west        "E" | "W" | ""
  county_name      "Garfield County" (Title Case + County suffix, or "")
  quadrant_label   "NE-NE-SW" (uppercase, hyphens, exactly 3 levels)

Usage
-----
  from coord.field_normalizer import (
      norm_section, norm_township, norm_range,
      norm_north_south, norm_east_west,
      norm_county, norm_quadrant,
  )

Design notes
------------
* Every function accepts None / empty / whitespace-only and returns "".
* No external dependencies — pure stdlib re + string ops.
* Functions are idempotent: normalising an already-normalised value is a no-op.
"""

from __future__ import annotations
import re

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_JUNK_COUNTY = re.compile(
    r"""
    ^ (
        not\s+detected\.? |   # "Not detected." / "Not detected"
        none                |   # "None"
        n/?a                |   # "N/A", "NA"
        \.+                 |   # "." / ".."
        unknown             |
        -+                      # "---"
    ) $
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Letters only in a quadrant part (NE, NW, SE, SW)
_QUAD_PART = re.compile(r"^(NE|NW|SE|SW)$")


# ---------------------------------------------------------------------------
# Section
# ---------------------------------------------------------------------------

def norm_section(v) -> str:
    """
    Normalise a section number to a plain integer string.

    "5"  / "05" / "5.0" / " 5 " -> "5"
    Anything unparseable         -> ""
    """
    s = str(v or "").strip()
    if not s:
        return ""
    # Strip T/R/S prefixes that sometimes appear
    s = re.sub(r"^[TRS]\s*", "", s, flags=re.IGNORECASE)
    try:
        n = int(float(s))
        if 1 <= n <= 36:
            return str(n)
        return ""           # out of valid PLSS range
    except (ValueError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# Township / Range combined string  (e.g. "25N", "14W")
# ---------------------------------------------------------------------------

def norm_township(v) -> str:
    """
    Normalise a township string to canonical "25N" / "25S" / "25" form.

    Handles:
      "25"   "25N"  "25 N"  "25n"   -> "25N"
      "T25N" "25.0" "T25"           -> "25N" / "25"
      "25S"  "25 S" "25s"           -> "25S"
      empty / None / junk           -> ""
    """
    return _norm_trs(v, "NS")


def norm_range(v) -> str:
    """
    Normalise a range string to canonical "14W" / "13E" / "14" form.

    Handles:
      "14"  "14W"  "14 W"  "14w"   -> "14W"
      "R14W" "14.0"                 -> "14W" / "14"
      "14E"  "14 E"                 -> "14E"
      empty / None / junk           -> ""
    """
    return _norm_trs(v, "EW")


def _norm_trs(v, dir_chars: str) -> str:
    """
    Core normaliser for Township / Range strings.

    1. Strip whitespace + common prefixes (T, R)
    2. Strip ".0" float suffix
    3. Extract number and optional direction letter
    4. Return "NUMdir" if both present, "NUM" if number-only, "" if nothing
    """
    s = str(v or "").strip()
    if not s:
        return ""
    # Remove "T" or "R" prefix (case-insensitive), allowing optional space
    s = re.sub(r"^[TR]\s*", "", s, flags=re.IGNORECASE)
    # Remove trailing ".0+" float artefacts
    s = re.sub(r"\.0+$", "", s)
    s = s.strip()
    # Collapse internal space between digit and direction: "25 N" -> "25N"
    s = re.sub(r"(\d)\s+([" + dir_chars + r"])$", r"\1\2", s, flags=re.IGNORECASE)
    if not s:
        return ""
    # Extract numeric part and optional direction
    num_m = re.search(r"\d+", s)
    dir_m = re.search(f"[{dir_chars}]", s, re.IGNORECASE)
    if not num_m:
        return ""
    num = num_m.group()
    direction = dir_m.group().upper() if dir_m else ""
    # Validate numeric range
    try:
        n = int(num)
        if n < 1 or n > 99:   # loose bound — PLSS max ~29N / 28W in OK
            return ""
    except ValueError:
        return ""
    return f"{num}{direction}" if direction else num


# ---------------------------------------------------------------------------
# North/South and East/West individual chars
# ---------------------------------------------------------------------------

def norm_north_south(v) -> str:
    """Extract "N" or "S" from any format, or "" if not determinable."""
    s = str(v or "").strip().upper()
    if s in ("N", "S"):
        return s
    # Try to extract from combined township string
    m = re.search(r"[NS]", s)
    return m.group() if m else ""


def norm_east_west(v) -> str:
    """Extract "E" or "W" from any format, or "" if not determinable."""
    s = str(v or "").strip().upper()
    if s in ("E", "W"):
        return s
    m = re.search(r"[EW]", s)
    return m.group() if m else ""


# ---------------------------------------------------------------------------
# County name
# ---------------------------------------------------------------------------

def norm_county(v, county_map: dict | None = None) -> str:
    """
    Normalise a county name to "Title Case County" format.

    Rules
    -----
    * Empty / None / junk phrases ("Not detected.", "None", "N/A") -> ""
    * Strips leading/trailing whitespace and trailing dots/commas
    * Applies title-case: "garfield county" -> "Garfield County"
    * Ensures " County" suffix (adds it if the name matches a known county
      base when county_map is provided)
    * RDS value "Le Flore County" is preserved (not forced to "Le Flore County")
      because Python's str.title() handles it correctly

    county_map : optional dict mapping clean base names to canonical full names
                 (e.g. {"garfield": "Garfield County"}).  When provided, an
                 exact base-name match overrides the title-case heuristic.
    """
    s = str(v or "").strip()
    if not s:
        return ""
    # Strip trailing punctuation
    s = s.rstrip(".,;")
    if not s:
        return ""
    # Reject known junk patterns
    if _JUNK_COUNTY.match(s):
        return ""
    # Lowercase clean key for map lookup
    clean_key = re.sub(r"\s+county\.?$", "", s, flags=re.IGNORECASE).strip().lower()
    if county_map and clean_key in county_map:
        return county_map[clean_key]   # use canonical name from map
    # Title-case the whole value
    result = s.title()
    # Ensure "County" suffix is capitalised correctly (title() handles this)
    # But fix known edge case: "Le Flore" -> already OK
    return result


# ---------------------------------------------------------------------------
# Quadrant label
# ---------------------------------------------------------------------------

def norm_quadrant(v) -> str:
    """
    Normalise a quadrant label to canonical "NE-NE-SW" form.

    Handles:
      "NE-NE-SW"  "ne-ne-sw"  "NE NE SW"  "NE_NE_SW"  -> "NE-NE-SW"
      Any 3-token sequence of NE/NW/SE/SW                -> joined with "-"
      Anything else / invalid                            -> ""
    """
    s = str(v or "").strip().upper()
    if not s:
        return ""
    # Replace spaces, underscores, slashes with hyphens
    s = re.sub(r"[\s_/]+", "-", s)
    # Strip any chars that aren't uppercase letters or hyphens
    s = re.sub(r"[^A-Z-]", "", s)
    parts = [p for p in s.split("-") if p]
    if len(parts) != 3:
        return ""
    if not all(_QUAD_PART.match(p) for p in parts):
        return ""
    return "-".join(parts)


# ---------------------------------------------------------------------------
# Convenience: normalise a full processing-status / coord row dict in-place
# ---------------------------------------------------------------------------

def norm_row(row: dict, county_map: dict | None = None) -> dict:
    """
    Apply canonical normalisation to all PLSS-related fields in a row dict.

    Fields touched
    --------------
    location_section  -> norm_section()
    location_township -> norm_township()
    location_range    -> norm_range()
    section           -> norm_section()          (coord output field)
    township          -> norm_township()         (coord output field)
    range             -> norm_range()            (coord output field)
    county_name       -> norm_county()
    dot_nw            -> norm_quadrant()
    effective_quad    -> norm_quadrant()
    ocr_quadrant_db   -> norm_quadrant()
    unet_nw           -> norm_quadrant()

    Returns the same dict (modified in-place) for chaining convenience.
    """
    cm = county_map

    # Processing-status fields
    if "location_section" in row:
        row["location_section"] = norm_section(row["location_section"])
    if "location_township" in row:
        row["location_township"] = norm_township(row["location_township"])
    if "location_range" in row:
        row["location_range"] = norm_range(row["location_range"])

    # Coord-enricher output fields
    if "section" in row:
        row["section"] = norm_section(row["section"])
    if "township" in row:
        row["township"] = norm_township(row["township"])
    if "range" in row:
        row["range"] = norm_range(row["range"])

    # County
    if "county_name" in row:
        row["county_name"] = norm_county(row["county_name"], cm)

    # Quadrant fields
    for q_field in ("dot_nw", "effective_quad", "ocr_quadrant_db",
                    "ocr_quadrant_pdf", "unet_nw"):
        if q_field in row and row[q_field]:
            row[q_field] = norm_quadrant(row[q_field])

    return row

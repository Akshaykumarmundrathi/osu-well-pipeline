"""
PLSS quadrant-label extractor.

Oklahoma oil-well PDFs often contain the dot's position as a 3-level PLSS
subdivision string, e.g.

    "NW of SW of NE"                 (English form, fine→coarse)
    "NW 1/4 of SW 1/4 of NE 1/4"    (legal description)
    "NW-SW-NE"                       (hyphenated)
    "NW¼SW¼NE¼"                      (Unicode fraction)

The PDF order is fine→coarse (most specific quadrant first).
The RDS plss_grid.quadrant_label order is coarse→fine.

So "NW-SW-NE" in a PDF maps to DB label "NE-SW-NW" (reversed).

Public API
----------
extract_quadrant(text) -> dict | None
    {
      "pdf_label"   : "NW-SW-NE",   # fine→coarse (as found in PDF)
      "db_label"    : "NE-SW-NW",   # coarse→fine (matches plss_grid.quadrant_label)
      "row"         : int,           # 1-8 (1 = northernmost)
      "col"         : int,           # 1-8 (1 = westernmost)
      "levels"      : 3,
      "confidence"  : float,         # 0-1
      "match_text"  : str,           # the raw matched substring
    }
    Returns None if no 3-level pattern found.

label_to_row_col(db_label) -> (row, col) | (None, None)
row_col_to_label(row, col) -> db_label | None
"""

import re

# ---------------------------------------------------------------------------
# Direction bit encoding: NW=(0,0) NE=(0,1) SW=(1,0) SE=(1,1)
# row_bit=0→north half, col_bit=0→west half
# ---------------------------------------------------------------------------
_DIR_BITS: dict[str, tuple[int, int]] = {
    "NW": (0, 0), "NE": (0, 1),
    "SW": (1, 0), "SE": (1, 1),
}
_BITS_DIR = {v: k for k, v in _DIR_BITS.items()}

# OCR noise map: common OCR errors for direction tokens
_OCR_FIX = {
    "N W": "NW", "N E": "NE", "S W": "SW", "S E": "SE",
    "N.W": "NW", "N.E": "NE", "S.W": "SW", "S.E": "SE",
    "N,W": "NW", "N,E": "NE", "S,W": "SW", "S,E": "SE",
    "5W":  "SW", "5E":  "SE",   # digit-5 mis-read as S
    "NWL": "NW", "NEL": "NE",   # trailing L artifact
}

# ---------------------------------------------------------------------------
# label ↔ (row, col) conversion
# ---------------------------------------------------------------------------

def label_to_row_col(db_label: str) -> tuple[int | None, int | None]:
    """
    'NE-SW-NW' (DB coarse→fine) → (row, col) 1-indexed.
    Returns (None, None) on invalid input.
    """
    parts = db_label.upper().strip().split("-")
    if len(parts) != 3:
        return None, None
    row = col = 0
    for i, p in enumerate(parts):
        bits = _DIR_BITS.get(p)
        if bits is None:
            return None, None
        stride = 4 >> i   # 4, 2, 1
        row += bits[0] * stride
        col += bits[1] * stride
    return row + 1, col + 1


def row_col_to_label(row: int, col: int) -> str | None:
    """
    (row, col) 1-indexed → DB label (coarse→fine) e.g. 'NW-SW-NE'.
    Returns None if row/col out of [1,8].
    """
    if not (1 <= row <= 8 and 1 <= col <= 8):
        return None
    r0, c0 = row - 1, col - 1
    parts = []
    for shift in (2, 1, 0):
        rb = (r0 >> shift) & 1
        cb = (c0 >> shift) & 1
        parts.append(_BITS_DIR[(rb, cb)])
    return "-".join(parts)


# ---------------------------------------------------------------------------
# OCR text normalisation helpers
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Replace Unicode fractions, fix common OCR artefacts, collapse whitespace."""
    s = text
    # Unicode quarter/fraction variants
    for ch in ("¼", "¾", "½", "¼", "¾", "½"):
        s = s.replace(ch, " 1/4 ")
    # Remove "1/4" or "1/4th" tokens (fractions are not part of direction)
    s = re.sub(r"\b1/4(?:th)?\b", " ", s, flags=re.I)
    # Fix OCR multi-char noise variants
    for bad, good in _OCR_FIX.items():
        s = re.sub(rf"\b{re.escape(bad)}\b", good, s, flags=re.I)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s


# A single direction token: NW / NE / SW / SE (with optional period/noise)
_DIR_PAT = r"(?:NW|NE|SW|SE|5W|5E)"

# Separator: -, /, whitespace, "of", "the", combinations thereof
_SEP_PAT = r"(?:[\s\-/,]+(?:of\s+|the\s+)?|(?:of|the)\s+)+"

# Full 3-level pattern (PDF order: fine → coarse)
_THREE_RE = re.compile(
    rf"({_DIR_PAT}){_SEP_PAT}({_DIR_PAT}){_SEP_PAT}({_DIR_PAT})",
    re.I,
)

# Fallback: 2-level pattern (less useful but detectable)
_TWO_RE = re.compile(
    rf"({_DIR_PAT}){_SEP_PAT}({_DIR_PAT})",
    re.I,
)


def _confidence_from_match(text: str, match_text: str, levels: int) -> float:
    """
    Heuristic confidence 0-1.
    - 3 levels: 0.85 base; boost if hyphen/slash separator used (clean OCR)
    - 2 levels: 0.45 (too ambiguous for single-cell resolution)
    """
    if levels == 3:
        base = 0.85
        # Clean separators → higher confidence
        if re.search(r"[/\-]", match_text):
            base = 0.93
        # Contains 1/4 notation → high confidence (it's a formal legal description)
        if re.search(r"1/4|¼", text):
            base = min(base + 0.05, 0.98)
        return base
    return 0.45


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_quadrant(text: str) -> dict | None:
    """
    Scan `text` for a 3-level PLSS quadrant string.

    Returns a result dict or None if not found.
    """
    if not text:
        return None

    norm = _normalise(text)

    m = _THREE_RE.search(norm)
    if m:
        # PDF order: fine→coarse → reverse for DB label
        pdf_parts = [m.group(i).upper() for i in (1, 2, 3)]
        # Fix OCR substitutions before lookup
        pdf_parts = [_OCR_FIX.get(p, p) for p in pdf_parts]
        # Validate all three are real direction tokens
        if not all(p in _DIR_BITS for p in pdf_parts):
            return None

        db_parts  = list(reversed(pdf_parts))
        db_label  = "-".join(db_parts)
        pdf_label = "-".join(pdf_parts)
        row, col  = label_to_row_col(db_label)
        if row is None:
            return None

        match_text = m.group(0)
        return {
            "pdf_label":  pdf_label,
            "db_label":   db_label,
            "row":        row,
            "col":        col,
            "levels":     3,
            "confidence": _confidence_from_match(text, match_text, 3),
            "match_text": match_text,
        }

    # 2-level fallback (report but row/col are None — can't pin exact cell)
    m2 = _TWO_RE.search(norm)
    if m2:
        pdf_parts = [m2.group(i).upper() for i in (1, 2)]
        pdf_parts = [_OCR_FIX.get(p, p) for p in pdf_parts]
        if not all(p in _DIR_BITS for p in pdf_parts):
            return None
        db_parts  = list(reversed(pdf_parts))
        return {
            "pdf_label":  "-".join(pdf_parts),
            "db_label":   "-".join(db_parts),   # 2-part, not queryable as exact cell
            "row":        None,
            "col":        None,
            "levels":     2,
            "confidence": _confidence_from_match(text, m2.group(0), 2),
            "match_text": m2.group(0),
        }

    return None

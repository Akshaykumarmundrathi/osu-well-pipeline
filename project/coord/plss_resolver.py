"""
PLSS coordinate resolver — Oklahoma well records pipeline.

Given a PLSS address (section, township, range, county) and optionally a
quadrant_label (e.g. "NE-SW-NW") and dot position from U-Net inference,
resolves the exact (lat, lon) of the well spot.

Algorithm (priority order)
--------------------------
1. If quadrant_label is available (from PDF OCR or U-Net nw field):
   Query plss_grid directly for the specific cell → use that cell's own
   minx/miny/maxx/maxy for corner computation (cell-level precision).
   Source: 'quadrant_direct'.

2. Otherwise compute corners from section MIN/MAX bbox + (row, col):
   Multiple lookup strategies ordered most→least specific:
   S1  exact_county      sect+twp+ns+rng+ew + county ILIKE
   S2  exact_no_county   sect+twp+ns+rng+ew (no county)
   S3  county_constrained ns/ew inferred from county constraints
   S4  ns_fallback        try N then S when north_south missing
   S5  ew_fallback        try W then E when east_west missing
   S6  county_stripped    strip 'County' suffix / try first word
   S7  section_adjacent   try section ±1 (OCR off-by-one)

3. Use dot position within cell for bilinear interpolation:
   rel_x = x_norm * 8 - (col - 1)
   rel_y = y_norm * 8 - (row - 1)
   Falls back to cell centre (0.5, 0.5) if x_norm/y_norm unavailable.

resolve() return dict keys
--------------------------
  lat, lon                    float | None
  source                      str   (strategy code)
  sec_minx/miny/maxx/maxy     float | None  (section or cell bbox)
  cell_corners                dict | None   {tl/tr/bl/br: (lon, lat)}
  rel_x, rel_y                float | None
  flags                       list[str]

Source codes: quadrant_direct, exact_county, exact_no_county,
  county_constrained, ns_fallback, ew_fallback, ns_ew_fallback,
  county_stripped, section_adjacent, rds_miss, bounds_invalid,
  parse_failed, geometry_error, quadrant_mismatch (U-Net vs OCR disagree)
"""

import logging
import os
import re

from ocr.quadrant_extractor import label_to_row_col, row_col_to_label

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RDS connection — all values come from environment variables, no defaults.
# Required: RDS_HOST, RDS_DBNAME, RDS_USER, RDS_PASSWORD
# Optional: RDS_PORT (defaults to 5432)
# ---------------------------------------------------------------------------
def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            "See .env.example for the full list."
        )
    return v

_RDS_HOST     = os.environ.get("RDS_HOST", "")
_RDS_PORT     = int(os.environ.get("RDS_PORT", "5432"))
_RDS_DBNAME   = os.environ.get("RDS_DBNAME",   "")
_RDS_USER     = os.environ.get("RDS_USER",     "")
_RDS_PASSWORD = os.environ.get("RDS_PASSWORD", "")

GRID_SIZE = 8

# ---------------------------------------------------------------------------
# Oklahoma PLSS hard bounds
# ---------------------------------------------------------------------------
OK_SECTION_MIN,    OK_SECTION_MAX    = 1,  36
OK_TOWNSHIP_MAX_N, OK_TOWNSHIP_MAX_S = 29, 8
OK_RANGE_MAX_E,    OK_RANGE_MAX_W    = 26, 28

_NS_TRY_ORDER = ("N", "S")
_EW_TRY_ORDER = ("W", "E")


# ---------------------------------------------------------------------------
# Input parsers
# ---------------------------------------------------------------------------

def _parse_trs(val: str, direction_chars: str):
    if not val:
        return None, None
    s = str(val).strip().upper()
    s = re.sub(r"^[TR]\s*", "", s)
    s = re.sub(r"\.0+$",    "", s)
    num_m = re.search(r"\d+", s)
    dir_m = re.search(f"[{direction_chars}]", s)
    return (int(num_m.group()) if num_m else None,
            dir_m.group()       if dir_m else None)


def parse_township(val: str):
    return _parse_trs(val, "NS")


def parse_range(val: str):
    return _parse_trs(val, "EW")


def parse_section(val) -> int | None:
    try:
        n = int(float(str(val)))
        return n if n else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Oklahoma PLSS bounds validation
# ---------------------------------------------------------------------------

def validate_oklahoma_plss(section, township, ns, range_num, ew) -> tuple[bool, str]:
    if section is None or section < OK_SECTION_MIN or section > OK_SECTION_MAX:
        return False, f"section_invalid:{section}"
    if township is None or township < 1:
        return False, f"township_invalid:{township}"
    if ns == "N" and township > OK_TOWNSHIP_MAX_N:
        return False, f"township_N_too_high:{township}>{OK_TOWNSHIP_MAX_N}"
    if ns == "S" and township > OK_TOWNSHIP_MAX_S:
        return False, f"township_S_too_high:{township}>{OK_TOWNSHIP_MAX_S}"
    if ns is None and township > OK_TOWNSHIP_MAX_N:
        return False, f"township_too_high:{township}"
    if range_num is None or range_num < 1:
        return False, f"range_invalid:{range_num}"
    if ew == "E" and range_num > OK_RANGE_MAX_E:
        return False, f"range_E_too_high:{range_num}>{OK_RANGE_MAX_E}"
    if ew == "W" and range_num > OK_RANGE_MAX_W:
        return False, f"range_W_too_high:{range_num}>{OK_RANGE_MAX_W}"
    if ew is None and range_num > OK_RANGE_MAX_W:
        return False, f"range_too_high:{range_num}"
    return True, "ok"


# ---------------------------------------------------------------------------
# County name helpers
# ---------------------------------------------------------------------------

def _county_variants(raw: str) -> list[str]:
    if not raw:
        return ["%"]
    s = raw.strip()
    patterns: list[str] = [f"%{s}%"]
    base = re.sub(r"\s+county\.?$", "", s, flags=re.I).strip()
    if base and base.lower() != s.lower():
        patterns.append(f"%{base}%")
    first = s.split()[0] if s.split() else ""
    if first and first.lower() not in (s.lower(), base.lower()):
        patterns.append(f"%{first}%")
    return patterns


# ---------------------------------------------------------------------------
# Geometry: cell corners + bilinear interpolation
# ---------------------------------------------------------------------------

def compute_cell_corners(sec_minx, sec_miny, sec_maxx, sec_maxy,
                          row: int, col: int) -> dict:
    """
    Corners of cell (row, col) within a section.
    Uses section-level bbox (derived from MIN/MAX across all 64 cells).
    Returns {tl, tr, bl, br} each as (lon, lat).
    """
    cell_w = (sec_maxx - sec_minx) / GRID_SIZE
    cell_h = (sec_maxy - sec_miny) / GRID_SIZE
    left  = sec_minx + (col - 1) * cell_w
    right = sec_minx +  col      * cell_w
    top   = sec_maxy - (row - 1) * cell_h
    bot   = sec_maxy -  row      * cell_h
    return {"tl": (left, top), "tr": (right, top),
            "bl": (left, bot), "br": (right, bot)}


def cell_corners_from_bbox(cell_minx, cell_miny, cell_maxx, cell_maxy) -> dict:
    """
    Build corners dict from a cell's own minx/miny/maxx/maxy.
    More precise than computing from section MIN/MAX.
    """
    return {
        "tl": (cell_minx, cell_maxy),   # NW
        "tr": (cell_maxx, cell_maxy),   # NE
        "bl": (cell_minx, cell_miny),   # SW
        "br": (cell_maxx, cell_miny),   # SE
    }


def bilinear_interpolate(corners: dict,
                         rel_x: float, rel_y: float) -> tuple[float, float]:
    """Bilinear interpolation. rel_x 0→W, rel_y 0→N. Returns (lat, lon)."""
    tl, tr, bl, br = corners["tl"], corners["tr"], corners["bl"], corners["br"]
    rx, ry = rel_x, rel_y
    lon = (tl[0]*(1-rx)*(1-ry) + tr[0]*rx*(1-ry)
         + bl[0]*(1-rx)*ry    + br[0]*rx*ry)
    lat = (tl[1]*(1-rx)*(1-ry) + tr[1]*rx*(1-ry)
         + bl[1]*(1-rx)*ry    + br[1]*rx*ry)
    return lat, lon


def dot_relative_in_cell(x_norm, y_norm, row: int, col: int) -> tuple[float, float]:
    """
    Convert section-normalised dot coordinates to cell-local (0-1).
    Falls back to cell centre (0.5, 0.5) if inputs are None or out of range.
    """
    if x_norm is not None and y_norm is not None:
        try:
            rx = float(x_norm) * GRID_SIZE - (col - 1)
            ry = float(y_norm) * GRID_SIZE - (row - 1)
            if 0.0 <= rx <= 1.0 and 0.0 <= ry <= 1.0:
                return rx, ry
        except (TypeError, ValueError):
            pass
    return 0.5, 0.5


# ---------------------------------------------------------------------------
# SQL templates
# ---------------------------------------------------------------------------

# Section-level query: aggregate bbox across all cells of a section
_SQL_SECTION_BOUNDS = """
SELECT
    MIN(minx) AS sec_minx, MIN(miny) AS sec_miny,
    MAX(maxx) AS sec_maxx, MAX(maxy) AS sec_maxy,
    COUNT(*)  AS cells
FROM plss_grid
WHERE
    sect_num     = %(sect_num)s
    AND township    = %(township)s
    AND north_south = %(ns)s
    AND "range"     = %(range_num)s
    AND east_west   = %(ew)s
    {county_clause}
"""

# Cell-level query: exact cell by quadrant_label
_SQL_CELL_BY_QUAD = """
SELECT minx, miny, maxx, maxy
FROM plss_grid
WHERE
    sect_num     = %(sect_num)s
    AND township    = %(township)s
    AND north_south = %(ns)s
    AND "range"     = %(range_num)s
    AND east_west   = %(ew)s
    AND quadrant_label = %(quad_label)s
    {county_clause}
LIMIT 1
"""

_COUNTY_CLAUSE = "AND county_name ILIKE %(county)s"


def _section_sql(with_county: bool) -> str:
    return _SQL_SECTION_BOUNDS.format(
        county_clause=_COUNTY_CLAUSE if with_county else ""
    )


def _cell_sql(with_county: bool) -> str:
    return _SQL_CELL_BY_QUAD.format(
        county_clause=_COUNTY_CLAUSE if with_county else ""
    )


# ---------------------------------------------------------------------------
# PLSSResolver
# ---------------------------------------------------------------------------

class PLSSResolver:
    """
    Lazy-connecting PLSS cell + coordinate resolver.

    Usage:
        r = PLSSResolver()
        r.load_county_constraints(output_dir)   # optional but recommended
        result = r.resolve(
            section=14, township_num=5, ns='N', range_num=9, ew='W',
            county='Blaine', dot_row=3, dot_col=5,
            x_norm=0.63, y_norm=0.42,
            quadrant_label='NE-SW-NW',   # from OCR or U-Net nw field
        )
        r.close()
    """

    def __init__(self):
        self._conn = None
        self._county_constraints: dict = {}

    # -- connection -----------------------------------------------------------

    def _connect(self):
        import psycopg2
        host     = os.environ.get("RDS_HOST")     or _require_env("RDS_HOST")
        dbname   = os.environ.get("RDS_DBNAME")   or _require_env("RDS_DBNAME")
        user     = os.environ.get("RDS_USER")     or _require_env("RDS_USER")
        password = os.environ.get("RDS_PASSWORD") or _require_env("RDS_PASSWORD")
        port     = int(os.environ.get("RDS_PORT", "5432"))
        self._conn = psycopg2.connect(
            host=host, port=port,
            dbname=dbname, user=user, password=password,
            sslmode="require", connect_timeout=15,
        )
        self._conn.set_session(readonly=True, autocommit=True)
        log.debug("PLSSResolver: connected to RDS at %s", host)

    def _cursor(self):
        if self._conn is None or self._conn.closed:
            self._connect()
        return self._conn.cursor()

    def close(self):
        if self._conn and not self._conn.closed:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def load_county_constraints(self, output_dir):
        """Load cached county constraints from output_dir (if available)."""
        from coord.county_constraints import load
        self._county_constraints = load(output_dir)
        log.debug("PLSSResolver: loaded %d county constraints",
                  len(self._county_constraints))

    # -- public resolve -------------------------------------------------------

    def resolve(self,
                section,
                township_num, ns: str | None,
                range_num,    ew: str | None,
                county: str,
                dot_row, dot_col,
                x_norm=None, y_norm=None,
                quadrant_label: str | None = None,
                unet_nw: str | None = None,
                ) -> dict:
        """
        Full resolution pipeline.

        quadrant_label : DB-format label from OCR (e.g. 'NE-SW-NW').
                         Takes priority over dot_row/dot_col for cell lookup.
        unet_nw        : DB-format label from U-Net nw field.
                         Used to cross-validate quadrant_label.

        Returns dict with keys:
            lat, lon, source, sec_minx, sec_miny, sec_maxx, sec_maxy,
            cell_corners, rel_x, rel_y, flags
        """
        result = {
            "lat": None, "lon": None, "source": "rds_miss",
            "sec_minx": None, "sec_miny": None,
            "sec_maxx": None, "sec_maxy": None,
            "cell_corners": None, "rel_x": None, "rel_y": None,
            "flags": [],
        }

        # -- Parse inputs ----------------------------------------------------
        try:
            sect  = parse_section(section)
            twp   = int(float(str(township_num))) if township_num is not None else None
            rng   = int(float(str(range_num)))    if range_num    is not None else None
            row_i = int(dot_row)
            col_i = int(dot_col)
        except (TypeError, ValueError):
            result["source"] = "parse_failed"
            return result

        if not (1 <= row_i <= GRID_SIZE and 1 <= col_i <= GRID_SIZE):
            result["source"] = "parse_failed"
            result["flags"].append(f"dot_rc_invalid:{dot_row},{dot_col}")
            return result

        # -- Resolve effective row/col from quadrant_label -------------------
        # Prefer OCR quadrant_label if valid; cross-validate with U-Net nw.
        eff_row, eff_col, eff_quad = self._resolve_quadrant(
            quadrant_label, unet_nw, row_i, col_i, result["flags"]
        )

        # -- Oklahoma hard-bounds validation ----------------------------------
        # Use county constraints to infer missing NS/EW if available
        ns_eff, ew_eff = self._effective_directions(ns, ew, county, twp, rng)

        valid, reason = validate_oklahoma_plss(sect, twp, ns_eff[0] if ns_eff else ns,
                                               rng, ew_eff[0] if ew_eff else ew)
        if not valid:
            result["source"] = "bounds_invalid"
            result["flags"].append(reason)
            return result

        # -- Strategy 1: quadrant_label direct lookup (cell-level precision) --
        if eff_quad:
            cell_bbox = self._find_cell_by_quad(
                sect, twp, ns_eff, rng, ew_eff, eff_quad, county
            )
            if cell_bbox:
                corners = cell_corners_from_bbox(*cell_bbox)
                rel_x, rel_y = dot_relative_in_cell(x_norm, y_norm, eff_row, eff_col)
                return self._finalise(result, corners, cell_bbox,
                                      rel_x, rel_y, "quadrant_direct",
                                      x_norm, y_norm)

        # -- Strategy 2+: section-bbox fallback with multi-strategy lookup ---
        strategies = self._build_strategies(
            ns_eff, ew_eff, ns, ew, county, sect, rng
        )
        bounds = source_label = None
        for ns_list, ew_list, county_patterns, label in strategies:
            bounds = self._find_section_bounds(sect, twp, ns_list, rng, ew_list, county_patterns)
            if bounds:
                source_label = label
                break

        if bounds is None:
            result["source"] = "rds_miss"
            return result

        corners = compute_cell_corners(*bounds, eff_row, eff_col)
        rel_x, rel_y = dot_relative_in_cell(x_norm, y_norm, eff_row, eff_col)
        return self._finalise(result, corners, bounds,
                              rel_x, rel_y, source_label, x_norm, y_norm)

    # -- quadrant resolution -------------------------------------------------

    def _resolve_quadrant(self, quad_label, unet_nw, row_i, col_i, flags):
        """
        Determine effective (row, col, db_label) from all available sources.

        Priority: OCR quadrant_label > U-Net nw > (row_i, col_i)
        Cross-validates and adds flags when sources disagree.
        """
        ocr_row = ocr_col = ocr_lab = None
        unet_row = unet_col = unet_lab = None

        if quad_label:
            ocr_row, ocr_col = label_to_row_col(quad_label)
            if ocr_row is not None:
                ocr_lab = quad_label

        if unet_nw:
            unet_row, unet_col = label_to_row_col(unet_nw)
            if unet_row is not None:
                unet_lab = unet_nw

        # Cross-validation
        if ocr_lab and unet_lab and (ocr_row, ocr_col) != (unet_row, unet_col):
            flags.append(
                f"quadrant_mismatch:ocr={ocr_lab}({ocr_row},{ocr_col})"
                f"_unet={unet_lab}({unet_row},{unet_col})"
            )

        # Also cross-validate U-Net nw against U-Net row/col
        unet_expected = row_col_to_label(row_i, col_i)
        if unet_lab and unet_expected and unet_lab != unet_expected:
            flags.append(
                f"unet_nw_rc_mismatch:nw={unet_lab}_rc=({row_i},{col_i})->{unet_expected}"
            )

        # Pick best
        if ocr_lab:
            return ocr_row, ocr_col, ocr_lab        # OCR has priority
        if unet_lab:
            return unet_row, unet_col, unet_lab     # U-Net nw
        if unet_expected:
            return row_i, col_i, unet_expected      # derive from row/col
        return row_i, col_i, None

    # -- direction inference -------------------------------------------------

    def _effective_directions(self, ns, ew, county, twp, rng):
        """Return (ns_list, ew_list) using county constraints if needed."""
        if self._county_constraints:
            from coord.county_constraints import infer_directions
            ns_list, ew_list = infer_directions(
                self._county_constraints, county, twp, rng, ns, ew
            )
        else:
            ns_list = [ns] if ns in ("N", "S") else list(_NS_TRY_ORDER)
            ew_list = [ew] if ew in ("E", "W") else list(_EW_TRY_ORDER)
        return ns_list, ew_list

    # -- cell-level lookup ---------------------------------------------------

    def _find_cell_by_quad(self, sect, twp, ns_list, rng, ew_list,
                            quad_label, county):
        """Query the specific quadrant_label cell row; return (minx,miny,maxx,maxy) or None."""
        county_pats = _county_variants(county)
        for ns in ns_list:
            for ew in ew_list:
                # Try with county first, then without
                for pat in (county_pats + ["%"]):
                    with_county = pat != "%"
                    params = {
                        "sect_num":   sect,   "township":  twp,
                        "ns":         ns,     "range_num": rng,
                        "ew":         ew,     "quad_label": quad_label,
                    }
                    if with_county:
                        params["county"] = pat
                    row = self._run_cell_query(_cell_sql(with_county), params)
                    if row:
                        return row
        return None

    # -- section-level lookup ------------------------------------------------

    def _build_strategies(self, ns_eff, ew_eff, ns_raw, ew_raw, county, sect, rng):
        """
        Build ordered strategy list for section-level bbox lookup.
        Each entry: (ns_list, ew_list, county_patterns, label).
        """
        ns_exact = [ns_raw] if ns_raw in ("N", "S") else None
        ew_exact = [ew_raw] if ew_raw in ("E", "W") else None
        county_variants = _county_variants(county)

        strats = []

        if ns_exact and ew_exact:
            for pat in county_variants:
                strats.append((ns_exact, ew_exact, [pat], "exact_county"))
            strats.append((ns_exact, ew_exact, ["%"], "exact_no_county"))
            # County-constrained: use inferred directions
            if ns_eff != ns_exact or ew_eff != ew_exact:
                strats.append((ns_eff, ew_eff, county_variants[:1],
                                "county_constrained"))
                strats.append((ns_eff, ew_eff, ["%"], "county_constrained"))
            # Adjacent section fallback
            for adj in [sect - 1, sect + 1]:
                if OK_SECTION_MIN <= adj <= OK_SECTION_MAX:
                    strats.append((ns_exact, ew_exact, ["%"], "section_adjacent"))
        elif ns_exact:
            for pat in county_variants:
                strats.append((ns_exact, ew_eff, [pat], "ew_fallback"))
            strats.append((ns_exact, ew_eff, ["%"], "ew_fallback"))
        elif ew_exact:
            for pat in county_variants:
                strats.append((ns_eff, ew_exact, [pat], "ns_fallback"))
            strats.append((ns_eff, ew_exact, ["%"], "ns_fallback"))
        else:
            strats.append((ns_eff, ew_eff, county_variants[:1], "ns_ew_fallback"))
            strats.append((ns_eff, ew_eff, ["%"], "ns_ew_fallback"))

        return strats

    def _find_section_bounds(self, sect, twp, ns_list, rng, ew_list, county_patterns):
        for ns in ns_list:
            for ew in ew_list:
                for pat in county_patterns:
                    with_county = pat != "%"
                    params = {
                        "sect_num": sect, "township": twp,
                        "ns": ns, "range_num": rng, "ew": ew,
                    }
                    if with_county:
                        params["county"] = pat
                    row = self._run_section_query(_section_sql(with_county), params)
                    if row:
                        return row
        return None

    # -- DB runners ----------------------------------------------------------

    def _run_section_query(self, sql, params):
        """Returns (minx, miny, maxx, maxy) or None."""
        try:
            cur = self._cursor()
            cur.execute(sql, params)
            row = cur.fetchone()
            if row and row[4] and row[0] is not None:
                minx, miny, maxx, maxy = row[0], row[1], row[2], row[3]
                if minx < maxx and miny < maxy:
                    return float(minx), float(miny), float(maxx), float(maxy)
        except Exception as exc:
            log.warning("PLSSResolver section query error: %s", exc)
            self._conn = None
        return None

    def _run_cell_query(self, sql, params):
        """Returns (minx, miny, maxx, maxy) for a specific cell, or None."""
        try:
            cur = self._cursor()
            cur.execute(sql, params)
            row = cur.fetchone()
            if row and row[0] is not None:
                minx, miny, maxx, maxy = row[0], row[1], row[2], row[3]
                if minx < maxx and miny < maxy:
                    return float(minx), float(miny), float(maxx), float(maxy)
        except Exception as exc:
            log.warning("PLSSResolver cell query error: %s", exc)
            self._conn = None
        return None

    # -- finalise ------------------------------------------------------------

    def _finalise(self, result, corners, bbox, rel_x, rel_y,
                  source, x_norm, y_norm):
        """Interpolate, sanity-check Oklahoma bbox, populate result dict."""
        try:
            lat, lon = bilinear_interpolate(corners, rel_x, rel_y)
        except Exception as exc:
            result["source"] = "geometry_error"
            result["flags"].append(f"interpolation_error:{exc}")
            return result

        if not (-103.1 <= lon <= -94.4 and 33.6 <= lat <= 37.1):
            result["source"] = "geometry_error"
            result["flags"].append(f"out_of_oklahoma:lat={lat:.3f},lon={lon:.3f}")
            return result

        if x_norm is None or y_norm is None:
            result["flags"].append("cell_centre_used")

        result.update({
            "lat": round(lat, 7),
            "lon": round(lon, 7),
            "source": source,
            "sec_minx": bbox[0], "sec_miny": bbox[1],
            "sec_maxx": bbox[2], "sec_maxy": bbox[3],
            "cell_corners": {k: list(v) for k, v in corners.items()},
            "rel_x": round(rel_x, 4),
            "rel_y": round(rel_y, 4),
        })
        return result

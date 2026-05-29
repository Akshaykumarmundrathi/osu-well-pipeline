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
# Oklahoma PLSS geographic priors
# ---------------------------------------------------------------------------
# Counties east of the Indian Meridian → Range East is most common
_COUNTY_RANGE_EAST_FIRST = frozenset({
    "adair", "cherokee", "craig", "delaware", "mayes", "nowata",
    "ottawa", "rogers", "sequoyah",
})
# Panhandle counties use the Cimarron Meridian → Range East only
_COUNTY_PANHANDLE = frozenset({"cimarron", "beaver", "texas"})


def _clean_county_key(county: str) -> str:
    """Normalise county name to a stats-lookup key (lower-case, no ' county' suffix)."""
    s = (county or "").lower().strip()
    return re.sub(r"\s+county\.?$", "", s).strip()


def _oklahoma_ns_prior(county_clean: str) -> list[str]:
    """
    Township-direction geographic prior.
    In Oklahoma, Township N dominates the entire state (baseline runs
    through central OK; only ~8 southern-tier rows are Township S).
    """
    return ["N", "S"]


def _oklahoma_ew_prior(county_clean: str) -> list[str]:
    """
    Range-direction geographic prior for Oklahoma.
    Panhandle: Cimarron Meridian → East only.
    East Oklahoma counties: Indian Meridian → East first.
    All others (most of the state): West first.
    """
    if county_clean in _COUNTY_PANHANDLE:
        return ["E"]           # Cimarron Meridian — Range East only
    if county_clean in _COUNTY_RANGE_EAST_FIRST:
        return ["E", "W"]      # East Oklahoma — East first
    return ["W", "E"]          # Default: West first (most of Oklahoma)


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


# Pass-3: infer missing township — given (sect, range, ew, [county])
_SQL_INFER_TOWNSHIP = """
SELECT township, north_south, COUNT(*) AS cnt
FROM plss_grid
WHERE
    sect_num  = %(sect_num)s
    AND "range"  = %(range_num)s
    AND east_west = %(ew)s
    {county_clause}
GROUP BY township, north_south
ORDER BY cnt DESC
LIMIT 20
"""

# Pass-3: infer missing range — given (sect, township, ns, [county])
_SQL_INFER_RANGE = """
SELECT "range", east_west, COUNT(*) AS cnt
FROM plss_grid
WHERE
    sect_num     = %(sect_num)s
    AND township    = %(township)s
    AND north_south = %(ns)s
    {county_clause}
GROUP BY "range", east_west
ORDER BY cnt DESC
LIMIT 20
"""


def _infer_sql(template: str, with_county: bool) -> str:
    return template.format(
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
        # Per-county direction statistics from RDS (N/S and E/W cell counts).
        # Keyed by _clean_county_key(); populated by load_county_direction_stats().
        self._county_direction_stats: dict = {}
        # In-memory caches populated by prefetch_for_records().
        # Key for section cache : (sect, twp, ns, rng, ew)
        # Key for cell cache    : (sect, twp, ns, rng, ew, quad_label)
        # Value                 : (minx, miny, maxx, maxy)
        self._section_cache: dict[tuple, tuple] = {}
        self._cell_cache:    dict[tuple, tuple] = {}
        # Counters for hit-rate logging
        self._cache_hits   = 0
        self._cache_misses = 0

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

    # -- batch prefetch -------------------------------------------------------

    def prefetch_for_records(self, records: list[dict]) -> None:
        """
        Pre-warm the section and cell caches with TWO batch queries so that
        subsequent resolve() calls hit the dict instead of RDS.

        Replaces O(N × strategies) round-trips with 2 fixed queries regardless
        of how many records are processed.

        records must have at minimum the keys that _process_row uses:
            section / location_section, township / location_township,
            range / location_range, dot_nw, location_quadrant_db
        """
        _CHUNK = 500   # max keys per query (each key has 3–4 params; well under 65535)

        # -- collect unique PLSS keys ----------------------------------------
        sect_keys: set[tuple[int,int,int]]         = set()   # (sect, twp, rng)
        cell_keys: set[tuple[int,int,int,str]]     = set()   # (sect, twp, rng, quad)

        for r in records:
            raw_sect = (r.get("section") or r.get("location_section") or "")
            raw_twp  = (r.get("township") or r.get("location_township") or "")
            raw_rng  = (r.get("range") or r.get("location_range") or "")

            sect = parse_section(raw_sect)
            twp_num, _ = parse_township(str(raw_twp))
            rng_num, _ = parse_range(str(raw_rng))

            if not (sect and twp_num and rng_num):
                continue

            sect_keys.add((sect, twp_num, rng_num))

            # Prefer dot_nw (already row_col_to_label output from UNet detector).
            # Fall back to deriving from dot_row/dot_col — no OCR dependency.
            quad = (r.get("dot_nw") or r.get("location_quadrant_db") or "").strip()
            if not quad:
                dr = r.get("dot_row", "")
                dc = r.get("dot_col", "")
                if dr and dc:
                    try:
                        quad = row_col_to_label(int(dr), int(dc)) or ""
                    except Exception:
                        quad = ""
            if quad:
                cell_keys.add((sect, twp_num, rng_num, quad))

        if not sect_keys:
            log.debug("prefetch_for_records: no parseable PLSS keys found")
            return

        log.info(
            "prefetch_for_records: %d unique (sect,twp,rng) keys, "
            "%d unique cell keys — running batch queries…",
            len(sect_keys), len(cell_keys),
        )

        # -- helper: run one chunk of a batch query --------------------------
        def _run_batch(sql_tpl: str, keys: list[tuple], n_fields: int) -> list:
            """Execute sql_tpl with parameterised IN clause for one chunk."""
            ph = ",".join([f"({','.join(['%s']*n_fields)})"] * len(keys))
            flat = [v for tup in keys for v in tup]
            try:
                cur = self._cursor()
                cur.execute(sql_tpl % ph, flat)
                return cur.fetchall()
            except Exception as exc:
                log.warning("prefetch batch query failed: %s", exc)
                self._conn = None
                return []

        # -- section bbox batch -----------------------------------------------
        _SQL_SECT = """
            SELECT sect_num, township, north_south, "range", east_west,
                   MIN(minx), MIN(miny), MAX(maxx), MAX(maxy)
            FROM   plss_grid
            WHERE  (sect_num, township, "range") IN (%s)
            GROUP  BY sect_num, township, north_south, "range", east_west
        """
        sect_list = list(sect_keys)
        for i in range(0, len(sect_list), _CHUNK):
            chunk = sect_list[i:i+_CHUNK]
            for row in _run_batch(_SQL_SECT, chunk, 3):
                sn, tw, ns, rn, ew, mnx, mny, mxx, mxy = row
                if mnx is not None and mnx < mxx and mny < mxy:
                    self._section_cache[(int(sn), int(tw), ns, int(rn), ew)] = (
                        float(mnx), float(mny), float(mxx), float(mxy)
                    )

        log.info("  → section cache: %d entries", len(self._section_cache))

        # -- cell bbox batch --------------------------------------------------
        if cell_keys:
            _SQL_CELL = """
                SELECT sect_num, township, north_south, "range", east_west,
                       quadrant_label, minx, miny, maxx, maxy
                FROM   plss_grid
                WHERE  (sect_num, township, "range", quadrant_label) IN (%s)
            """
            cell_list = list(cell_keys)
            for i in range(0, len(cell_list), _CHUNK):
                chunk = cell_list[i:i+_CHUNK]
                for row in _run_batch(_SQL_CELL, chunk, 4):
                    sn, tw, ns, rn, ew, ql, mnx, mny, mxx, mxy = row
                    if mnx is not None and mnx < mxx and mny < mxy:
                        self._cell_cache[(int(sn), int(tw), ns, int(rn), ew, ql)] = (
                            float(mnx), float(mny), float(mxx), float(mxy)
                        )

            log.info("  → cell cache: %d entries", len(self._cell_cache))

    def cache_stats(self) -> dict:
        """Return hit-rate counters set during resolve() calls."""
        total = self._cache_hits + self._cache_misses
        return {
            "hits":   self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate": round(self._cache_hits / total, 3) if total else 0.0,
        }

    def load_county_direction_stats(self) -> None:
        """
        One-time RDS query: build a per-county direction-priority table.

        For each county name in plss_grid, counts how many cells use N vs S
        (township direction) and W vs E (range direction).  The result is
        stored in self._county_direction_stats and used by
        _effective_directions() to order fallback attempts by statistical
        probability rather than a fixed default.

        Called once during enricher setup; subsequent calls are no-ops.
        Safe to call even if RDS is temporarily slow — logs a warning and
        falls back to geographic priors (_oklahoma_ns/ew_prior).
        """
        if self._county_direction_stats:
            return  # already loaded

        _SQL = """
            SELECT
                lower(trim(county_name))  AS county_clean,
                north_south,
                east_west,
                COUNT(*)                  AS cnt
            FROM  plss_grid
            WHERE north_south IN ('N', 'S')
              AND east_west   IN ('E', 'W')
              AND county_name IS NOT NULL
              AND trim(county_name) != ''
            GROUP BY lower(trim(county_name)), north_south, east_west
        """
        try:
            cur = self._cursor()
            cur.execute(_SQL)
            rows = cur.fetchall()
        except Exception as exc:
            log.warning("load_county_direction_stats: query failed: %s", exc)
            return

        from collections import defaultdict
        ns_cnt: dict[str, dict[str, int]] = defaultdict(lambda: {"N": 0, "S": 0})
        ew_cnt: dict[str, dict[str, int]] = defaultdict(lambda: {"W": 0, "E": 0})

        for county_raw, ns, ew, cnt in rows:
            if not county_raw:
                continue
            # Normalise key the same way callers do: strip "county" suffix
            county_clean = _clean_county_key(county_raw)
            if not county_clean:
                continue
            if ns in ("N", "S"):
                ns_cnt[county_clean][ns] += int(cnt)
            if ew in ("E", "W"):
                ew_cnt[county_clean][ew] += int(cnt)

        stats: dict[str, dict] = {}
        for county in set(ns_cnt) | set(ew_cnt):
            ns_sorted = sorted(ns_cnt[county].items(), key=lambda x: -x[1])
            ew_sorted = sorted(ew_cnt[county].items(), key=lambda x: -x[1])
            stats[county] = {
                "ns_order": [k for k, v in ns_sorted if v > 0] or ["N", "S"],
                "ew_order": [k for k, v in ew_sorted if v > 0] or ["W", "E"],
            }

        self._county_direction_stats = stats
        log.info(
            "load_county_direction_stats: %d counties — direction priors loaded",
            len(stats),
        )

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

        # Detect whether a county direction override was applied.
        # When OCR gave an explicit direction that contradicts county stats
        # (e.g. "13W" in Tulsa County which is 100% range-East), the prefetch
        # cache was built using the OCR's wrong direction and MUST be bypassed —
        # otherwise the cache short-circuits the county-corrected live query.
        county_dir_override = (
            (ew in ("E", "W") and len(ew_eff) > 1 and ew_eff[0] != ew) or
            (ns in ("N", "S") and len(ns_eff) > 1 and ns_eff[0] != ns)
        )

        valid, reason = validate_oklahoma_plss(sect, twp, ns_eff[0] if ns_eff else ns,
                                               rng, ew_eff[0] if ew_eff else ew)
        if not valid:
            result["source"] = "bounds_invalid"
            result["flags"].append(reason)
            return result

        # -- Strategy 1: quadrant_label direct lookup (cell-level precision) --
        if eff_quad:
            cell_bbox = self._find_cell_by_quad(
                sect, twp, ns_eff, rng, ew_eff, eff_quad, county,
                skip_cache=county_dir_override,
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
            bounds = self._find_section_bounds(
                sect, twp, ns_list, rng, ew_list, county_patterns,
                skip_cache=county_dir_override,
            )
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
        """
        Return (ns_list, ew_list) for strategy building.

        Priority when a direction is MISSING from OCR:
          1. county_constraints file (pre-built from pipeline metadata)
          2. Per-county direction statistics from RDS (most-probable first)
          3. Oklahoma geographic priors (_oklahoma_ns/ew_prior)
          4. Global defaults (_NS_TRY_ORDER / _EW_TRY_ORDER)

        County-direction override (soft correction):
          When a direction IS present from OCR but the county is statistically
          100% the opposite direction (e.g. Tulsa County is always range-East,
          but OCR extracted "13W"), put the county-preferred direction FIRST so
          the RDS lookup tries the geographically correct cell before the OCR
          direction.  This recovers the common OCR E/W confusion in eastern OK.
          The OCR direction is kept as a fallback (second element) so genuine
          edge cases still resolve.

          Override is applied only when county stats are 100% one direction
          (len(order)==1) — never overrides when the county uses both.
        """
        if self._county_constraints:
            from coord.county_constraints import infer_directions
            return infer_directions(
                self._county_constraints, county, twp, rng, ns, ew
            )

        county_clean = _clean_county_key(county)
        stats = self._county_direction_stats.get(county_clean) or {}

        if ns in ("N", "S"):
            county_ns = stats.get("ns_order") or []
            if (len(county_ns) == 1 and county_ns[0] != ns):
                # County is 100% one NS direction, OCR gave the other.
                # Try county-preferred direction first, OCR direction as fallback.
                ns_list = [county_ns[0], ns]
                log.debug("ns county-override %s: RDS stats say %s-only, OCR gave %s",
                          county_clean, county_ns[0], ns)
            else:
                ns_list = [ns]
        else:
            # Statistical prior -> geographic prior -> global default
            ns_list = (stats.get("ns_order") or
                       _oklahoma_ns_prior(county_clean) or
                       list(_NS_TRY_ORDER))

        if ew in ("E", "W"):
            county_ew = stats.get("ew_order") or []
            if (len(county_ew) == 1 and county_ew[0] != ew):
                # County is 100% one EW direction, OCR gave the other.
                # Typical case: eastern OK county (Tulsa, Creek, Washington)
                # always range-East, but OCR read "13W" instead of "13E".
                ew_list = [county_ew[0], ew]
                log.debug("ew county-override %s: RDS stats say %s-only, OCR gave %s",
                          county_clean, county_ew[0], ew)
            else:
                ew_list = [ew]
        else:
            ew_list = (stats.get("ew_order") or
                       _oklahoma_ew_prior(county_clean) or
                       list(_EW_TRY_ORDER))

        return ns_list, ew_list

    # -- cell-level lookup ---------------------------------------------------

    def _find_cell_by_quad(self, sect, twp, ns_list, rng, ew_list,
                            quad_label, county,
                            strict_county: bool = False,
                            skip_cache: bool = False):
        """
        Return (minx,miny,maxx,maxy) for the specific quadrant_label cell,
        or None.

        strict_county=True : only accept county-filtered matches; no fallback
                             to no-county query.  Used by resolve_exhaustive()
                             to prevent false positives when inferring direction.
        skip_cache=True    : bypass the prefetch cache entirely and force a
                             live RDS query.  Required when a county direction
                             override is active — the prefetch cache was built
                             using the OCR's (potentially wrong) direction and
                             would return a cell from the wrong part of the state.
        """
        # -- cache check (skip when county verification is required, or when
        #    a county direction override means the cache has stale direction data)
        if not strict_county and not skip_cache:
            for ns in ns_list:
                for ew in ew_list:
                    cached = self._cell_cache.get((sect, twp, ns, rng, ew, quad_label))
                    if cached:
                        self._cache_hits += 1
                        return cached

        # -- live RDS query ---------------------------------------------------
        self._cache_misses += 1
        county_pats = _county_variants(county)
        # Build the pattern list: county-filtered patterns + optional no-county
        all_pats = county_pats if strict_county else (county_pats + ["%"])
        for ns in ns_list:
            for ew in ew_list:
                for pat in all_pats:
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
                        # Warm the cache so any retry hits it
                        self._cell_cache[(sect, twp, ns, rng, ew, quad_label)] = row
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

    def _find_section_bounds(self, sect, twp, ns_list, rng, ew_list,
                              county_patterns, skip_cache: bool = False):
        """
        Return (minx, miny, maxx, maxy) for the section bbox, or None.

        skip_cache=True  : bypass the pre-warmed section cache and go straight
                           to a live county-filtered query.  Used by
                           resolve_exhaustive() to prevent returning a cached
                           section from a different county/direction that
                           happens to share the same (sect, twp, rng) numbers.
        skip_cache=False : (default) check cache first for fast lookups.
        """
        # -- cache check (only when cache is trusted) -------------------------
        if not skip_cache:
            for ns in ns_list:
                for ew in ew_list:
                    cached = self._section_cache.get((sect, twp, ns, rng, ew))
                    if cached:
                        self._cache_hits += 1
                        return cached

        # -- live RDS query ---------------------------------------------------
        self._cache_misses += 1
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
                        # Only warm cache for non-skip queries (county-verified)
                        if not skip_cache:
                            self._section_cache[(sect, twp, ns, rng, ew)] = row
                        return row
        return None

    # -- Pass-2 exhaustive resolver ------------------------------------------

    def resolve_exhaustive(self,
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
        Pass-2 resolver: exhaustive direction sweep for records that
        returned 'rds_miss' in Pass 1.

        Strategy
        --------
        When ns/ew is MISSING (None): build an ordered list of ALL valid
        directions for that county (statistical priors first, then remaining
        values), then iterate every NS×EW combination.

        When ns/ew is KNOWN: keep that direction; only widen the section
        adjacency search (±1 and ±2 instead of ±1 only).

        Search order
        ------------
        1. Cell-level (quadrant_label) — all NS×EW combos
        2. Section-level bbox          — all NS×EW combos
        3. Adjacent sections ±1, ±2   — all NS×EW combos

        Source codes produced:
          direction_inferred_p2_{NS}{EW}     — cell or section hit
          sect_adj_p2_{sect}_{NS}{EW}        — adjacent section hit
        """
        result = {
            "lat": None, "lon": None, "source": "rds_miss",
            "sec_minx": None, "sec_miny": None,
            "sec_maxx": None, "sec_maxy": None,
            "cell_corners": None, "rel_x": None, "rel_y": None,
            "flags": [],
        }

        try:
            sect  = parse_section(section)
            twp   = int(float(str(township_num))) if township_num is not None else None
            rng   = int(float(str(range_num)))    if range_num    is not None else None
            row_i = int(dot_row)
            col_i = int(dot_col)
        except (TypeError, ValueError):
            result["source"] = "parse_failed"
            return result

        if not (sect and twp and rng):
            result["flags"].append(
                f"p2_missing_str:sect={sect},twp={twp},rng={rng}"
            )
            return result
        if not (1 <= row_i <= GRID_SIZE and 1 <= col_i <= GRID_SIZE):
            result["source"] = "parse_failed"
            return result

        eff_row, eff_col, eff_quad = self._resolve_quadrant(
            quadrant_label, unet_nw, row_i, col_i, result["flags"]
        )

        # Build ordered direction lists: stats → geographic prior → exhaustive
        county_clean = _clean_county_key(county)
        stats = self._county_direction_stats.get(county_clean) or {}

        if ns in ("N", "S"):
            ns_order = [ns]   # direction known — stay with it in P2
        else:
            ns_order = list(stats.get("ns_order") or _oklahoma_ns_prior(county_clean))
            for v in ("N", "S"):        # guarantee exhaustive coverage
                if v not in ns_order:
                    ns_order.append(v)

        if ew in ("E", "W"):
            ew_order = [ew]   # direction known
        else:
            ew_order = list(stats.get("ew_order") or _oklahoma_ew_prior(county_clean))
            for v in ("W", "E"):
                if v not in ew_order:
                    ew_order.append(v)

        county_pats = _county_variants(county)   # county-filtered patterns

        # ── 1. Cell-level sweep (needs quadrant_label, strict county only) ─────
        # strict_county=True prevents _find_cell_by_quad from falling back to
        # a no-county query — which would accept cells where county_name=NULL
        # and produce geographically incorrect coordinates when the section
        # numbers come from a different part of the state.
        if eff_quad:
            for ns_try in ns_order:
                for ew_try in ew_order:
                    ok, _ = validate_oklahoma_plss(sect, twp, ns_try, rng, ew_try)
                    if not ok:
                        continue
                    cell_bbox = self._find_cell_by_quad(
                        sect, twp, [ns_try], rng, [ew_try], eff_quad, county,
                        strict_county=True,
                    )
                    if cell_bbox:
                        corners = cell_corners_from_bbox(*cell_bbox)
                        rel_x, rel_y = dot_relative_in_cell(
                            x_norm, y_norm, eff_row, eff_col
                        )
                        src  = f"direction_inferred_p2_{ns_try}{ew_try}"
                        flag = f"p2_inferred:{ns_try}{ew_try}"
                        res = self._finalise(
                            dict(result), corners, cell_bbox, rel_x, rel_y,
                            src, x_norm, y_norm,
                        )
                        if res["lat"] is not None:
                            res["flags"].insert(0, flag)
                            return res

        # ── 2. Section-level sweep (exact section, live county query) ────────
        # skip_cache=True forces live RDS query (county-filtered).
        # The prefetch cache was built without county validation, so cached
        # entries for the same (sect,twp,rng) numbers in a different county
        # would produce geographically wrong coordinates.
        for ns_try in ns_order:
            for ew_try in ew_order:
                ok, _ = validate_oklahoma_plss(sect, twp, ns_try, rng, ew_try)
                if not ok:
                    continue
                bounds = self._find_section_bounds(
                    sect, twp, [ns_try], rng, [ew_try],
                    county_pats, skip_cache=True,
                )
                if bounds:
                    corners = compute_cell_corners(*bounds, eff_row, eff_col)
                    rel_x, rel_y = dot_relative_in_cell(
                        x_norm, y_norm, eff_row, eff_col
                    )
                    src  = f"direction_inferred_p2_{ns_try}{ew_try}"
                    flag = f"p2_inferred:{ns_try}{ew_try}"
                    res = self._finalise(
                        dict(result), corners, bounds, rel_x, rel_y,
                        src, x_norm, y_norm,
                    )
                    if res["lat"] is not None:
                        res["flags"].insert(0, flag)
                        return res

        # ── 3. Adjacent section sweep (±1, ±2 — live county query) ───────────
        for ns_try in ns_order:
            for ew_try in ew_order:
                ok, _ = validate_oklahoma_plss(sect, twp, ns_try, rng, ew_try)
                if not ok:
                    continue
                for delta in (-1, 1, -2, 2):
                    adj = sect + delta
                    if not (OK_SECTION_MIN <= adj <= OK_SECTION_MAX):
                        continue
                    bounds = self._find_section_bounds(
                        adj, twp, [ns_try], rng, [ew_try],
                        county_pats, skip_cache=True,
                    )
                    if bounds:
                        corners = compute_cell_corners(
                            *bounds, eff_row, eff_col
                        )
                        rel_x, rel_y = dot_relative_in_cell(
                            x_norm, y_norm, eff_row, eff_col
                        )
                        src  = f"sect_adj_p2_{adj}_{ns_try}{ew_try}"
                        flag = f"p2_sect_adj:{adj},{ns_try}{ew_try}"
                        res = self._finalise(
                            dict(result), corners, bounds, rel_x, rel_y,
                            src, x_norm, y_norm,
                        )
                        if res["lat"] is not None:
                            res["flags"].insert(0, flag)
                            return res

        return result

    # -- Pass-3: infer missing PLSS field ------------------------------------

    def resolve_missing_field(self,
                               section,
                               township_num,  ns: str | None,
                               range_num,     ew: str | None,
                               county: str,
                               dot_row, dot_col,
                               x_norm=None, y_norm=None,
                               quadrant_label: str | None = None,
                               unet_nw: str | None = None,
                               ) -> dict:
        """
        Pass-3: infer the missing PLSS coordinate from RDS.

        Called only for records where exactly ONE of township_num or range_num
        is None (the other is present and valid).  Queries the RDS with the
        known PLSS fields + county to find uniquely-matching values.

        Decision rule (conservative):
          • With county filter:   use the result if exactly 1 (val, dir) row is
                                  returned *or* the top row has ≥ 4× the count
                                  of the second row (dominant match).
          • Without county filter (fallback, only when county is blank):
                                  use only when exactly 1 row is returned.

        On success: delegates to resolve() with the inferred field filled in.
        Source codes produced:
          p3_twp_{twp}{ns}   — township was inferred
          p3_rng_{rng}{ew}   — range was inferred

        Returns a normal resolve() result dict, or a stub dict with
        source='p3_ambiguous' / 'p3_nomatch' / 'p3_invalid_input'.
        """
        _STUB = lambda src, flags=(): {
            "lat": None, "lon": None, "source": src,
            "sec_minx": None, "sec_miny": None,
            "sec_maxx": None, "sec_maxy": None,
            "cell_corners": None, "rel_x": None, "rel_y": None,
            "flags": list(flags),
        }

        try:
            sect  = parse_section(section)
            twp   = int(float(str(township_num))) if township_num is not None else None
            rng   = int(float(str(range_num)))    if range_num    is not None else None
        except (TypeError, ValueError):
            return _STUB("p3_invalid_input", ["p3_parse_error"])

        # Need exactly one of twp / rng to be None, the other valid
        if twp is None and rng is None:
            return _STUB("p3_invalid_input", ["p3_both_missing"])
        if twp is not None and rng is not None:
            return _STUB("p3_invalid_input", ["p3_nothing_missing"])
        if not sect:
            return _STUB("p3_invalid_input", ["p3_section_missing"])

        county_clean = _clean_county_key(county)
        stats = self._county_direction_stats.get(county_clean) or {}

        # ── Case A: Township is missing, Range is known ───────────────────
        if twp is None:
            # Infer east_west from county stats.
            # Apply county direction override: if county is 100% one EW direction
            # but OCR gave the other (e.g. "14W" in an all-East county), try the
            # county-preferred direction first so we don't miss matches.
            ew_order: list[str]
            if ew in ("E", "W"):
                county_ew = stats.get("ew_order") or []
                if len(county_ew) == 1 and county_ew[0] != ew:
                    ew_order = [county_ew[0], ew]   # override: county dir first
                else:
                    ew_order = [ew]
            else:
                ew_order = (stats.get("ew_order") or
                            _oklahoma_ew_prior(county_clean) or
                            list(_EW_TRY_ORDER))

            county_pats = _county_variants(county)
            inferred_twp = inferred_ns = None

            for ew_try in ew_order:
                # Try county-filtered first, then bare if county is empty
                patterns = county_pats if county else ["%"]
                for pat in patterns:
                    with_county = pat != "%"
                    params = {"sect_num": sect, "range_num": rng, "ew": ew_try}
                    if with_county:
                        params["county"] = pat
                    sql = _infer_sql(_SQL_INFER_TOWNSHIP, with_county)
                    try:
                        cur = self._cursor()
                        cur.execute(sql, params)
                        rows = cur.fetchall()
                    except Exception as exc:
                        log.warning("p3 infer_township query error: %s", exc)
                        self._conn = None
                        rows = []

                    if not rows:
                        continue

                    # Decision: unique or dominant match
                    if len(rows) == 1:
                        inferred_twp, inferred_ns, _ = rows[0]
                    elif with_county and len(rows) >= 2:
                        top_cnt, sec_cnt = rows[0][2], rows[1][2]
                        if top_cnt >= 4 * sec_cnt:   # dominant (≥4×)
                            inferred_twp, inferred_ns, _ = rows[0]
                    if inferred_twp is not None:
                        break
                if inferred_twp is not None:
                    break

            if inferred_twp is None:
                return _STUB("p3_nomatch", [f"p3_twp_nomatch:sect={sect},rng={rng}{ew or ''}"])

            # Validate inferred township against PLSS bounds
            ok, reason = validate_oklahoma_plss(
                sect, inferred_twp, inferred_ns or "N", rng,
                ew_order[0] if ew is None else ew
            )
            if not ok:
                return _STUB("p3_invalid_input", [f"p3_inferred_invalid:{reason}"])

            log.debug(
                "p3 inferred township: sect=%s rng=%s%s county=%s -> %s%s",
                sect, rng, ew_order[0], county, inferred_twp, inferred_ns
            )
            res = self.resolve(
                section=section,
                township_num=inferred_twp, ns=inferred_ns,
                range_num=rng,             ew=(ew or ew_order[0]),
                county=county,
                dot_row=dot_row, dot_col=dot_col,
                x_norm=x_norm, y_norm=y_norm,
                quadrant_label=quadrant_label,
                unet_nw=unet_nw,
            )
            # Tag with Pass-3 provenance
            ew_used = ew or ew_order[0]
            res["flags"].insert(0, f"p3_township_inferred:{inferred_twp}{inferred_ns or ''}")
            if res["source"] not in ("bounds_invalid", "rds_miss", "parse_failed"):
                res["source"] = f"p3_twp_{inferred_twp}{inferred_ns or ''}"
            return res

        # ── Case B: Range is missing, Township is known ───────────────────
        else:
            # Infer north_south from county stats.
            # Apply county direction override (same logic as Case A for NS).
            ns_order: list[str]
            if ns in ("N", "S"):
                county_ns = stats.get("ns_order") or []
                if len(county_ns) == 1 and county_ns[0] != ns:
                    ns_order = [county_ns[0], ns]   # override: county dir first
                else:
                    ns_order = [ns]
            else:
                ns_order = (stats.get("ns_order") or
                            _oklahoma_ns_prior(county_clean) or
                            list(_NS_TRY_ORDER))

            county_pats = _county_variants(county)
            inferred_rng = inferred_ew = None

            for ns_try in ns_order:
                patterns = county_pats if county else ["%"]
                for pat in patterns:
                    with_county = pat != "%"
                    params = {"sect_num": sect, "township": twp, "ns": ns_try}
                    if with_county:
                        params["county"] = pat
                    sql = _infer_sql(_SQL_INFER_RANGE, with_county)
                    try:
                        cur = self._cursor()
                        cur.execute(sql, params)
                        rows = cur.fetchall()
                    except Exception as exc:
                        log.warning("p3 infer_range query error: %s", exc)
                        self._conn = None
                        rows = []

                    if not rows:
                        continue

                    if len(rows) == 1:
                        inferred_rng, inferred_ew, _ = rows[0]
                    elif with_county and len(rows) >= 2:
                        top_cnt, sec_cnt = rows[0][2], rows[1][2]
                        if top_cnt >= 4 * sec_cnt:
                            inferred_rng, inferred_ew, _ = rows[0]
                    if inferred_rng is not None:
                        break
                if inferred_rng is not None:
                    break

            if inferred_rng is None:
                return _STUB("p3_nomatch", [f"p3_rng_nomatch:sect={sect},twp={twp}{ns or ''}"])

            ok, reason = validate_oklahoma_plss(
                sect, twp, ns_order[0] if ns is None else ns,
                inferred_rng, inferred_ew or "W"
            )
            if not ok:
                return _STUB("p3_invalid_input", [f"p3_inferred_invalid:{reason}"])

            log.debug(
                "p3 inferred range: sect=%s twp=%s%s county=%s -> %s%s",
                sect, twp, ns_order[0], county, inferred_rng, inferred_ew
            )
            res = self.resolve(
                section=section,
                township_num=twp,      ns=(ns or ns_order[0]),
                range_num=inferred_rng, ew=inferred_ew,
                county=county,
                dot_row=dot_row, dot_col=dot_col,
                x_norm=x_norm, y_norm=y_norm,
                quadrant_label=quadrant_label,
                unet_nw=unet_nw,
            )
            res["flags"].insert(0, f"p3_range_inferred:{inferred_rng}{inferred_ew or ''}")
            if res["source"] not in ("bounds_invalid", "rds_miss", "parse_failed"):
                res["source"] = f"p3_rng_{inferred_rng}{inferred_ew or ''}"
            return res

    # -- Pass-5: section centroid (no dot position required) -----------------

    def resolve_section_centroid(self,
                                  section,
                                  township_num, ns: str | None,
                                  range_num,    ew: str | None,
                                  county: str,
                                  ) -> dict:
        """
        Pass-5: Return the geographic centre of the PLSS section as an
        approximate well location.

        No dot row/col required — uses only section/township/range/county.
        Precision is approximately ½ mile (section is 1 mile square).

        Uses the same direction-inference and strategy-building as resolve()
        so county direction overrides, county constraints, and the pre-warmed
        section cache all apply normally.

        Source code: 'section_centroid'
        Flags:       'approx_section_center' always present
                     'p5_ns_inferred:{dir}' when NS was not explicit in OCR
                     'p5_ew_inferred:{dir}' when EW was not explicit in OCR
        """
        result = {
            "lat": None, "lon": None, "source": "rds_miss",
            "sec_minx": None, "sec_miny": None,
            "sec_maxx": None, "sec_maxy": None,
            "cell_corners": None, "rel_x": 0.5, "rel_y": 0.5,
            "flags": ["approx_section_center"],
        }

        try:
            sect = parse_section(section)
            twp  = int(float(str(township_num))) if township_num is not None else None
            rng  = int(float(str(range_num)))    if range_num    is not None else None
        except (TypeError, ValueError):
            result["source"] = "parse_failed"
            return result

        if not (sect and twp and rng):
            result["source"] = "bounds_invalid"
            result["flags"].append(
                f"p5_missing:sect={sect},twp={twp},rng={rng}"
            )
            return result

        # Effective direction lists (same logic as resolve())
        ns_list, ew_list = self._effective_directions(ns, ew, county, twp, rng)

        # Validate with effective primary directions
        valid, reason = validate_oklahoma_plss(
            sect, twp, ns_list[0] if ns_list else ns,
            rng,  ew_list[0] if ew_list else ew,
        )
        if not valid:
            result["source"] = "bounds_invalid"
            result["flags"].append(reason)
            return result

        # Use the same multi-strategy lookup as resolve()
        strategies = self._build_strategies(ns_list, ew_list, ns, ew, county, sect, rng)

        bounds = None
        ns_used = ew_used = None
        for ns_s, ew_s, county_pats, _label in strategies:
            bounds = self._find_section_bounds(sect, twp, ns_s, rng, ew_s, county_pats)
            if bounds:
                ns_used = ns_s[0] if ns_s else (ns_list[0] if ns_list else ns)
                ew_used = ew_s[0] if ew_s else (ew_list[0] if ew_list else ew)
                break

        if bounds is None:
            return result   # rds_miss

        minx, miny, maxx, maxy = bounds
        lat = (miny + maxy) / 2.0
        lon = (minx + maxx) / 2.0

        if not (-103.1 <= lon <= -94.4 and 33.6 <= lat <= 37.1):
            result["source"] = "geometry_error"
            result["flags"].append(
                f"out_of_oklahoma:lat={lat:.3f},lon={lon:.3f}"
            )
            return result

        # Annotate when direction was inferred (not explicit in OCR)
        if ns_used and ns not in ("N", "S"):
            result["flags"].append(f"p5_ns_inferred:{ns_used}")
        if ew_used and ew not in ("E", "W"):
            result["flags"].append(f"p5_ew_inferred:{ew_used}")

        result.update({
            "lat":      round(lat, 7),
            "lon":      round(lon, 7),
            "source":   "section_centroid",
            "sec_minx": minx, "sec_miny": miny,
            "sec_maxx": maxx, "sec_maxy": maxy,
        })
        return result

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

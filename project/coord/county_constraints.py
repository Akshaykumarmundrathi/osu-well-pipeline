"""
County→PLSS bounds lookup.

Queries the plss_grid table once to build a mapping:
  county_name → {
      min_twp, max_twp, ns_values,    # valid township values for this county
      min_rng, max_rng, ew_values,    # valid range values
  }

Used by PLSSResolver to:
  1. Infer missing N/S or E/W direction when township/range number is known
  2. Quickly rule out impossible county+direction combinations before DB
  3. Provide fallback (ns, ew) candidates ordered by frequency in that county

Saved to JSON (county_constraints.json) alongside processing_status.csv so
it's built once per installation, not once per record.

All connections are closed before return.  Zero idle charges.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_CONSTRAINTS_FILE = "county_constraints.json"


def _query_constraints(conn) -> dict:
    """
    Single aggregate query.  Returns raw dict keyed by county_name.
    """
    sql = """
        SELECT
            county_name,
            MIN(township)                                        AS min_twp,
            MAX(township)                                        AS max_twp,
            array_agg(DISTINCT north_south ORDER BY north_south) AS ns_vals,
            MIN("range")                                         AS min_rng,
            MAX("range")                                         AS max_rng,
            array_agg(DISTINCT east_west   ORDER BY east_west)   AS ew_vals,
            COUNT(DISTINCT sect_num)                             AS n_sections
        FROM plss_grid
        WHERE county_name IS NOT NULL
          AND sect_num BETWEEN 1 AND 36
          AND township BETWEEN 1 AND 29
          AND "range"  BETWEEN 1 AND 28
        GROUP BY county_name
        ORDER BY county_name;
    """
    import psycopg2.extras
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()

    out: dict = {}
    for r in rows:
        name = r["county_name"].strip()
        if not name:
            continue
        out[name] = {
            "min_twp":    int(r["min_twp"]),
            "max_twp":    int(r["max_twp"]),
            "ns_values":  [x for x in (r["ns_vals"] or []) if x],
            "min_rng":    int(r["min_rng"]),
            "max_rng":    int(r["max_rng"]),
            "ew_values":  [x for x in (r["ew_vals"] or []) if x],
            "n_sections": int(r["n_sections"]),
        }
    return out


def build_and_save(output_dir: Path,
                   host, port, dbname, user, password) -> dict:
    """
    Connect to RDS, build constraints, save JSON, close connection.
    Returns the constraints dict (empty on error).
    """
    import psycopg2
    conn = None
    try:
        conn = psycopg2.connect(
            host=host, port=port, dbname=dbname,
            user=user, password=password,
            sslmode="require", connect_timeout=20,
        )
        conn.set_session(readonly=True, autocommit=True)
        constraints = _query_constraints(conn)
        _save(output_dir, constraints)
        log.info("county_constraints: built %d counties, saved to %s",
                 len(constraints), output_dir / _CONSTRAINTS_FILE)
        return constraints
    except Exception as exc:
        log.warning("county_constraints: build failed: %s", exc)
        return {}
    finally:
        if conn and not conn.closed:
            try:
                conn.close()
            except Exception:
                pass


def _save(output_dir: Path, constraints: dict):
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / _CONSTRAINTS_FILE
    tmp  = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(constraints, indent=2), encoding="utf-8")
    tmp.replace(path)


def load(output_dir: Path) -> dict:
    """Load cached constraints JSON.  Returns {} if not found."""
    path = output_dir / _CONSTRAINTS_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("county_constraints: load failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Query helpers used by PLSSResolver
# ---------------------------------------------------------------------------

def infer_directions(constraints: dict,
                     county: str,
                     township: int | None,
                     range_num: int | None,
                     ns_given: str | None,
                     ew_given: str | None,
                     ) -> tuple[list[str], list[str]]:
    """
    Given partial PLSS info and county constraints, return ordered lists of
    NS and EW candidates to try.

    Returns (ns_candidates, ew_candidates), each ordered best-first.
    - If direction already known, returns [known_direction].
    - If county found and only one direction exists → returns that.
    - Otherwise returns all valid directions for county, falling back to ['N','S']
      / ['W','E'] ordered by Oklahoma frequency.
    """
    # Defaults (Oklahoma-wide frequency order)
    default_ns = ["N", "S"]
    default_ew = ["W", "E"]

    if ns_given in ("N", "S"):
        ns_out = [ns_given]
    else:
        ns_out = None

    if ew_given in ("E", "W"):
        ew_out = [ew_given]
    else:
        ew_out = None

    # Try to find county in constraints (case-insensitive partial match)
    info = _find_county(constraints, county)

    if info:
        if ns_out is None:
            ns_vals = info.get("ns_values", [])
            if len(ns_vals) == 1:
                ns_out = ns_vals          # only one direction exists → definitive
            elif ns_vals:
                # Filter by township number range if available
                if township is not None:
                    if township <= info["max_twp"] and "N" in ns_vals:
                        ns_out = ["N"] if township <= info["max_twp"] else ns_vals
                    else:
                        ns_out = ns_vals
                else:
                    ns_out = ns_vals      # all options for this county
            else:
                ns_out = default_ns

        if ew_out is None:
            ew_vals = info.get("ew_values", [])
            if len(ew_vals) == 1:
                ew_out = ew_vals
            elif ew_vals:
                if range_num is not None:
                    ew_out = [e for e in ew_vals
                              if range_num <= (info["max_rng"] if e == "W" else 26)]
                    if not ew_out:
                        ew_out = ew_vals
                else:
                    ew_out = ew_vals
            else:
                ew_out = default_ew
    else:
        if ns_out is None:
            ns_out = default_ns
        if ew_out is None:
            ew_out = default_ew

    return ns_out, ew_out


def _find_county(constraints: dict, raw: str) -> dict | None:
    """Case-insensitive lookup; tries exact, then suffix-stripped, then first word."""
    if not raw:
        return None
    s = raw.strip()
    # 1. Exact
    if s in constraints:
        return constraints[s]
    # 2. Case-insensitive
    sl = s.lower()
    for k, v in constraints.items():
        if k.lower() == sl:
            return v
    # 3. Strip "County" suffix
    import re
    base = re.sub(r"\s+county\.?$", "", s, flags=re.I).strip()
    for k, v in constraints.items():
        if k.lower().startswith(base.lower()):
            return v
    # 4. First word
    first = s.split()[0] if s.split() else ""
    if first:
        for k, v in constraints.items():
            if k.lower().startswith(first.lower()):
                return v
    return None


def validate_with_constraints(constraints: dict,
                               county: str,
                               township: int | None,
                               ns: str | None,
                               range_num: int | None,
                               ew: str | None,
                               ) -> tuple[bool, str]:
    """
    Check if the township/range values are plausible for the given county.
    Returns (is_plausible, reason).

    Does NOT replace validate_oklahoma_plss() — only adds county-level check.
    """
    info = _find_county(constraints, county)
    if not info:
        return True, "county_unknown"

    if township is not None and ns == "N":
        if township > info["max_twp"] and "N" in info["ns_values"]:
            return False, (f"township {township}N exceeds county max "
                           f"{info['max_twp']} for {county}")

    if ns is not None and ns not in info.get("ns_values", ["N", "S"]):
        return False, f"direction {ns} not used in {county}"

    if ew is not None and ew not in info.get("ew_values", ["E", "W"]):
        return False, f"direction {ew} not used in {county}"

    return True, "ok"

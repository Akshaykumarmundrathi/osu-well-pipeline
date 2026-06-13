"""
gh_apply_corrections.py -- GitHub Actions runner for map verify-panel fixes.

Self-contained: reads open [CORRECTION]/[VERIFIED-CORRECT] issues, patches
docs/data/well_locations.json, re-resolves coordinates via the PLSS RDS in
batch when STR/quadrant changed (secrets permitting), logs every change to
docs/data/corrections_log.csv, comments + closes the issues.
The workflow step commits whatever this script changes.
"""
import csv
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO   = os.environ.get("GITHUB_REPOSITORY", "Akshaykumarmundrathi/osu-well-pipeline")
TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GEO    = Path("docs/data/well_locations.json")
LOGCSV = Path("docs/data/corrections_log.csv")

_FIELD_RE = re.compile(
    r"^- (section|township|range|county|quadrant): `([^`]*)` -> `([^`]*)`", re.M)
_COORD_RE = re.compile(r"^- (lat|lon): `([^`]*)` -> `([^`]*)`", re.M)
_STEM_RE = re.compile(r"\*\*Well:\*\* `([^`]+)`")
FIELD_TO_PROP = {"section": "section", "township": "township",
                 "range": "range", "county": "county", "quadrant": "quadrant"}


def _to_coord(v: str) -> float | None:
    try:
        f = float(str(v).strip())
        return f if -180.0 <= f <= 180.0 else None
    except (TypeError, ValueError):
        return None


def api(path, method="GET", payload=None):
    req = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {TOKEN}",
                 "User-Agent": "osu-corrections-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "null")


def parse_dir(v: str, default: str) -> tuple[int, str] | None:
    m = re.match(r"^\s*(\d{1,3})\s*([NSEW]?)\s*$", (v or "").upper())
    if not m:
        return None
    return int(m.group(1)), (m.group(2) or default)


def resolve_rds(section, township, rng, quadrant, county) -> tuple[float, float, str] | None:
    """Centroid of the quadrant cell (preferred) or the section bbox."""
    host = os.environ.get("RDS_HOST", "")
    if not host:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=host, port=int(os.environ.get("RDS_PORT") or 5432),
            dbname=os.environ["RDS_DBNAME"], user=os.environ["RDS_USER"],
            password=os.environ["RDS_PASSWORD"], connect_timeout=20)
    except Exception as exc:
        print(f"RDS unavailable: {exc}")
        return None
    try:
        sec = int(re.sub(r"\D", "", section or "") or 0)
        twp = parse_dir(township, "N")
        rgn = parse_dir(rng, "W")
        if not (1 <= sec <= 36) or not twp or not rgn:
            return None
        cur = conn.cursor()
        params = {"s": sec, "t": twp[0], "ns": twp[1],
                  "r": rgn[0], "ew": rgn[1]}
        if quadrant:
            cur.execute(
                'SELECT minx,miny,maxx,maxy FROM plss_grid WHERE sect_num=%(s)s '
                'AND township=%(t)s AND north_south=%(ns)s AND "range"=%(r)s '
                'AND east_west=%(ew)s AND quadrant_label=%(q)s LIMIT 1',
                {**params, "q": quadrant})
            row = cur.fetchone()
            if row:
                return ((row[1] + row[3]) / 2, (row[0] + row[2]) / 2,
                        "corrected_quadrant")
        cur.execute(
            'SELECT MIN(minx),MIN(miny),MAX(maxx),MAX(maxy) FROM plss_grid '
            'WHERE sect_num=%(s)s AND township=%(t)s AND north_south=%(ns)s '
            'AND "range"=%(r)s AND east_west=%(ew)s', params)
        row = cur.fetchone()
        if row and row[0] is not None:
            return ((row[1] + row[3]) / 2, (row[0] + row[2]) / 2,
                    "corrected_centroid")
        return None
    finally:
        conn.close()


def main() -> None:
    issues = [i for i in api(f"/repos/{REPO}/issues?state=open&per_page=100")
              if "pull_request" not in i
              and (i["title"].startswith("[CORRECTION]")
                   or i["title"].startswith("[VERIFIED-CORRECT]"))]
    if not issues:
        print("no correction issues")
        return

    geo = json.loads(GEO.read_text(encoding="utf-8"))
    feats = {f["properties"].get("pdf_stem", ""): f for f in geo["features"]}
    log_rows, handled = [], []

    for issue in issues:
        body = issue.get("body") or ""
        m = _STEM_RE.search(body)
        if not m:
            continue
        stem = m.group(1)
        changes = {f: new for f, old, new in _FIELD_RE.findall(body)}
        coords = {f: new for f, old, new in _COORD_RE.findall(body)}
        feat = feats.get(stem)
        verified = (issue["title"].startswith("[VERIFIED-CORRECT]")
                    or (not changes and not coords))
        note = ""
        if feat is None:
            note = "well not found in current dataset (may be unmapped) — logged only"
        elif not verified:
            p = feat["properties"]
            str_changed = False
            for f, newv in changes.items():
                prop = FIELD_TO_PROP[f]
                old = str(p.get(prop, ""))
                if old != newv:
                    log_rows.append({"stem": stem, "field": prop, "old": old,
                                     "new": newv, "issue": issue["number"]})
                    p[prop] = newv
                    if f != "county":
                        str_changed = True
            # Direct coordinate override wins over PLSS re-resolution: the user
            # read the lat/lon off the PDF, so trust it verbatim.
            lat_v = _to_coord(coords.get("lat")) if "lat" in coords else None
            lon_v = _to_coord(coords.get("lon")) if "lon" in coords else None
            if lat_v is not None or lon_v is not None:
                lat = lat_v if lat_v is not None else _to_coord(p.get("lat"))
                lon = lon_v if lon_v is not None else _to_coord(p.get("lon"))
                if lat is not None and lon is not None:
                    for fld, ov, nv in (("lat", p.get("lat"), lat),
                                        ("lon", p.get("lon"), lon)):
                        log_rows.append({"stem": stem, "field": fld,
                                         "old": str(ov), "new": str(nv),
                                         "issue": issue["number"]})
                    feat["geometry"]["coordinates"] = [round(lon, 7), round(lat, 7)]
                    p["lat"], p["lon"] = round(lat, 7), round(lon, 7)
                    p["resolution"] = "human_latlong"
                    note = "coordinates set from direct lat/lon override (human_latlong)"
                else:
                    note = "lat/lon override incomplete — ignored"
            elif str_changed:
                res = resolve_rds(p.get("section"), p.get("township"),
                                  p.get("range"), p.get("quadrant"),
                                  p.get("county"))
                if res:
                    lat, lon, how = res
                    feat["geometry"]["coordinates"] = [round(lon, 7), round(lat, 7)]
                    p["lat"], p["lon"] = round(lat, 7), round(lon, 7)
                    p["resolution"] = how
                    note = f"fields + coordinates updated ({how})"
                else:
                    p["resolution"] = (p.get("resolution") or "") + "+needs-coord-rerun"
                    note = "fields updated; coordinates flagged for re-run"
            else:
                note = "fields updated"
        else:
            p = feat["properties"]
            p["verified"] = "human"
            note = "marked human-verified"
        handled.append((issue["number"], note))

    GEO.write_text(json.dumps(geo, separators=(",", ":")), encoding="utf-8")
    new_log = not LOGCSV.exists()
    with LOGCSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["stem", "field", "old", "new", "issue"])
        if new_log:
            w.writeheader()
        w.writerows(log_rows)

    for num, note in handled:
        try:
            api(f"/repos/{REPO}/issues/{num}/comments", "POST",
                {"body": f"✅ {note}. The live map updates within minutes. Thank you!"})
            api(f"/repos/{REPO}/issues/{num}", "PATCH", {"state": "closed"})
        except Exception as exc:
            print(f"close #{num}: {exc}")
    print(f"{len(log_rows)} field fixes, {len(handled)} issues handled")


if __name__ == "__main__":
    main()

"""latlong_upgrade.py -- upgrade modern_text_section_centroid wells to the EXACT
printed lat/long when the 1002A form carries it (free; reads the PDF text layer).

Parses DMS like:  Latitude (if known) 34 23' 52.8 N   Longitude 95 44' 01.6 W
Converts to decimal degrees, validates within Oklahoma, and (with --apply)
replaces resolved_lat/lon + sets resolution_source=printed_latlong.

Usage: python latlong_upgrade.py [--limit N] [--apply]
"""
import argparse, csv, os, re, sys
from pathlib import Path
csv.field_size_limit(2_000_000)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import fitz
OUT = Path(r"D:\project_outputs")

# OK bounds
LAT0,LAT1 = 33.5,37.1
LON0,LON1 = -103.1,-94.4

# Decimal (modern completion reports): "Latitude: 36.958904 Longitude: -98.256947"
_LAT_DEC = re.compile(r"Latitude\s*[:=]?\s*(-?\d{2}\.\d{3,})", re.I)
_LON_DEC = re.compile(r"Longitude\s*[:=]?\s*(-?\d{2,3}\.\d{3,})", re.I)
# DMS fallback (older forms): "Latitude (if known) 34 23' 52.8 N ... 95 44' 01.6 W"
_LAT_DMS = re.compile(r"Latitude[a-z ()]*?\b(\d{2})\s*[°:` ]\s*(\d{1,2})\s*['’:` ]\s*([\d.]+)\s*[\"”]?\s*([NS])", re.I)
_LON_DMS = re.compile(r"Longitude[a-z ()]*?\b(\d{2,3})\s*[°:` ]\s*(\d{1,2})\s*['’:` ]\s*([\d.]+)\s*[\"”]?\s*([EW])", re.I)


def _dms(d, m, s, hemi):
    try:
        v = int(d) + int(m)/60.0 + float(s)/3600.0
    except ValueError:
        return None
    return round(-v if hemi.upper() in ("S", "W") else v, 7)


def _ok(lat, lon):
    if lat is None or lon is None:
        return None
    if lon > 0:           # some forms drop the minus on longitude
        lon = -lon
    return (lat, lon) if (LAT0 <= lat <= LAT1 and LON0 <= lon <= LON1) else None


def find_latlon(txt):
    # decimal first (modern), then DMS
    ml, mo = _LAT_DEC.search(txt), _LON_DEC.search(txt)
    if ml and mo:
        r = _ok(round(float(ml.group(1)), 7), round(float(mo.group(1)), 7))
        if r:
            return r
    ml, mo = _LAT_DMS.search(txt), _LON_DMS.search(txt)
    if ml and mo:
        return _ok(_dms(*ml.groups()), _dms(*mo.groups()))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    path = {}
    with (OUT/"dataset_index.csv").open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            path[r.get("pdf_stem","")] = r.get("pdf_path","")

    cols, rows = None, []
    with (OUT/"dot_coordinates.csv").open(newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f); cols = rd.fieldnames; rows = list(rd)
    targets = [r for r in rows if r.get("resolution_source") == "modern_text_section_centroid"]
    if a.limit:
        targets = targets[:a.limit]
    print(f"{len(targets)} modern_text_section_centroid wells to scan")

    found = tried = 0; updates = {}
    for i, r in enumerate(targets):
        p = path.get(r["pdf_stem"], "")
        if not p or not os.path.exists(p):
            continue
        tried += 1
        try:
            d = fitz.open(p); txt = "".join(pg.get_text() for pg in d); d.close()
        except Exception:
            continue
        ll = find_latlon(txt)
        if ll:
            found += 1
            updates[r["pdf_stem"]] = ll
        if (i+1) % 500 == 0:
            print(f"  {i+1}/{len(targets)} | printed lat/long found {found}", flush=True)
    print(f"DONE: scanned {tried}, printed lat/long found {found} ({found*100//max(tried,1)}%)")

    if a.apply and updates:
        import shutil
        shutil.copy2(OUT/"dot_coordinates.csv", OUT/"dot_coordinates.csv.prelatlon_bak")
        ch = 0
        for r in rows:
            u = updates.get(r["pdf_stem"])
            if u:
                r["resolved_lat"], r["resolved_lon"] = u
                r["resolution_source"] = "printed_latlong"
                ch += 1
        tmp = OUT/"dot_coordinates.csv.t"
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
        os.replace(tmp, OUT/"dot_coordinates.csv")
        print(f"APPLIED: upgraded {ch} wells to exact printed lat/long (backup .prelatlon_bak)")
    elif updates:
        # sample for review
        for s,(la,lo) in list(updates.items())[:6]:
            print(f"  sample {s[:30]} -> {la}, {lo}")


if __name__ == "__main__":
    main()

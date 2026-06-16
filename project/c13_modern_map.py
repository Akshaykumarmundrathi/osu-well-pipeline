"""c13_modern_map.py -- ADDITIVE text-path mapper for digital-native records.

C12-C13 (and some C11) are typed completion reports with no grid/dot, so the
grid pipeline maps few. This reads the PDF text layer and recovers the location
from typed fields, resolves coordinates via the PLSS RDS, and writes
dot_coordinates-schema rows to a SEPARATE file for review/union. It NEVER
edits the grid pipeline or overwrites existing coordinates.

Signals (in priority order):
  county : "API No.: 35CCC..." -> code -> sorted_counties[(code-1)//2]
           (authoritative; verified 35-003=Alfalfa, 35-011=Blaine)
  STR    : a "Location:" line  -> county SEC TWP[NS] RGE[EW] [quadrant], OR
           the well name's "<sec>-<twp>[NS]-<rng>[EW]" encoding
  coords : plss_resolver -> section/quadrant centroid (same as enrichment)

Output: D:\\project_outputs\\c13_modern_coords.csv  (resolution_source=modern_text)
Then union into dot_coordinates via safe path + monotonic map rebuild.

Usage: python c13_modern_map.py --index D:\\project_outputs_sample\\c13_index.csv [--limit N]
"""
import argparse, csv, os, re, sys
from pathlib import Path

csv.field_size_limit(2_000_000)
sys.path.insert(0, str(Path(__file__).parent))
import fitz
from config import COUNTY_LIST_CLEAN

OUT = Path(r"D:\project_outputs")
COUNTIES = sorted(set(COUNTY_LIST_CLEAN))

_API = re.compile(r"API\s*No\.?:?\s*35(\d{3})", re.I)
_LOCBLOCK = re.compile(
    r"\b([A-Z][A-Za-z]{2,15})\s+(\d{1,2})\s+(\d{1,3})\s*([NS])\s+(\d{1,3})\s*([EW])", re.I)
_NAMESTR = re.compile(r"\b(\d{1,2})\s*-\s*(\d{1,3})\s*([NS])\s*-\s*(\d{1,3})\s*([EW])\b", re.I)
_QUAD = re.compile(r"\b([NS][EW])[\s-]+([NS][EW])[\s-]+([NS][EW])\b", re.I)


def decode_county(code: int):
    i = (code - 1) // 2
    return COUNTIES[i] if 0 <= i < len(COUNTIES) else ""


def extract(txt: str, stem: str) -> dict:
    out = {"county": "", "section": "", "township": "", "range": "", "quadrant": ""}
    m = _API.search(txt)
    if m:
        out["county"] = decode_county(int(m.group(1)))
    lb = _LOCBLOCK.search(txt)
    if lb:
        cand = lb.group(1).lower()
        if not out["county"] and cand in set(COUNTIES):
            out["county"] = cand
        out["section"] = lb.group(2)
        out["township"] = f"{lb.group(3)}{lb.group(4).upper()}"
        out["range"] = f"{lb.group(5)}{lb.group(6).upper()}"
    if not out["section"]:
        nm = _NAMESTR.search(stem) or _NAMESTR.search(txt)
        if nm:
            out["section"] = nm.group(1)
            out["township"] = f"{nm.group(2)}{nm.group(3).upper()}"
            out["range"] = f"{nm.group(4)}{nm.group(5).upper()}"
    q = _QUAD.search(txt)
    if q:
        out["quadrant"] = "-".join(x.upper() for x in q.groups())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(OUT / "c13_modern_coords.csv"))
    a = ap.parse_args()

    # only attempt records the grid pipeline did NOT already map
    mapped = set()
    dc = OUT / "dot_coordinates.csv"
    if dc.exists():
        with dc.open(newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                if (r.get("resolved_lat") or "").strip():
                    mapped.add(r.get("pdf_stem", ""))

    rows = list(csv.DictReader(open(a.index, newline="", encoding="utf-8", errors="replace")))
    if a.limit:
        rows = rows[:a.limit]

    # lazy RDS
    try:
        from coord import plss_resolver
        conn = plss_resolver.get_connection()
    except Exception as exc:
        print(f"RDS unavailable ({exc}); will extract fields only, no coords")
        conn = None

    def resolve(sec, twp, rng, quad, county):
        if conn is None:
            return None
        try:
            s = int(re.sub(r"\D", "", sec or "") or 0)
            tm = re.match(r"(\d+)([NS])", twp or ""); rm = re.match(r"(\d+)([EW])", rng or "")
            if not (1 <= s <= 36) or not tm or not rm:
                return None
            cur = conn.cursor()
            p = {"s": s, "t": int(tm.group(1)), "ns": tm.group(2),
                 "r": int(rm.group(1)), "ew": rm.group(2)}
            if quad:
                cur.execute('SELECT minx,miny,maxx,maxy FROM plss_grid WHERE sect_num=%(s)s '
                            'AND township=%(t)s AND north_south=%(ns)s AND "range"=%(r)s '
                            'AND east_west=%(ew)s AND quadrant_label=%(q)s LIMIT 1',
                            {**p, "q": quad})
                row = cur.fetchone()
                if row:
                    return (row[1]+row[3])/2, (row[0]+row[2])/2, "modern_text_quadrant"
            cur.execute('SELECT MIN(minx),MIN(miny),MAX(maxx),MAX(maxy) FROM plss_grid '
                        'WHERE sect_num=%(s)s AND township=%(t)s AND north_south=%(ns)s '
                        'AND "range"=%(r)s AND east_west=%(ew)s', p)
            row = cur.fetchone()
            if row and row[0] is not None:
                return (row[1]+row[3])/2, (row[0]+row[2])/2, "modern_text_centroid"
        except Exception:
            return None
        return None

    fields = ["pdf_stem", "collection", "year", "month", "county_name",
              "section", "township", "range", "quadrant",
              "resolved_lat", "resolved_lon", "resolution_source", "needs_review"]
    n_ext = n_coord = 0
    with open(a.out, "w", newline="", encoding="utf-8") as fo:
        w = csv.DictWriter(fo, fieldnames=fields); w.writeheader()
        for i, r in enumerate(rows):
            stem = r.get("pdf_stem", "")
            if stem in mapped:
                continue
            p = r.get("pdf_path", "")
            if not p or not os.path.exists(p):
                continue
            try:
                d = fitz.open(p); txt = "".join(pg.get_text() for pg in d); d.close()
            except Exception:
                continue
            e = extract(txt, stem)
            if not (e["county"] or e["section"]):
                continue
            n_ext += 1
            res = resolve(e["section"], e["township"], e["range"], e["quadrant"], e["county"])
            row = {"pdf_stem": stem, "collection": r.get("collection", ""),
                   "year": r.get("year", ""), "month": r.get("month", ""),
                   "county_name": e["county"], "section": e["section"],
                   "township": e["township"], "range": e["range"],
                   "quadrant": e["quadrant"], "needs_review": "1"}
            if res:
                lat, lon, how = res
                row["resolved_lat"] = round(lat, 7); row["resolved_lon"] = round(lon, 7)
                row["resolution_source"] = how; n_coord += 1
            w.writerow(row)
            if (i + 1) % 500 == 0:
                print(f"  {i+1}/{len(rows)} scanned, {n_ext} extracted, {n_coord} resolved", flush=True)
    print(f"DONE: {n_ext} fields extracted, {n_coord} coordinate-resolved -> {a.out}")


if __name__ == "__main__":
    main()

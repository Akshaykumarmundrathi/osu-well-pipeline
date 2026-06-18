"""fold_campaign.py -- one-time: resolve ALL campaign records that have a
location+county (not just dot-done) and fold to dot_coordinates. Dot-based
resolution where a dot exists (precise), section-centroid otherwise (~1/2 mile,
flagged). Additive union (never overwrites existing coords). Low-memory stream.
"""
import csv, os, re, sys
from pathlib import Path
csv.field_size_limit(2_000_000)
HERE = Path(__file__).parent; OUT = Path(r"D:\project_outputs")
for _l in (HERE.parent/".env").read_text(encoding="utf-8",errors="replace").splitlines():
    _l=_l.strip()
    if "=" in _l and not _l.startswith("#"):
        k,v=_l.split("=",1); os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
sys.path.insert(0,str(HERE))
from coord.plss_resolver import PLSSResolver
R=PLSSResolver()

def parse(v,dirs):
    m=re.match(r"(\d+)\s*(["+dirs+r"])",(v or "").upper());
    return (int(m.group(1)),m.group(2)) if m else (None,None)

already=set()
with (OUT/"dot_coordinates.csv").open(newline="",encoding="utf-8",errors="replace") as f:
    for r in csv.DictReader(f):
        if (r.get("resolved_lat") or "").strip(): already.add(r.get("pdf_stem",""))

rows=[]; seen=tried=res=0
shard=OUT/"processing_status.campaign.csv"
with shard.open(newline="",encoding="utf-8",errors="replace") as f:
    for r in csv.DictReader(f):
        s=r.get("pdf_stem","")
        if not s or s in already: continue
        sec=r.get("location_section",""); cty=r.get("county_name","")
        if not (sec and cty): continue
        tw,ns=parse(r.get("location_township"),"NS"); rg,ew=parse(r.get("location_range"),"EW")
        if not (tw and rg): continue
        tried+=1
        out=None
        # dot-based first if a valid dot exists
        dr,dc=r.get("dot_row"),r.get("dot_col")
        try:
            if dr not in (None,"","0") and dc not in (None,"","0"):
                rr=R.resolve(sec,tw,ns,rg,ew,cty,dot_row=dr,dot_col=dc,
                             x_norm=r.get("dot_x_norm") or None,y_norm=r.get("dot_y_norm") or None,
                             quadrant_label=r.get("location_quadrant_db") or None)
                if rr and rr.get("lat") is not None and rr.get("source") not in("rds_miss","parse_failed","bounds_invalid",None):
                    out=(rr["lat"],rr["lon"],rr["source"])
            if out is None:
                rc=R.resolve_section_centroid(sec,tw,ns,rg,ew,cty)
                if rc and rc.get("lat") is not None and rc.get("source") not in("rds_miss","parse_failed","bounds_invalid",None):
                    out=(rc["lat"],rc["lon"],rc["source"])
        except Exception: out=None
        if out:
            res+=1
            rows.append({"pdf_stem":s,"collection":r.get("collection",""),"year":r.get("year",""),
                "month":r.get("month",""),"county_name":cty,"section":sec,
                "township":r.get("location_township",""),"range":r.get("location_range",""),
                "resolved_lat":round(out[0],7),"resolved_lon":round(out[1],7),
                "resolution_source":out[2],"needs_review":"1"})
print(f"tried {tried}, resolved {res}")
if rows:
    o=OUT/"campaign_coords.csv"
    with o.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print("wrote",o)

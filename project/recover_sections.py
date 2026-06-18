"""recover_sections.py -- FREE section recovery on already-processed records.

For records whose location stage succeeded but lost the section number (parser
gap, now fixed), re-run the location stage. The Vision OCR is served from the
disk cache, so this adds NO API cost. Newly-complete S+T+R records (with a
county) are resolved to coordinates and written for folding to the map.

Output: D:\\project_outputs\\recovered_coords.csv
Usage: python recover_sections.py [--limit N]
"""
import argparse, csv, logging, os, re, sys
from pathlib import Path
csv.field_size_limit(2_000_000)
HERE = Path(__file__).parent; OUT = Path(r"D:\project_outputs")
for _l in (HERE.parent/".env").read_text(encoding="utf-8",errors="replace").splitlines():
    _l=_l.strip()
    if "=" in _l and not _l.startswith("#"):
        k,v=_l.split("=",1); os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
sys.path.insert(0,str(HERE))
logging.disable(logging.CRITICAL)
from pdf.pdf_manager import PDFDocumentManager
from location.location_extractor import process_single_location
from coord.plss_resolver import PLSSResolver
from config import RESOLUTION_MULTIPLIER

def parse(v,dirs):
    m=re.match(r"(\d+)\s*(["+dirs+r"])",(v or "").upper())
    return (int(m.group(1)),m.group(2)) if m else (None,None)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--limit",type=int,default=0); a=ap.parse_args()
    # pdf paths
    path={}
    for r in csv.DictReader(open(OUT/"dataset_index.csv",newline="",encoding="utf-8",errors="replace")):
        path[r.get("pdf_stem","")]=r.get("pdf_path","")
    # already mapped (skip)
    mapped=set()
    for r in csv.DictReader(open(OUT/"dot_coordinates.csv",newline="",encoding="utf-8",errors="replace")):
        if (r.get("resolved_lat") or "").strip(): mapped.add(r.get("pdf_stem",""))
    # candidates: location done, no section, has twp+rng, has county, not mapped
    cand=[]
    for r in csv.DictReader(open(OUT/"processing_status.csv",newline="",encoding="utf-8",errors="replace")):
        if r.get("location_status")!="done": continue
        s=r.get("location_section","").strip(); t=r.get("location_township","").strip()
        g=r.get("location_range","").strip(); c=r.get("county_name","").strip()
        if s or not (t and g and c): continue
        if r["pdf_stem"] in mapped: continue
        cand.append((r["pdf_stem"],c))
    if a.limit: cand=cand[:a.limit]
    print(f"{len(cand)} missing-section candidates",flush=True)
    R=PLSSResolver(); rows=[]; recov=0; res=0
    for i,(stem,cty) in enumerate(cand):
        p=path.get(stem,"")
        if not p or not os.path.exists(p): continue
        try:
            mgr=PDFDocumentManager(p,resolution_multiplier=RESOLUTION_MULTIPLIER)
            loc=process_single_location(mgr,OUT/"tmp",stem,logging.getLogger("r"))
        except Exception: continue
        sec=str(loc.get("section","") or "").strip()
        tw,ns=parse(loc.get("township"),"NS"); rg,ew=parse(loc.get("range"),"EW")
        if not (sec and tw and rg): continue
        recov+=1
        try:
            rc=R.resolve_section_centroid(sec,tw,ns,rg,ew,cty)
        except Exception: rc=None
        if rc and rc.get("lat") is not None and rc.get("source") not in("rds_miss","parse_failed","bounds_invalid",None):
            res+=1
            rows.append({"pdf_stem":stem,"county_name":cty,"section":sec,
                "township":loc.get("township",""),"range":loc.get("range",""),
                "resolved_lat":round(rc["lat"],7),"resolved_lon":round(rc["lon"],7),
                "resolution_source":"recovered_section_centroid","needs_review":"1"})
        if (i+1)%200==0: print(f"  {i+1}/{len(cand)} | recovered-sec {recov}, resolved {res}",flush=True)
    print(f"DONE: section recovered {recov}, coordinate-resolved {res}",flush=True)
    if rows:
        o=OUT/"recovered_coords.csv"
        with o.open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
        print("wrote",o)

if __name__=="__main__":
    main()

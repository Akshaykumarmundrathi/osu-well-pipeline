"""
embedded_text_validate.py -- does the FREE embedded-text layer AGREE with Vision?
=================================================================================

The coverage scan (embedded_text_scan.py) showed how often a usable STR/county
VALUE sits in the existing text layer. This asks the decisive follow-up: when it
IS there, is it CORRECT? We compare embedded-text extraction against records
Vision already processed (county_status/location_status == done) -- the same
free ground truth the Tesseract pilot used.

Output: embedded_text_validate.md  (per-collection agreement %, the number that
decides whether an embedded-text-first tier can safely skip Vision).

Usage:
    python embedded_text_validate.py --per-collection 80
"""
import argparse
import csv
import os
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)
sys.path.insert(0, str(Path(__file__).parent))

import fitz
from config import COUNTY_LIST_CLEAN

MASTER = Path(os.environ.get("OUTPUT_ROOT", r"D:\project_outputs")) / "processing_status.csv"
INDEX  = Path(os.environ.get("OUTPUT_ROOT", r"D:\project_outputs")) / "dataset_index.csv"
OUT    = Path(r"D:\project_outputs_pilot")

# Reuse the honest detectors' spirit, but here we EXTRACT values to compare.
_NON_OK = [c for c in COUNTY_LIST_CLEAN if c != "oklahoma"]
_CTY_NONOK = re.compile(r"\b(" + "|".join(re.escape(c) for c in
                        sorted(_NON_OK, key=len, reverse=True)) + r")\b", re.I)
_COUNTY_LABEL = re.compile(r"county\s*[:\-]?\s*([A-Za-z]{3,15})", re.I)
_CTY_SET = set(COUNTY_LIST_CLEAN)
# Section/Township/Range filled values
_SEC = re.compile(r"\bsec(?:tion)?\s*\.?\s*(\d{1,2})\b", re.I)
_TWP = re.compile(r"\bt(?:wp|ownship)?\s*\.?\s*(\d{1,3})\s*([NS])", re.I)
_RGE = re.compile(r"\br(?:ge|ange|ng)?\s*\.?\s*(\d{1,3})\s*([EW])", re.I)
# Modern well-name encoding: "23-20N-1W" = sec-twp-rng
_NAME_STR = re.compile(r"\b(\d{1,2})\s*-\s*(\d{1,3})([NS])\s*-\s*(\d{1,3})([EW])\b", re.I)


def _pdf_text(path: Path) -> str:
    try:
        d = fitz.open(path)
        t = "\n".join(pg.get_text() for pg in d)
        d.close()
        return t
    except Exception:
        return ""


def _extract_county(txt: str) -> str:
    m = _CTY_NONOK.search(txt)
    if m:
        return m.group(1).lower()
    for lm in _COUNTY_LABEL.finditer(txt):
        c = lm.group(1).lower()
        if c in _CTY_SET and c != "oklahoma":
            return c
    return ""


def _extract_str(txt: str) -> tuple[str, str, str]:
    nm = _NAME_STR.search(txt)
    if nm:
        return nm.group(1), f"{nm.group(2)}{nm.group(3).upper()}", f"{nm.group(4)}{nm.group(5).upper()}"
    sec = _SEC.search(txt)
    twp = _TWP.search(txt)
    rge = _RGE.search(txt)
    return (sec.group(1) if sec else "",
            f"{twp.group(1)}{twp.group(2).upper()}" if twp else "",
            f"{rge.group(1)}{rge.group(2).upper()}" if rge else "")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-collection", type=int, default=80)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    # stem -> pdf_path
    paths = {}
    with INDEX.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            paths[r.get("pdf_stem", "")] = r.get("pdf_path", "")

    # Only records with a Vision ground-truth value are useful here. Sample from
    # the done-pool (county or location), else agreement can never be measured.
    by = defaultdict(list)
    with MASTER.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            m = re.search(r"\((\d+)\)", r.get("collection", ""))
            if not m:
                continue
            has_cty = r.get("county_status") == "done" and r.get("county_name")
            has_loc = r.get("location_status") == "done"
            if not (has_cty or has_loc):
                continue
            r["_c"] = int(m.group(1))
            by[r["_c"]].append(r)
    rng = random.Random(7)
    records = []
    for c, rs in sorted(by.items()):
        rng.shuffle(rs)
        records.extend(rs[:args.per_collection])
    pools = {c: len(rs) for c, rs in sorted(by.items())}
    print(f"validate sample: {len(records)} records from done-pools {pools}")

    stats = defaultdict(lambda: defaultdict(int))
    for i, r in enumerate(records):
        c = r["_c"]
        p = paths.get(r.get("pdf_stem", ""), "")
        if not p or not Path(p).exists():
            continue
        txt = _pdf_text(Path(p))
        if not txt:
            continue
        stats[c]["n"] += 1
        # county
        if r.get("county_status") == "done" and r.get("county_name"):
            ec = _extract_county(txt)
            if ec:
                stats[c]["cty_got"] += 1
                if _norm(ec) == _norm(r["county_name"]):
                    stats[c]["cty_agree"] += 1
        # STR
        if r.get("location_status") == "done":
            es = _extract_str(txt)
            vs = (r.get("location_section", ""), r.get("location_township", ""),
                  r.get("location_range", ""))
            if any(es):
                stats[c]["str_got"] += 1
                # field-level agreement (sec/twp/rng each)
                hits = sum(_norm(a) == _norm(b) and _norm(a) != "" for a, b in zip(es, vs))
                if hits == 3:
                    stats[c]["str_agree_full"] += 1
                stats[c]["str_field_hits"] += hits
                stats[c]["str_field_tot"]  += 3
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(records)}")

    lines = ["# Embedded-text vs Vision agreement (can a free tier skip Vision?)", "",
             "Agreement = embedded-text extraction matches the Vision-verified value.",
             "  • cty got/agree = county extracted / of those, matched Vision exactly",
             "  • STR got/full  = STR extracted / of those, all 3 fields matched",
             "  • STR field acc = per-field (sec,twp,rng) accuracy among extracted",
             "",
             "| coll | n | cty got | cty agree(of got) | STR got | STR full(of got) | STR field acc |",
             "|--|--|--|--|--|--|--|"]
    for c in sorted(stats):
        s = stats[c]
        n = max(s["n"], 1)
        cty_got = s["cty_got"]
        str_got = s["str_got"]
        lines.append(
            f"| C{c} | {s['n']} | {cty_got*100//n}% | "
            f"{(s['cty_agree']*100//cty_got) if cty_got else 0}% | "
            f"{str_got*100//n}% | "
            f"{(s['str_agree_full']*100//str_got) if str_got else 0}% | "
            f"{(s['str_field_hits']*100//s['str_field_tot']) if s['str_field_tot'] else 0}% |")
    report = OUT / "embedded_text_validate.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nreport -> {report}")


if __name__ == "__main__":
    main()

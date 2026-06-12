"""
pilot_tesseract.py -- measure Tesseract-vs-Vision cost/accuracy per era
========================================================================

Goal: decide how much of the $1,570 Vision bill the free tier-1 OCR can
absorb. Runs ENTIRELY OFFLINE (no Vision, no Gemini, local PDFs).

Method:
  1. Sample N already-Vision-processed records per collection from the
     master status CSV (their Vision-derived fields = free ground truth).
  2. Re-run location+county extraction with USE_VISION_API=0 (the
     pipeline's built-in Tesseract fallback) into a throwaway output dir.
  3. Compare field-by-field vs the Vision results; report per collection:
     gate-pass rate, agreement rate, disagreement samples for HUMAN REVIEW.

Outputs:
  D:\\project_outputs_pilot\\pilot_report.md
  D:\\project_outputs_pilot\\disagreements.csv   <- human review queue

Prereq: a working tesseract binary. Either:
  - TESSERACT_CMD env var pointing at tesseract.exe, or
  - run inside the project Docker image (tesseract preinstalled):
      docker run --rm -v D:\\:/data -e USE_VISION_API=0 ... (see NIGHT_REPORT)

Usage:
    python pilot_tesseract.py --per-collection 80
"""
import argparse
import csv
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)

# Offline guards
os.environ["USE_VISION_API"] = "0"
os.environ["GEMINI_DISABLED"] = "1"
# Bundled-engine Tesseract (tesserocr wheel) — no tesseract.exe required.
os.environ.setdefault("TESSEROCR_DATA", r"D:\tools\tessdata")
_tc = os.environ.get("TESSERACT_CMD", "")
if _tc:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = _tc
    os.environ["PATH"] = str(Path(_tc).parent) + os.pathsep + os.environ["PATH"]

MASTER = Path(r"D:\project_outputs\processing_status.csv")
OUT    = Path(r"D:\project_outputs_pilot")
STAGES_FIELDS = {
    "location": ["location_section", "location_township", "location_range"],
    "county":   ["county_name"],
}


def sample_records(per_coll: int) -> list[dict]:
    by = defaultdict(list)
    for r in csv.DictReader(MASTER.open(newline="", encoding="utf-8",
                                        errors="replace")):
        if r.get("location_status") != "done" and r.get("county_status") != "done":
            continue
        coll = r.get("collection", "")
        try:
            c = int(coll.split("(")[1].split(")")[0])
        except (IndexError, ValueError):
            continue
        pdf = Path(rf"D:\{coll.replace('.zip','')}") / r.get("year","") / r.get("month","") / f"{r['pdf_stem']}.pdf"
        if pdf.exists():
            r["_pdf"] = str(pdf)
            r["_coll"] = c
            by[c].append(r)
    rng = random.Random(42)
    out = []
    for c, rs in sorted(by.items()):
        rng.shuffle(rs)
        out.extend(rs[:per_coll])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-collection", type=int, default=80)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(Path(__file__).parent))
    import logging
    logging.disable(logging.CRITICAL)
    from pdf.pdf_manager import PDFDocumentManager
    from location.location_extractor import process_single_location
    from county.county_extractor import process_single_county
    from config import RESOLUTION_MULTIPLIER

    records = sample_records(args.per_collection)
    print(f"pilot sample: {len(records)} Vision-verified records")

    stats = defaultdict(lambda: defaultdict(int))
    disagreements = []
    for i, r in enumerate(records):
        c = r["_coll"]
        try:
            mgr = PDFDocumentManager(r["_pdf"],
                                     resolution_multiplier=RESOLUTION_MULTIPLIER)
            loc = process_single_location(mgr, OUT / "tmp", r["pdf_stem"],
                                          logging.getLogger("p"),
                                          collection_num=c)
            cty = process_single_county(mgr, OUT / "tmp", r["pdf_stem"],
                                        logging.getLogger("p"),
                                        collection_num=c)
        except Exception:
            stats[c]["crash"] += 1
            continue
        stats[c]["n"] += 1
        # location agreement
        if r.get("location_status") == "done":
            t = (loc.get("section",""), loc.get("township",""), loc.get("range",""))
            v = tuple(r.get(f,"") for f in STAGES_FIELDS["location"])
            if loc.get("detected"):
                stats[c]["loc_extracted"] += 1
                if t == v:
                    stats[c]["loc_agree"] += 1
                else:
                    disagreements.append({"pdf_stem": r["pdf_stem"], "coll": c,
                                          "field": "STR",
                                          "vision": "/".join(v),
                                          "tesseract": "/".join(t),
                                          "pdf": r["_pdf"]})
        if r.get("county_status") == "done":
            if cty.get("detected"):
                stats[c]["cty_extracted"] += 1
                if cty.get("name","") == r.get("county_name",""):
                    stats[c]["cty_agree"] += 1
                else:
                    disagreements.append({"pdf_stem": r["pdf_stem"], "coll": c,
                                          "field": "county",
                                          "vision": r.get("county_name",""),
                                          "tesseract": cty.get("name",""),
                                          "pdf": r["_pdf"]})
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(records)}")

    lines = ["# Tesseract pilot report", "",
             "| coll | n | loc extracted | loc agree | cty extracted | cty agree | crashes |",
             "|--|--|--|--|--|--|--|"]
    for c in sorted(stats):
        s = stats[c]
        n = max(s["n"], 1)
        lines.append(f"| C{c} | {s['n']} | {s['loc_extracted']*100//n}% | "
                     f"{s['loc_agree']*100//n}% | {s['cty_extracted']*100//n}% | "
                     f"{s['cty_agree']*100//n}% | {s['crash']} |")
    (OUT / "pilot_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (OUT / "disagreements.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["pdf_stem","coll","field","vision","tesseract","pdf"])
        w.writeheader(); w.writerows(disagreements)
    print("\n".join(lines))
    print(f"\nreport -> {OUT/'pilot_report.md'}")
    print(f"HUMAN REVIEW: {len(disagreements)} disagreements -> {OUT/'disagreements.csv'}")


if __name__ == "__main__":
    main()

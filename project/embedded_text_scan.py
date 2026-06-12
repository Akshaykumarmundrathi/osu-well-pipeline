"""
embedded_text_scan.py -- measure the FREE embedded-text-layer lever per era
===========================================================================

Many source PDFs carry a pre-existing OCR text layer (bulk-OCR'd upstream).
Where that layer already contains a usable STR / county, we can extract those
fields with ZERO API cost and zero local OCR -- just fitz.get_text() + regex.

The Tesseract pilot proved re-OCRing early scans is hopeless (C1-C5 loc 0-8%).
This asks the cheaper question: how much is ALREADY in the text layer, for free?

Method (entirely offline, read-only):
  1. Sample N PDFs per collection from dataset_index.csv (real file paths on D:).
  2. fitz.get_text() across pages -> raw embedded text.
  3. Score three signals with the pipeline's own vocabulary:
       has_text   : >= MIN_CHARS non-space chars (a text layer exists at all)
       str_signal : SEC/TWP/RGE-style tokens with adjacent digits (location is
                    recoverable from the layer)
       cty_signal : a config.COUNTY_LIST_CLEAN county name appears verbatim
  4. Report per-collection coverage + sample snippets -> embedded_text_report.md

Usage:
    python embedded_text_scan.py --per-collection 80
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

import fitz  # PyMuPDF
from config import COUNTY_LIST_CLEAN

INDEX = Path(os.environ.get("OUTPUT_ROOT", r"D:\project_outputs")) / "dataset_index.csv"
OUT   = Path(r"D:\project_outputs_pilot")
SRC_BASE = Path(os.environ.get("COLLECTION_SRC_BASE", "D:/"))

MIN_CHARS = 60           # below this = effectively no text layer

# ---------------------------------------------------------------------------
# Honest signal detection — distinguish FILLED VALUES from blank-form template.
#
# Naive keyword/county matching is fooled by:
#   • boilerplate "Oklahoma City, Oklahoma" / "OKLAHOMA CORPORATION COMMISSION"
#     → spurious county=Oklahoma on essentially every record
#   • printed blank labels "SEC ___ TWP ___ RGE ___" → STR keyword with no value
# Both inflate coverage without yielding a usable field. We require:
#   str_value : a township OR range label immediately followed by a NUMBER
#               (i.e. the blank was filled in), OR a compact "T##N R##W" run.
#   cty_value : a "COUNTY <name>" labeled value, OR any NON-Oklahoma county name
#               (Oklahoma alone is indistinguishable from the address boilerplate).
#   clean     : low OCR-garbage ratio → digital-native text layer (modern forms),
#               as opposed to bulk-OCR'd scans whose text == Tesseract-grade junk.
# ---------------------------------------------------------------------------

# Filled STR: label glued to a digit (allow OCR noise chars between).
_STR_VALUE = re.compile(
    r"(?:t(?:wp|ownship)?|r(?:ng|ange)?)\s*\.?\s*\d{1,3}\s*[NSEW]"
    r"|\bsec(?:tion)?\s*\.?\s*\d{1,2}\b"
    r"|\b\d{1,2}\s*-\s*\d{1,3}[NS]\s*-\s*\d{1,3}[EW]\b",   # 23-20N-1W style
    re.I)

_COUNTY_LABEL = re.compile(r"county\s*[:\-]?\s*([A-Za-z]{3,15})", re.I)
_NON_OK = [c for c in COUNTY_LIST_CLEAN if c != "oklahoma"]
_CTY_NONOK = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in sorted(_NON_OK, key=len, reverse=True))
    + r")\b", re.I)
_CTY_SET = set(COUNTY_LIST_CLEAN)

# Markers of the modern digital-native completion-report template.
_DIGITAL_MARK = re.compile(r"OTC Prod\.\s*Unit|Completion Report|API No\.", re.I)


def _is_clean(txt: str) -> bool:
    """True if the text layer reads as real digital text, not OCR garbage."""
    if _DIGITAL_MARK.search(txt):
        return True
    letters = sum(c.isalpha() or c.isspace() or c.isdigit() for c in txt)
    # Garbage OCR is dense with punctuation/symbol noise (~`^•|).
    return letters / max(len(txt), 1) > 0.82


def _county_value(txt: str) -> str | None:
    """Return a real well-county if present (not address boilerplate)."""
    m = _CTY_NONOK.search(txt)            # any non-Oklahoma county = real
    if m:
        return m.group(1)
    for lm in _COUNTY_LABEL.finditer(txt):  # "COUNTY <name>" labeled value
        cand = lm.group(1).lower()
        if cand in _CTY_SET and cand != "oklahoma":
            return cand
    return None


def _pdf_path(r: dict) -> Path:
    # dataset_index.csv carries an authoritative absolute pdf_path; prefer it.
    pp = (r.get("pdf_path") or "").strip()
    if pp:
        return Path(pp)
    folder = (r.get("collection") or "").replace(".zip", "").strip()
    return SRC_BASE / folder / r.get("year", "") / r.get("month", "") / f"{r.get('pdf_stem','')}.pdf"


def sample(per_coll: int) -> list[dict]:
    by = defaultdict(list)
    with INDEX.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            coll = r.get("collection", "")
            m = re.search(r"\((\d+)\)", coll)
            if not m:
                continue
            r["_c"] = int(m.group(1))
            by[r["_c"]].append(r)
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

    records = sample(args.per_collection)
    print(f"embedded-text scan: {len(records)} sampled PDFs")

    stats = defaultdict(lambda: defaultdict(int))
    samples = defaultdict(list)
    for i, r in enumerate(records):
        c = r["_c"]
        p = _pdf_path(r)
        stats[c]["n"] += 1
        if not p.exists():
            stats[c]["missing"] += 1
            continue
        try:
            d = fitz.open(p)
            txt = "\n".join(pg.get_text() for pg in d)
            d.close()
        except Exception:
            stats[c]["crash"] += 1
            continue
        clean = txt.strip()
        if len(clean) >= MIN_CHARS:
            stats[c]["has_text"] += 1
            is_clean = _is_clean(clean)
            str_ok   = bool(_STR_VALUE.search(clean))
            cty_val  = _county_value(clean)
            if is_clean:
                stats[c]["clean_text"] += 1
            if str_ok:
                stats[c]["str_value"] += 1
            if cty_val:
                stats[c]["cty_value"] += 1
            # FREE win = clean digital text AND a filled STR value AND a real county
            if is_clean and str_ok and cty_val:
                stats[c]["free_win"] += 1
                if len(samples[c]) < 2:
                    samples[c].append((r["pdf_stem"], cty_val,
                                       clean[:200].replace("\n", " ")))
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(records)}")

    lines = ["# Embedded-text-layer coverage (free extraction potential)", "",
             "Honest signal = a FILLED value in the layer, not blank-form template "
             "or address boilerplate.",
             "  • clean text  = digital-native layer (not OCR garbage)",
             "  • STR value   = township/range/section label glued to a number",
             "  • county value= a real well-county (non-Oklahoma, or 'COUNTY <name>')",
             "  • FREE WIN    = clean AND STR-value AND county-value (extractable, $0)",
             "",
             "| coll | n | has text | clean text | STR value | county value | FREE WIN |",
             "|--|--|--|--|--|--|--|"]
    for c in sorted(stats):
        s = stats[c]
        n = max(s["n"], 1)
        lines.append(f"| C{c} | {s['n']} | {s['has_text']*100//n}% | "
                     f"{s['clean_text']*100//n}% | {s['str_value']*100//n}% | "
                     f"{s['cty_value']*100//n}% | **{s['free_win']*100//n}%** |")
    lines += ["", "## Sample FREE WINs (clean text + filled STR + real county)", ""]
    for c in sorted(samples):
        for stem, cty, snip in samples[c]:
            lines.append(f"- **C{c}** `{stem}` -> county **{cty}** -- `{snip}`")

    report = OUT / "embedded_text_report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nreport -> {report}")


if __name__ == "__main__":
    main()

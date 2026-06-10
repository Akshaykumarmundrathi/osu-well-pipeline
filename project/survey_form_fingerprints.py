"""
survey_form_fingerprints.py -- catalog printed form IDs + layout fingerprints
==============================================================================

Question being tested: can we classify form layout from the PAGE TEXT
(printed form number + title + keyword geometry) instead of inferring it
from the detected grid's bbox — which the casing-table incident proved can
lie?

Samples N PDFs per collection across D:\ExportedFolderContents (1..13),
OCRs page 1 (and page 2 when page 1 is textless), and records:

  - form_id        printed "Form NNNN" / "OCC ..." token if present
  - title          first all-caps title line (e.g. WELL RECORD, COMPLETION REPORT)
  - kw positions   normalised (x,y) of SEC/TWP/RGE/COUNTY keyword tokens
  - mail_to        'Corporation Commission' boilerplate presence
  - n_tokens       OCR density (handwritten-era indicator)

Output: $OUTPUT_ROOT/form_fingerprints.csv + printed pivot
form_id x collection x year -> count, so we can see how many distinct
physical forms exist per era and whether form_id alone routes reliably.

Cost note: ~N*13 Vision calls (≈ $0.20 at N=10).

Usage:
    python survey_form_fingerprints.py --per-collection 10
"""
import argparse
import collections
import csv
import os
import random
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Load .env exactly like main.py so Vision credentials resolve.
_ENV = Path(__file__).parent.parent / ".env"
if _ENV.exists():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import logging
logging.disable(logging.WARNING)

OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", r"D:\project_outputs"))
OUT_CSV     = OUTPUT_ROOT / "form_fingerprints.csv"

_FORM_ID_RE = re.compile(
    r"\b(?:Form|FORM)\s*(?:No\.?\s*)?"
    r"([0-9]{3,5}(?:[-\s]?[A-Z0-9]{1,4})?|[A-Z]{1,4}-?[0-9]{2,5})\b")
_TITLE_RE   = re.compile(r"^[A-Z][A-Z .&'-]{6,40}$")

FIELDS = ["collection", "year", "pdf_stem", "page", "n_tokens",
          "form_id", "title", "mail_to",
          "sec_x", "sec_y", "twp_x", "twp_y", "rge_x", "rge_y",
          "county_x", "county_y", "page_w", "page_h"]


def fingerprint_page(anns, pil) -> dict | None:
    if not anns:
        return None
    full = anns[0].description or ""
    pw, ph = pil.size

    form_id = ""
    m = _FORM_ID_RE.search(full)
    if m:
        form_id = m.group(1)

    title = ""
    for line in full.splitlines()[:14]:
        line = line.strip()
        if _TITLE_RE.match(line) and "OKLAHOMA" not in line:
            title = line
            break

    out = {"n_tokens": len(anns) - 1, "form_id": form_id, "title": title,
           "mail_to": "corporation commission" in full.lower(),
           "page_w": pw, "page_h": ph}

    targets = {"sec": ("sec", "section"), "twp": ("twp", "township"),
               "rge": ("rge", "range"), "county": ("county",)}
    found = {}
    for a in anns[1:]:
        tok = (a.description or "").lower().strip(".,:;")
        for key, variants in targets.items():
            if key not in found and tok in variants:
                try:
                    xs = [v.x for v in a.bounding_poly.vertices]
                    ys = [v.y for v in a.bounding_poly.vertices]
                    found[key] = (round(min(xs) / pw, 3), round(min(ys) / ph, 3))
                except Exception:
                    pass
    for key in targets:
        x, y = found.get(key, ("", ""))
        out[f"{key}_x"], out[f"{key}_y"] = x, y
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-collection", type=int, default=10)
    ap.add_argument("--seed", type=int, default=99)
    args = ap.parse_args()

    from pdf.pdf_manager import PDFDocumentManager
    from ocr.vision_api import detect_text_with_vision
    from config import RESOLUTION_MULTIPLIER

    random.seed(args.seed)
    rows = []
    for cnum in range(1, 14):
        root = Path(rf"D:\ExportedFolderContents ({cnum})")
        if not root.exists():
            continue
        pdfs = list(root.rglob("*.pdf"))
        if not pdfs:
            continue
        sample = random.sample(pdfs, min(args.per_collection, len(pdfs)))
        print(f"C{cnum}: {len(sample)} sampled from {len(pdfs):,}")
        for p in sample:
            year = ""
            for part in p.parts:
                if part.isdigit() and len(part) == 4:
                    year = part
            try:
                mgr = PDFDocumentManager(str(p),
                                         resolution_multiplier=RESOLUTION_MULTIPLIER)
                fp = None
                page_used = 0
                for page_num, pil in mgr.iter_pil_pages():
                    anns = detect_text_with_vision(pil, manager=mgr,
                                                   page_num=page_num)
                    fp = fingerprint_page(anns, pil)
                    page_used = page_num
                    if fp and fp["n_tokens"] > 40:
                        break   # first text-rich page wins
                if fp is None:
                    continue
                fp.update(collection=cnum, year=year, pdf_stem=p.stem,
                          page=page_used)
                rows.append(fp)
            except Exception as exc:
                print(f"   ERR {p.stem[:40]}: {str(exc)[:60]}")

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    print(f"\n{len(rows)} fingerprints -> {OUT_CSV}\n")

    # Pivot: form_id by collection
    print("form_id x collection (count)")
    print("-" * 60)
    piv = collections.Counter((r.get("form_id") or "(none)", r["collection"])
                              for r in rows)
    by_form = collections.defaultdict(list)
    for (fid, c), n in piv.items():
        by_form[fid].append((c, n))
    for fid in sorted(by_form, key=lambda k: -sum(n for _, n in by_form[k])):
        cols = "  ".join(f"C{c}:{n}" for c, n in sorted(by_form[fid]))
        print(f"  {fid:<14} {cols}")

    print("\ntitle x collection")
    print("-" * 60)
    piv2 = collections.Counter((r.get("title") or "(none)", r["collection"])
                               for r in rows)
    by_t = collections.defaultdict(list)
    for (t, c), n in piv2.items():
        by_t[t].append((c, n))
    for t in sorted(by_t, key=lambda k: -sum(n for _, n in by_t[k]))[:15]:
        cols = "  ".join(f"C{c}:{n}" for c, n in sorted(by_t[t]))
        print(f"  {t[:34]:<36} {cols}")


if __name__ == "__main__":
    main()

"""
auto_scan.py  —  Bulk OCR + pattern detection on the inspection PDF set
========================================================================

Scans every PDF in the inspection index, extracts text (fitz embedded text
for digital PDFs; Tesseract OCR for scanned), then runs regex patterns for:
  - County name + placement style
  - Section / Township / Range
  - Lat/Lon presence and format (DMS vs decimal)
  - Grid quadrant / dot_nw indicators

Output: <folder>/_auto_scan.csv  — open in Excel, sort/filter by
        collection and year to see how layouts change across tiers.

Usage:
    python tools/auto_scan.py
    python tools/auto_scan.py --folder D:/inspection_pdfs
    python tools/auto_scan.py --collections 1 7 9      # subset
    python tools/auto_scan.py --workers 4               # parallel OCR
    python tools/auto_scan.py --pages all               # scan every page, not just page 0
"""

import argparse
import csv
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import fitz
except ImportError:
    sys.exit("pip install pymupdf")

try:
    from PIL import Image
except ImportError:
    sys.exit("pip install Pillow")

try:
    import pytesseract
    _TESS = True
except ImportError:
    _TESS = False
    print("WARN: pytesseract not found — only embedded text will be extracted.")

# ── Pattern definitions ────────────────────────────────────────────────────────
_PAT = {
    "county":    re.compile(
                    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+[Cc]ounty\b'
                    r'|[Cc]ounty[\s:,]+([A-Z][A-Za-z ]+?)(?:\n|,|\d)',
                    re.MULTILINE),
    "section":   re.compile(r'[Ss]ec(?:tion)?\.?\s*#?\s*(\d{1,2})\b'),
    "township":  re.compile(r'[Tt](?:wp?|ownship)?\.?\s*(\d{1,3})\s*([NnSs])\b'),
    "range":     re.compile(r'[Rr](?:ge?|ange)?\.?\s*(\d{1,3})\s*([EeWw])\b'),
    "dms":       re.compile(r'(\d{2,3})\s*[°o\*]\s*(\d{1,2})\s*[\'`′]\s*(\d{0,2})'),
    "lat_dec":   re.compile(r'\b(3[4-7]\.\d{3,6})\b'),
    "lon_dec":   re.compile(r'\b(9[4-9]\.\d{3,6}|10[0-3]\.\d{3,6})\b'),
    "lat_lbl":   re.compile(r'[Ll]at(?:itude)?[\s:.]+([0-9°\'".\-N ]{4,25})'),
    "lon_lbl":   re.compile(r'[Ll]on(?:g(?:itude)?)?[\s:.]+([0-9°\'".\-W ]{4,25})'),
    "dot_nw":    re.compile(r'\b(NW|NE|SW|SE)\s*(?:¼|1/4|quarter|QUARTER|corner|of)', re.I),
    "quadrant":  re.compile(r'\b(NW|NE|SW|SE)\b'),
    # County position heuristics — first/second half of page text
    "county_kw": re.compile(r'[Cc]ounty', re.MULTILINE),
}

OUTPUT_FIELDS = [
    "local_path", "collection", "year", "month", "position",
    "total_in_folder", "ocr_method",
    "county", "county_position",          # name + 'top'/'bottom'/'unknown'
    "section", "township", "range",
    "latlon_format",                      # 'decimal' / 'DMS' / 'labeled' / 'none'
    "lat", "lon",                         # values if found
    "dot_nw", "quadrants_seen",
    "text_length",                        # proxy for OCR quality
    "ocr_text_preview",                   # first 200 chars
]


# ── Text extraction ────────────────────────────────────────────────────────────

def extract_text(pdf_path: str, page_num: int = 0, max_width: int = 1400) -> tuple:
    """Returns (text, method)."""
    doc  = fitz.open(pdf_path)
    if page_num >= doc.page_count:
        doc.close()
        return "", "none"

    page = doc[page_num]
    embedded = page.get_text("text").strip()
    if len(embedded) > 40:
        doc.close()
        return embedded, "embedded"

    if not _TESS:
        doc.close()
        return embedded, "embedded(short)"

    scale = min(max_width / page.rect.width, 2.5)
    mat   = fitz.Matrix(scale, scale)
    pix   = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, alpha=False)
    img   = Image.frombytes("L", [pix.width, pix.height], pix.samples)
    doc.close()

    try:
        text = pytesseract.image_to_string(img, config="--psm 6")
        return text.strip(), "tesseract"
    except Exception as exc:
        return f"(OCR error: {exc})", "error"


def analyse(text: str, page_num: int = 0, total_pages: int = 1) -> dict:
    """Run all patterns and return a flat dict of findings."""
    out = {}

    # County
    m = _PAT["county"].search(text)
    if m:
        out["county"] = (m.group(1) or m.group(2) or "").strip().rstrip(",")
        # Position: where in the text did the county appear?
        pos_fraction = m.start() / max(len(text), 1)
        out["county_position"] = "top" if pos_fraction < 0.4 else "bottom"
    else:
        out["county"] = ""
        out["county_position"] = ""

    # STR
    m = _PAT["section"].search(text)
    out["section"] = m.group(1) if m else ""

    m = _PAT["township"].search(text)
    out["township"] = f"{m.group(1)}{m.group(2).upper()}" if m else ""

    m = _PAT["range"].search(text)
    out["range"] = f"{m.group(1)}{m.group(2).upper()}" if m else ""

    # Lat/Lon
    lat_dec = _PAT["lat_dec"].findall(text)
    lon_dec = _PAT["lon_dec"].findall(text)
    dms_all = _PAT["dms"].findall(text)
    lat_lbl = _PAT["lat_lbl"].findall(text)
    lon_lbl = _PAT["lon_lbl"].findall(text)

    if lat_dec or lon_dec:
        out["latlon_format"] = "decimal"
        out["lat"] = lat_dec[0] if lat_dec else ""
        out["lon"] = lon_dec[0] if lon_dec else ""
    elif dms_all:
        out["latlon_format"] = "DMS"
        d, mn, s = dms_all[0]
        out["lat"] = f"{d}°{mn}'{s}\""
        out["lon"] = f"{dms_all[1][0]}°{dms_all[1][1]}'{dms_all[1][2]}\"" if len(dms_all) > 1 else ""
    elif lat_lbl or lon_lbl:
        out["latlon_format"] = "labeled"
        out["lat"] = lat_lbl[0].strip() if lat_lbl else ""
        out["lon"] = lon_lbl[0].strip() if lon_lbl else ""
    else:
        out["latlon_format"] = "none"
        out["lat"] = ""
        out["lon"] = ""

    # Quadrant / dot_nw
    dot_nw = _PAT["dot_nw"].findall(text)
    if dot_nw:
        out["dot_nw"] = dot_nw[0].upper()
    else:
        out["dot_nw"] = ""

    quads = list(dict.fromkeys(_PAT["quadrant"].findall(text)))
    out["quadrants_seen"] = " ".join(quads) if quads else ""

    return out


# ── Worker ────────────────────────────────────────────────────────────────────

def process_entry(entry: dict, scan_all_pages: bool) -> dict:
    path  = entry["local_path"]
    row   = {k: entry.get(k, "") for k in
             ["local_path", "collection", "year", "month",
              "position", "total_in_folder"]}

    try:
        doc = fitz.open(path)
        n   = doc.page_count
        doc.close()
    except Exception:
        n = 1

    pages_to_scan = range(n) if scan_all_pages else [0]
    best_text, best_method, best_findings = "", "none", {}

    for pg in pages_to_scan:
        text, method = extract_text(path, pg)
        if len(text) > len(best_text):
            best_text    = text
            best_method  = method
            best_findings = analyse(text, pg, n)
            if best_findings.get("county") and best_findings.get("section"):
                break   # good enough

    row["ocr_method"]       = best_method
    row["text_length"]      = len(best_text)
    row["ocr_text_preview"] = best_text[:200].replace("\n", " ")
    row.update(best_findings)

    # Fill missing keys
    for f in OUTPUT_FIELDS:
        row.setdefault(f, "")

    return row


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Bulk OCR + pattern detection on inspection PDFs.")
    ap.add_argument("--folder",      default=str(Path(__file__).parent.parent / "inspection_pdfs"),
                    help="Folder with _inspection_index.csv (default: ../inspection_pdfs)")
    ap.add_argument("--collections", type=int, nargs="+", metavar="N",
                    help="Only scan these collection numbers")
    ap.add_argument("--workers",     type=int, default=2,
                    help="Parallel OCR workers (default: 2)")
    ap.add_argument("--pages",       choices=["first", "all"], default="first",
                    help="Scan only page 0 (fast) or all pages (thorough)")
    ap.add_argument("--limit",       type=int, default=None,
                    help="Stop after N entries (quick test)")
    args = ap.parse_args()

    folder = Path(args.folder)
    idx    = folder / "_inspection_index.csv"
    if not idx.exists():
        sys.exit(f"No _inspection_index.csv in {folder}")

    entries = []
    with open(idx, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status") != "ok":
                continue
            if not Path(row["local_path"]).exists():
                continue
            if args.collections:
                m = re.search(r"\d+", row.get("collection", ""))
                if not m or int(m.group()) not in args.collections:
                    continue
            entries.append(row)

    if args.limit:
        entries = entries[:args.limit]

    scan_all = (args.pages == "all")
    out_path = folder / "_auto_scan.csv"

    print(f"Auto-scan: {len(entries)} PDFs  |  workers={args.workers}  |  pages={args.pages}")
    print(f"Output → {out_path}\n")

    results   = []
    done      = 0
    lock      = threading.Lock()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_entry, e, scan_all): e for e in entries}
        for fut in as_completed(futures):
            try:
                row = fut.result()
                with lock:
                    results.append(row)
                    done += 1
                    if done % 50 == 0 or done == len(entries):
                        pct = done / len(entries) * 100
                        coll = row.get("collection", "")
                        year = row.get("year", "")
                        print(f"  [{done:>4}/{len(entries)}  {pct:4.0f}%]  "
                              f"{coll}/{year}  county={row.get('county','')!r}  "
                              f"latlon={row.get('latlon_format','')}")
            except Exception as exc:
                e = futures[fut]
                print(f"  ERROR {e.get('local_path','')}: {exc}")

    # Sort by collection number + year + month + position
    def _sort_key(r):
        coll_n = int(re.search(r"\d+", r.get("collection","0")).group())
        return (coll_n, r.get("year",""), r.get("month",""), r.get("position",""))
    results.sort(key=_sort_key)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    # Quick summary
    print(f"\n{'='*60}")
    print(f"  PDFs scanned  : {len(results)}")
    print(f"  County found  : {sum(1 for r in results if r.get('county'))}")
    print(f"  STR found     : {sum(1 for r in results if r.get('section'))}")
    fmt_counts = {}
    for r in results:
        fmt_counts[r.get("latlon_format","none")] = fmt_counts.get(r.get("latlon_format","none"),0)+1
    for fmt, cnt in sorted(fmt_counts.items(), key=lambda x: -x[1]):
        print(f"  Lat/lon={fmt:<12}: {cnt}")
    print(f"\n  Saved: {out_path}")
    print(f"  Open in Excel — sort by collection+year to spot layout patterns.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()

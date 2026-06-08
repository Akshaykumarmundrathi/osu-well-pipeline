"""
run_1911.py  --  Grid extraction runner + manual inspection loop
================================================================
Usage:
    cd project_modular
    python run_1911.py              # run grid extraction on 100 PDFs
    python run_1911.py --inspect    # manual inspection of results
    python run_1911.py --reinforce  # apply learned fixes to detector

Outputs:
    outputs_1911/grids/         extracted grid PNGs
    outputs_1911/debug/         full-page debug images with bbox overlay
    outputs_1911/results.csv    per-PDF result: detected / method / size
    outputs_1911/inspection.csv manual Y/N labels
"""

import argparse
import csv
import os
import random
import shutil
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image as PILImage

# -------------------------------------------------------------------
# Paths
# -------------------------------------------------------------------
THIS_DIR    = Path(__file__).parent   # D:\project_modular\project
OUT_DIR     = THIS_DIR / "outputs_1911"
GRID_DIR    = OUT_DIR / "grids"
DEBUG_DIR   = OUT_DIR / "debug"
RESULTS_CSV = OUT_DIR / "results.csv"
INSPECT_CSV = OUT_DIR / "inspection.csv"
SAMPLE_TXT  = THIS_DIR / "sample_1911.txt"

# THIS_DIR is already the project package root; add it to path so
# `from grid.scoring import ...` works when invoked via `python run_1911.py`
sys.path.insert(0, str(THIS_DIR))

# GCP credentials: prefer GOOGLE_APPLICATION_CREDENTIALS env var (set by .env).
# Fall back to auto-discovering the single *.json in credentials/ (same logic as config.py).
if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    _creds_dir = THIS_DIR.parent / "credentials"
    if _creds_dir.is_dir():
        _candidates = sorted(_creds_dir.glob("*.json"))
        if len(_candidates) == 1:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_candidates[0])

# -------------------------------------------------------------------
# Grid detection (inline — same logic as project/grid/)
# -------------------------------------------------------------------

TARGET_AR_MIN  = 0.78   # slightly relaxed — early forms can be slightly taller
TARGET_AR_MAX  = 1.30   # relaxed for wider form layouts (JUDY BARNETT type)
TARGET_W_MIN   = 180    # lowered — catches genuinely small grids
TARGET_W_MAX   = 600    # tightened — rejects full-page grabs (>900px wide)
TARGET_H_MIN   = 180
TARGET_H_MAX   = 600
RESOLUTION     = 2      # PDF render multiplier; fallback to 3x for all_none


def _crop(cv_img, x, y, w, h, buf=5):
    H, W = cv_img.shape[:2]
    y0 = max(0, y - buf);  y1 = min(H, y + h + buf)
    x0 = max(0, x - buf);  x1 = min(W, x + w + buf)
    if y1 > y0 and x1 > x0:
        return cv_img[y0:y1, x0:x1], (x, y, w, h)
    return None, None


def _largest_quad_contour(binary, cv_img, min_wh=50):
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, max_area = None, 0
    for cnt in contours:
        peri  = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) == 4:
            x, y, w, h = cv2.boundingRect(approx)
            area = w * h
            if area > max_area and w > min_wh and h > min_wh:
                max_area = area
                best = (x, y, w, h)
    if best:
        return _crop(cv_img, *best)
    return None, None


def method_adaptive(cv_img):
    gray   = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray   = cv2.GaussianBlur(gray, (5, 5), 0)
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 8)
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    hl = cv2.morphologyEx(binary, cv2.MORPH_OPEN, hk, iterations=2)
    vl = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vk, iterations=2)
    combined = cv2.dilate(
        cv2.addWeighted(hl, 0.5, vl, 0.5, 0),
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)), iterations=2)
    return _largest_quad_contour(combined, cv_img)


def method_otsu(cv_img):
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    dilated = cv2.dilate(
        binary, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=3)
    return _largest_quad_contour(dilated, cv_img)


def method_canny(cv_img):
    gray    = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    filtered = cv2.GaussianBlur(gray, (5, 5), 0)
    edges   = cv2.Canny(filtered, 50, 150)
    dilated = cv2.dilate(
        edges, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=2)
    return _largest_quad_contour(dilated, cv_img)


def method_hough(cv_img):
    """Hough: tuned to find grid lines, not page boundary."""
    gray  = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    blur  = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150, apertureSize=3)
    H, W  = cv_img.shape[:2]

    # minLineLength = 15% of shorter side so we find internal grid lines
    min_len = int(min(H, W) * 0.15)
    lines   = cv2.HoughLinesP(edges, 1, np.pi / 180,
                               threshold=40, minLineLength=min_len, maxLineGap=8)
    if lines is None:
        return None, None

    h_lines, v_lines = [], []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        angle  = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        length = np.hypot(x2 - x1, y2 - y1)
        # restrict to lines < 80% of full dimension (exclude page border)
        if abs(angle) < 5 and length < W * 0.80:
            h_lines.append(line[0])
        elif abs(abs(angle) - 90) < 5 and length < H * 0.80:
            v_lines.append(line[0])

    if len(h_lines) < 4 or len(v_lines) < 4:
        return None, None

    xs = [c for l in h_lines + v_lines for c in (l[0], l[2])]
    ys = [c for l in h_lines + v_lines for c in (l[1], l[3])]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    w, h = x_max - x_min, y_max - y_min
    if w < 10 or h < 10:
        return None, None
    return _crop(cv_img, x_min, y_min, w, h)


ALL_METHODS = [
    ("adaptive", method_adaptive),
    ("otsu",     method_otsu),
    ("canny",    method_canny),
    ("hough",    method_hough),
]


def is_valid_bbox(bbox, cv_img):
    x, y, w, h = bbox
    H, W = cv_img.shape[:2]
    if w <= 0 or h <= 0:
        return False
    if w > 0.85 * W or h > 0.85 * H:   # reject full-page grabs
        return False
    ar = w / h
    return (TARGET_AR_MIN <= ar <= TARGET_AR_MAX and
            TARGET_W_MIN <= w <= TARGET_W_MAX and
            TARGET_H_MIN <= h <= TARGET_H_MAX)


def _line_density_score(region_bgr):
    """
    Estimate how grid-like a region is by measuring H+V line density.
    Returns 0.0-1.0.  Pure text/blank areas score near 0; tight grids score >0.5.
    """
    gray = cv2.cvtColor(region_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    H, W = binary.shape
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, W // 8), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, H // 8)))
    hl = cv2.morphologyEx(binary, cv2.MORPH_OPEN, hk)
    vl = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vk)
    h_pct = hl.sum() / (255 * H * W + 1)
    v_pct = vl.sum() / (255 * H * W + 1)
    return min(1.0, (h_pct + v_pct) * 20)   # scale so ~0.05 coverage = 1.0


def detect_grid(cv_img, min_line_density=0.15):
    """
    Run all methods, filter by size+AR+line-density, return best candidate.
    min_line_density: reject regions that don't look like a grid (text blocks, blank).
    """
    candidates = []
    for name, func in ALL_METHODS:
        try:
            region, bbox = func(cv_img)
        except Exception:
            continue
        if region is None or bbox is None:
            continue
        if not is_valid_bbox(bbox, cv_img):
            continue
        # Line-density guard: reject text bands and blank regions
        score = _line_density_score(region)
        if score < min_line_density:
            continue
        x, y, w, h = bbox
        candidates.append({"method": name, "region": region,
                            "bbox": bbox, "area": w * h, "density": score})
    if not candidates:
        return None, None, "none"
    # Prefer highest density first, break ties by area
    best = max(candidates, key=lambda c: (c["density"], c["area"]))
    return best["region"], best["bbox"], best["method"]


def render_pdf_page(pdf_path, page_num=0, mult=RESOLUTION):
    import fitz
    doc = fitz.open(str(pdf_path))
    if page_num >= len(doc):
        doc.close()
        return None
    pix = doc[page_num].get_pixmap(matrix=fitz.Matrix(mult, mult), alpha=False)
    doc.close()
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def save_debug_image(cv_img, bbox, pdf_stem, page_num, out_path):
    """Full-page image with bbox overlay."""
    overlay = cv_img.copy()
    if bbox:
        x, y, w, h = bbox
        cv2.rectangle(overlay, (x, y), (x + w, y + h), (0, 255, 0), 3)
        cv2.putText(overlay, f"{w}x{h}", (x, max(0, y - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    small = cv2.resize(overlay, (overlay.shape[1] // 3, overlay.shape[0] // 3))
    cv2.imwrite(str(out_path), small)


# -------------------------------------------------------------------
# Phase 1: Run grid extraction
# -------------------------------------------------------------------

def run_extraction():
    sample_pdfs = Path(SAMPLE_TXT).read_text(encoding="utf-8").strip().splitlines()
    print(f"\n{'='*60}")
    print(f"  GRID EXTRACTION — {len(sample_pdfs)} PDFs (1911 collection)")
    print(f"{'='*60}\n")

    GRID_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    detected = failed = skipped = 0
    method_counts = {}
    size_failures = []

    for i, pdf_path in enumerate(sample_pdfs, 1):
        stem = Path(pdf_path).stem
        print(f"  [{i:3d}/100] {stem[:55]}", end=" ... ", flush=True)

        cv_img = None
        try:
            cv_img = render_pdf_page(pdf_path, page_num=0, mult=RESOLUTION)
        except Exception as e:
            print(f"RENDER ERR: {e}")
            results.append({"pdf_stem": stem, "pdf_path": pdf_path,
                            "status": "render_error", "method": "", "grid_w": 0,
                            "grid_h": 0, "page_w": 0, "page_h": 0, "grid_png": ""})
            skipped += 1
            continue

        if cv_img is None:
            print("NO PAGE")
            results.append({"pdf_stem": stem, "pdf_path": pdf_path,
                            "status": "no_page", "method": "", "grid_w": 0,
                            "grid_h": 0, "page_w": 0, "page_h": 0, "grid_png": ""})
            skipped += 1
            continue

        pH, pW = cv_img.shape[:2]
        region, bbox, method = detect_grid(cv_img)

        # Save debug (always)
        debug_path = DEBUG_DIR / f"{stem}_debug.jpg"
        save_debug_image(cv_img, bbox, stem, 0, debug_path)

        if region is not None:
            grid_png = GRID_DIR / f"{stem}_grid.png"
            cv2.imwrite(str(grid_png), region)
            x, y, w, h = bbox
            print(f"OK  {w}x{h}  [{method}]")
            method_counts[method] = method_counts.get(method, 0) + 1
            detected += 1
            results.append({
                "pdf_stem": stem, "pdf_path": pdf_path,
                "status": "detected", "method": method,
                "grid_w": w, "grid_h": h,
                "page_w": pW, "page_h": pH,
                "grid_png": str(grid_png),
            })
        else:
            # Fallback 1: try page 1
            cv_img2 = None
            try:
                cv_img2 = render_pdf_page(pdf_path, page_num=1, mult=RESOLUTION)
            except Exception:
                pass
            if cv_img2 is not None:
                region2, bbox2, method2 = detect_grid(cv_img2)
                if region2 is not None:
                    grid_png = GRID_DIR / f"{stem}_grid.png"
                    cv2.imwrite(str(grid_png), region2)
                    x, y, w, h = bbox2
                    print(f"OK(p2) {w}x{h}  [{method2}]")
                    method_counts[method2] = method_counts.get(method2, 0) + 1
                    detected += 1
                    results.append({
                        "pdf_stem": stem, "pdf_path": pdf_path,
                        "status": "detected_p2", "method": method2,
                        "grid_w": w, "grid_h": h,
                        "page_w": pW, "page_h": pH,
                        "grid_png": str(grid_png),
                    })
                    continue

            # Fallback 2: 3x resolution on page 0 (catches small/faint grids)
            cv_img3 = None
            try:
                cv_img3 = render_pdf_page(pdf_path, page_num=0, mult=3)
            except Exception:
                pass
            if cv_img3 is not None:
                region3, bbox3, method3 = detect_grid(cv_img3)
                if region3 is not None:
                    grid_png = GRID_DIR / f"{stem}_grid.png"
                    cv2.imwrite(str(grid_png), region3)
                    x, y, w, h = bbox3
                    print(f"OK(3x) {w}x{h}  [{method3}]")
                    method_counts[method3 + "@3x"] = method_counts.get(method3 + "@3x", 0) + 1
                    detected += 1
                    results.append({
                        "pdf_stem": stem, "pdf_path": pdf_path,
                        "status": "detected_3x", "method": method3 + "@3x",
                        "grid_w": w, "grid_h": h,
                        "page_w": pW, "page_h": pH,
                        "grid_png": str(grid_png),
                    })
                    continue

            # Probe what each method returned (for diagnosis)
            probe = []
            for name, func in ALL_METHODS:
                try:
                    _, bb = func(cv_img)
                    if bb:
                        w2, h2 = bb[2], bb[3]
                        ar2 = round(w2 / h2, 2) if h2 else 0
                        probe.append(f"{name}:{w2}x{h2}(AR={ar2})")
                except Exception:
                    pass
            probe_str = " | ".join(probe) if probe else "all_none"
            print(f"MISS  [{probe_str}]")
            size_failures.append((stem, probe_str))
            failed += 1
            results.append({
                "pdf_stem": stem, "pdf_path": pdf_path,
                "status": "missed", "method": "",
                "grid_w": 0, "grid_h": 0,
                "page_w": pW, "page_h": pH,
                "grid_png": "",
            })

    # Write results CSV
    fields = ["pdf_stem", "pdf_path", "status", "method",
              "grid_w", "grid_h", "page_w", "page_h", "grid_png"]
    with RESULTS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    print(f"\n{'='*60}")
    print(f"  RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"  Detected : {detected}/100  ({detected}%)")
    print(f"  Missed   : {failed}/100")
    print(f"  Skipped  : {skipped}/100")
    print(f"\n  Method breakdown:")
    for m, n in sorted(method_counts.items(), key=lambda x: -x[1]):
        print(f"    {m:12s}  {n}")
    if size_failures:
        print(f"\n  Missed PDFs (raw detection before filter):")
        for stem, probe in size_failures[:20]:
            print(f"    {stem[:50]:50s}  {probe}")
    print(f"\n  Results -> {RESULTS_CSV}")
    print(f"  Grids   -> {GRID_DIR}")
    print(f"  Debug   -> {DEBUG_DIR}")


# -------------------------------------------------------------------
# Phase 2: Manual inspection
# -------------------------------------------------------------------

def run_inspection():
    if not RESULTS_CSV.exists():
        print("Run extraction first: python run_1911.py")
        return

    with RESULTS_CSV.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Load existing inspection labels
    existing = {}
    if INSPECT_CSV.exists():
        with INSPECT_CSV.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing[r["pdf_stem"]] = r

    detected = [r for r in rows if r["status"].startswith("detected")]
    print(f"\n{'='*60}")
    print(f"  MANUAL INSPECTION — {len(detected)} detected grids")
    print(f"  Already labelled: {len(existing)}")
    print(f"{'='*60}")
    print("  Commands: y=correct  n=wrong  s=skip  q=quit")
    print("  (The debug image shows the full page with green bbox)")
    print()

    new_labels = dict(existing)
    inspected = 0

    for r in detected:
        stem = r["pdf_stem"]
        if stem in existing:
            continue   # already labelled

        grid_png  = r["grid_png"]
        debug_img = DEBUG_DIR / f"{stem}_debug.jpg"

        print(f"  [{inspected+1}/{len(detected)-len(existing)}]  {stem[:60]}")
        print(f"    Grid: {r['grid_w']}x{r['grid_h']}px  method={r['method']}")
        print(f"    Debug image: {debug_img}")

        # Try to open the images for the user
        opened = False
        for img_path in [grid_png, str(debug_img)]:
            if img_path and Path(img_path).exists():
                try:
                    os.startfile(str(img_path))
                    opened = True
                    break
                except Exception:
                    pass

        if not opened:
            print("    (could not auto-open image — check path above)")

        while True:
            ans = input("    Correct? [y/n/s/q]: ").strip().lower()
            if ans in ("y", "n", "s", "q"):
                break
            print("    Enter y, n, s, or q")

        if ans == "q":
            print("  Quitting inspection.")
            break
        if ans == "s":
            continue

        note = ""
        if ans == "n":
            note = input("    What's wrong? (brief): ").strip()

        new_labels[stem] = {
            "pdf_stem":   stem,
            "pdf_path":   r["pdf_path"],
            "grid_w":     r["grid_w"],
            "grid_h":     r["grid_h"],
            "method":     r["method"],
            "correct":    "yes" if ans == "y" else "no",
            "note":       note,
            "grid_png":   grid_png,
        }
        inspected += 1

    # Write inspection CSV
    fields = ["pdf_stem", "pdf_path", "grid_w", "grid_h",
              "method", "correct", "note", "grid_png"]
    with INSPECT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(new_labels.values())

    # Summary
    labels = list(new_labels.values())
    yes = sum(1 for l in labels if l["correct"] == "yes")
    no  = sum(1 for l in labels if l["correct"] == "no")
    print(f"\n  Labelled {inspected} new.  Total: {yes} correct, {no} wrong.")
    print(f"  Saved -> {INSPECT_CSV}")

    if no:
        print(f"\n  Wrong detections:")
        for l in labels:
            if l["correct"] == "no":
                print(f"    {l['pdf_stem'][:55]}  note={l['note']}")


# -------------------------------------------------------------------
# Phase 3: Reinforce (analyze failures + print recommendations)
# -------------------------------------------------------------------

def run_reinforce():
    if not RESULTS_CSV.exists():
        print("Run extraction first.")
        return

    with RESULTS_CSV.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    missed  = [r for r in rows if r["status"] == "missed"]
    detected = [r for r in rows if r["status"].startswith("detected")]

    print(f"\n{'='*60}")
    print(f"  REINFORCEMENT ANALYSIS")
    print(f"{'='*60}")
    print(f"  Detected: {len(detected)}  |  Missed: {len(missed)}")

    # Inspect inspection labels if available
    wrong = []
    if INSPECT_CSV.exists():
        with INSPECT_CSV.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["correct"] == "no":
                    wrong.append(r)

    print(f"  Manually flagged wrong: {len(wrong)}")

    # Analyse size of missed detections
    print(f"\n  MISSED — page sizes (what the full page rendered to):")
    for r in missed[:20]:
        print(f"    {r['pdf_stem'][:50]:50s}  page={r['page_w']}x{r['page_h']}")

    # Recommendations
    print(f"\n{'='*60}")
    print(f"  RECOMMENDATIONS")
    print(f"{'='*60}")

    total = len(rows)
    det_rate = 100 * len(detected) / total if total else 0
    acc_rate = 100 * (len(detected) - len(wrong)) / total if total else 0

    print(f"  Detection rate : {det_rate:.0f}%")
    print(f"  Accuracy rate  : {acc_rate:.0f}%  (detected AND correct)")

    if missed:
        # Check if lowering threshold would help
        print(f"\n  To recover missed grids:")
        print(f"    1. Try resolution_multiplier=3 (renders grids ~50% larger)")
        print(f"    2. Lower TARGET_W_MIN/H_MIN further if grids are truly small")
        print(f"    3. Check if missed PDFs actually have a grid (open debug images)")

    if wrong:
        print(f"\n  Wrong detections ({len(wrong)}):")
        for r in wrong:
            print(f"    {r['pdf_stem'][:50]}  note: {r['note']}")
        print(f"    Fix: tighten AR filter or add line-density check")


# -------------------------------------------------------------------
# Entrypoint
# -------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect",   action="store_true")
    ap.add_argument("--reinforce", action="store_true")
    args = ap.parse_args()

    if args.inspect:
        run_inspection()
    elif args.reinforce:
        run_reinforce()
    else:
        run_extraction()

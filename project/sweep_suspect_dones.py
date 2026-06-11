"""
sweep_suspect_dones.py -- find SILENTLY-WRONG done records, era-wide
=====================================================================

Premise (user insight): if ~79 sampled grids of an era were false
positives, the rest of that era's "done" records are suspect too —
failure patterns generalize across form eras.

This sweep needs NO reprocessing to detect them: every done record's
stored grid bbox (metadata.json, raw [x,y,w,h]) is compared against the
hand-measured per-collection envelope (2,269 manual grid boxes). A bbox
centre outside the envelope (+2*PAD) on a collection with envelope data
is flagged suspect.

Outputs:
  suspect_dones.csv            stem, collection, year, bbox, centre, verdict
  printed per-collection/era summary
  --reset writes grid->pending for suspects in the chosen status CSV
          so the next run redoes them with the current stack.

Usage:
    python sweep_suspect_dones.py                       # main outputs scan
    python sweep_suspect_dones.py --root D:\\project_outputs_test1000
    python sweep_suspect_dones.py --reset               # also reset suspects
"""
import argparse
import csv
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)
sys.path.insert(0, str(Path(__file__).parent))

from location.recipes import GRID_ENVELOPES, PAD


def page_size_of(meta: dict) -> tuple[int, int] | None:
    # metadata doesn't store page size; estimate from known render ~2x:
    # not needed — envelopes are page-relative, so we need page size.
    # Fall back: grid stage page sizes cluster ~1240-1280 x 1600-2010 px.
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"D:\project_outputs")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    root = Path(args.root)
    meta_root = root / "metadata"

    suspects = []
    scanned = 0
    by_coll: Counter = Counter()
    sus_coll: Counter = Counter()

    for meta_path in meta_root.rglob("metadata.json"):
        try:
            d = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        src = d.get("source", {})
        g = (d.get("stages") or {}).get("grid") or {}
        if not g.get("detected"):
            continue
        coll_raw = src.get("collection", "")
        try:
            cnum = int(coll_raw.split("(")[1].split(")")[0])
        except (IndexError, ValueError):
            continue
        env = GRID_ENVELOPES.get(cnum)
        if env is None:
            continue
        bbox = g.get("bbox") or []
        if len(bbox) != 4:
            continue
        try:
            x, y, w, h = [float(v) for v in bbox]
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0 or w > 2000:
            continue
        # Page size: derive from the saved annotated page if cheap — else
        # assume the 2x render family (pw ~1240-1660). Use relative position
        # with the COMMON page width family per orientation:
        pw = 1268.0 if w < 700 else 1614.0   # portrait vs landscape heuristic
        ph = 2002.0 if pw == 1268.0 else 1240.0
        cx = (x + w / 2) / pw
        cy = (y + h / 2) / ph
        scanned += 1
        by_coll[cnum] += 1
        ex0, ey0, ex1, ey1 = env
        if not (ex0 - 2 * PAD <= cx <= ex1 + 2 * PAD
                and ey0 - 2 * PAD <= cy <= ey1 + 2 * PAD):
            sus_coll[cnum] += 1
            suspects.append({
                "pdf_stem": src.get("pdf_stem", ""),
                "collection": cnum, "year": src.get("year", ""),
                "bbox": f"{x:.0f},{y:.0f},{w:.0f},{h:.0f}",
                "centre": f"{cx:.2f},{cy:.2f}",
                "envelope": str(env),
            })

    print(f"scanned {scanned} done-grid records with envelope coverage")
    print(f"{'coll':<6}{'done':<8}{'suspect':<9}rate")
    for c in sorted(by_coll):
        n, s = by_coll[c], sus_coll.get(c, 0)
        print(f"C{c:<5}{n:<8}{s:<9}{s*100//n if n else 0}%")

    out_csv = root / "suspect_dones.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["pdf_stem", "collection", "year",
                                          "bbox", "centre", "envelope"])
        w.writeheader(); w.writerows(suspects)
    print(f"{len(suspects)} suspects -> {out_csv}")

    if args.reset and suspects:
        status_csv = root / "processing_status.csv"
        stems = {s["pdf_stem"] for s in suspects}
        with status_csv.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)
        n = 0
        for row in rows:
            if row["pdf_stem"] in stems:
                for stage in ("grid", "location", "county", "dot"):
                    for c in (f"{stage}_status", f"{stage}_confidence",
                              f"{stage}_error_type"):
                        if c in row:
                            row[c] = "pending" if c.endswith("_status") else ""
                n += 1
        shutil.copy2(status_csv, status_csv.with_suffix(".csv.suspect_bak"))
        tmp = status_csv.with_suffix(".csv.new")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader(); w.writerows(rows)
        tmp.replace(status_csv)
        print(f"{n} suspect records reset to pending in {status_csv}")


if __name__ == "__main__":
    main()

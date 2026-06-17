"""clean_failures.py -- per-stage failure CSVs for human inspection.

Streams processing_status.csv (low memory, safe to run alongside a live chain)
and writes one clean CSV per stage listing only the FAILED records, with the
fields a reviewer needs: stem, collection/year/month, error type, the grid
image path, and whatever was extracted so far. Also writes a one-line summary.

Output (under $OUTPUT_ROOT/failures/):
  grid_failures.csv  location_failures.csv  county_failures.csv  dot_failures.csv
  failures_summary.csv

Usage: python clean_failures.py
"""
import csv, os
from collections import Counter
from pathlib import Path

csv.field_size_limit(2_000_000)
OUT = Path(os.environ.get("OUTPUT_ROOT", r"D:\project_outputs"))
FAILDIR = OUT / "failures"
STAGES = ["grid", "location", "county", "dot"]
COMMON = ["pdf_stem", "collection", "year", "month"]
EXTRA = {
    "grid":     ["grid_error_type", "grid_image_path"],
    "location": ["location_error_type", "location_section", "location_township",
                 "location_range", "grid_image_path"],
    "county":   ["county_error_type", "county_name", "grid_image_path"],
    "dot":      ["dot_error_type", "grid_image_path"],
}


def main():
    FAILDIR.mkdir(parents=True, exist_ok=True)
    writers, files, counts = {}, {}, Counter()
    err_by_stage = {s: Counter() for s in STAGES}
    for s in STAGES:
        fp = (FAILDIR / f"{s}_failures.csv").open("w", newline="", encoding="utf-8")
        files[s] = fp
        w = csv.DictWriter(fp, fieldnames=COMMON + EXTRA[s])
        w.writeheader(); writers[s] = w

    src = OUT / "processing_status.csv"
    with src.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            for s in STAGES:
                if (r.get(f"{s}_status") or "") == "failed":
                    counts[s] += 1
                    et = r.get(f"{s}_error_type", "") or "unspecified"
                    err_by_stage[s][et] += 1
                    writers[s].writerow({k: r.get(k, "") for k in COMMON + EXTRA[s]})
    for fp in files.values():
        fp.close()

    with (FAILDIR / "failures_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["stage", "total_failed", "error_type", "count"])
        for s in STAGES:
            for et, c in err_by_stage[s].most_common():
                w.writerow([s, counts[s], et, c])
    print("Failure CSVs ->", FAILDIR)
    for s in STAGES:
        print(f"  {s}: {counts[s]:,} failed  "
              + ", ".join(f"{e}={c}" for e, c in err_by_stage[s].most_common(4)))


if __name__ == "__main__":
    main()

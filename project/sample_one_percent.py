"""Sample 1% of PDFs per (collection, year, month) for review-driven tuning.
Writes a dataset_index-schema CSV consumable by `main.py --index`.
Usage: python sample_one_percent.py [--pct 1.0] [--out D:\\project_outputs_sample\\sample_index.csv]
"""
import argparse, csv, math, random
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(2_000_000)
SRC = Path(r"D:\project_outputs\dataset_index.csv")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pct", type=float, default=1.0)
    ap.add_argument("--out", type=Path,
                    default=Path(r"D:\project_outputs_sample\sample_index.csv"))
    a = ap.parse_args()
    with SRC.open(newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f); cols = rd.fieldnames
        groups = defaultdict(list)
        for r in rd:
            groups[(r.get("collection",""), r.get("year",""), r.get("month",""))].append(r)
    rng = random.Random(13); out = []
    for key in sorted(groups):
        g = groups[key]; rng.shuffle(g)
        n = max(1, math.ceil(len(g) * a.pct / 100))
        out.extend(g[:n])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(out)
    print(f"{len(out)} sampled from {len(groups)} month-folders -> {a.out}")


if __name__ == "__main__":
    main()

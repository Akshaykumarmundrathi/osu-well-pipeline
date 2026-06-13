"""Sample 1% per (collection,year,month) from records NEVER processed before
(not present in processing_status.csv). Writes a dataset_index-schema CSV for
`main.py --index`. Usage: python sample_unseen.py [--pct 1.0] [--out PATH]
"""
import argparse, csv, math, random
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(2_000_000)
SRC    = Path(r"D:\project_outputs\dataset_index.csv")
STATUS = Path(r"D:\project_outputs\processing_status.csv")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pct", type=float, default=1.0)
    ap.add_argument("--out", type=Path,
                    default=Path(r"D:\project_outputs_sample\unseen_index.csv"))
    a = ap.parse_args()
    seen = set()
    with STATUS.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            s = r.get("pdf_stem", "")
            if s:
                seen.add(s)
    groups = defaultdict(list)
    with SRC.open(newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f); cols = rd.fieldnames
        for r in rd:
            if r.get("pdf_stem", "") in seen:
                continue
            groups[(r.get("collection",""), r.get("year",""), r.get("month",""))].append(r)
    rng = random.Random(13); out = []
    for key in sorted(groups):
        g = groups[key]; rng.shuffle(g)
        out.extend(g[:max(1, math.ceil(len(g) * a.pct / 100))])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(out)
    print(f"{len(out)} UNSEEN sampled from {len(groups)} month-folders "
          f"({len(seen):,} already-processed excluded) -> {a.out}")


if __name__ == "__main__":
    main()

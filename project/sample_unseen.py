"""Sample 1% per (collection,year,month) from records with NO stage results yet
(overall_state not_processed or queued in master_ledger.csv) -- i.e. never
actually processed, even if a status row exists. Writes a dataset_index-schema
CSV for `main.py --index`. Usage: python sample_unseen.py [--pct 1.0] [--out PATH]
"""
import argparse, csv, math, random
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(2_000_000)
SRC    = Path(r"D:\project_outputs\dataset_index.csv")
LEDGER = Path(r"D:\project_outputs\master_ledger.csv")
ELIGIBLE = {"not_processed", "queued"}   # never produced a stage result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pct", type=float, default=1.0)
    ap.add_argument("--out", type=Path,
                    default=Path(r"D:\project_outputs_sample\unseen_index.csv"))
    a = ap.parse_args()

    state = {}
    with LEDGER.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            state[r["pdf_stem"]] = r.get("overall_state", "")
    groups = defaultdict(list)
    with SRC.open(newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f); cols = rd.fieldnames
        for r in rd:
            if state.get(r.get("pdf_stem", ""), "not_processed") in ELIGIBLE:
                groups[(r.get("collection",""), r.get("year",""), r.get("month",""))].append(r)
    rng = random.Random(13); out = []
    for key in sorted(groups):
        g = groups[key]; rng.shuffle(g)
        out.extend(g[:max(1, math.ceil(len(g) * a.pct / 100))])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(out)
    elig = sum(len(v) for v in groups.values())
    print(f"{len(out)} sampled (1%/month) from {len(groups)} month-folders "
          f"({elig:,} never-processed eligible) -> {a.out}")


if __name__ == "__main__":
    main()

"""build_campaign_index.py -- ordered index for the back-fill campaign.

Concatenates the requested collections IN PRIORITY ORDER (default 12,11,10,9)
into one index, skipping records already finished (any stage done/failed in
base processing_status.csv). The chunk-chain then processes them in that order,
crash-proof and resumable. Run on a QUIET machine (loads the big status once).

Usage: python build_campaign_index.py --collections 12,11,10,9
"""
import argparse, csv, re
from pathlib import Path

csv.field_size_limit(2_000_000)
OUT = Path(r"D:\project_outputs")
STAGES = ["latlong", "grid", "location", "county", "dot"]


def done_stems():
    done = set()
    p = OUT / "processing_status.csv"
    with p.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            s = r.get("pdf_stem", "")
            if not s:
                continue
            st = [(r.get(f"{x}_status") or "") for x in STAGES]
            if any(st) and all(v in ("done", "failed", "skipped") for v in st):
                done.add(s)
    # also skip anything already mapped (e.g. by the modern-text path) so the
    # slow grid pipeline doesn't re-process records that already have coords.
    dc = OUT / "dot_coordinates.csv"
    if dc.exists():
        with dc.open(newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                if (r.get("resolved_lat") or "").strip():
                    done.add(r.get("pdf_stem", ""))
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collections", default="12,11,10,9")
    ap.add_argument("--out", default=str(Path(r"D:\project_outputs_sample\campaign_index.csv")))
    a = ap.parse_args()
    order = [int(x) for x in a.collections.split(",")]
    done = done_stems()
    print(f"{len(done):,} records already finished (skipped)")

    buckets = {c: [] for c in order}
    with (OUT / "dataset_index.csv").open(newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f); cols = rd.fieldnames
        for r in rd:
            m = re.search(r"\((\d+)\)", r.get("collection", ""))
            if not m:
                continue
            c = int(m.group(1))
            if c in buckets and r.get("pdf_stem") not in done:
                buckets[c].append(r)
    out = []
    for c in order:
        out.extend(buckets[c])
        print(f"  C{c}: {len(buckets[c]):,} to process")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(out)
    print(f"campaign index: {len(out):,} records (order {order}) -> {a.out}")


if __name__ == "__main__":
    main()

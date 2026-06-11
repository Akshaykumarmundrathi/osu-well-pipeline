"""
extract_insights.py -- era-clustered learning from done + newly-done records
=============================================================================

Reads the master status CSV (run-scoped fast load) + metadata fingerprints
and produces INSIGHTS.md: what improved, what still fails, clustered by
era/collection/form-type — the feedback loop that turns every run into
pipeline tuning evidence.

Sections:
  1. Stage rates by collection (current truth)
  2. Redo delta — records that flipped failed->done since the last snapshot
  3. Failure clustering: error_type x collection x decade
  4. Grid-method evolution by era (anchor vs envelope vs full-page CV)
  5. Suspect-flag counts (ground-truth guards firing)
  6. Recommendations queue (auto-generated from thresholds)

Usage:
    python extract_insights.py                      # vs ps_checkpoint snapshot
    python extract_insights.py --baseline <csv>     # custom baseline
"""
import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
csv.field_size_limit(2_000_000)

STATUS   = Path(r"D:\project_outputs\processing_status.csv")
BASELINE = Path(r"D:\project_outputs_c2345\ps_checkpoint.csv")
OUT_MD   = Path(r"D:\project_outputs\INSIGHTS.md")
STAGES   = ("grid", "location", "county", "dot")


def load(p: Path) -> dict[str, dict]:
    out = {}
    with p.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            if r.get("grid_status") not in ("", "pending", None):
                out[r["pdf_stem"]] = r
    return out


def cnum(r) -> int:
    c = r.get("collection", "")
    try:
        return int(c.split("(")[1].split(")")[0])
    except (IndexError, ValueError):
        return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default=str(BASELINE))
    args = ap.parse_args()

    cur = load(STATUS)
    base = load(Path(args.baseline)) if Path(args.baseline).exists() else {}
    lines = ["# Pipeline Insights (auto-generated)", ""]

    # 1. stage rates by collection
    lines += ["## 1. Stage success by collection", "",
              "| coll | n | grid | location | county | dot |", "|--|--|--|--|--|--|"]
    by = defaultdict(list)
    for r in cur.values():
        by[cnum(r)].append(r)
    for c in sorted(by):
        rs = by[c]
        n = len(rs)
        cells = [f"{sum(1 for r in rs if r.get(s+'_status')=='done')*100//n}%"
                 for s in STAGES]
        lines.append(f"| C{c} | {n} | " + " | ".join(cells) + " |")

    # 2. redo delta
    flips = defaultdict(Counter)
    for stem, r in cur.items():
        b = base.get(stem)
        if not b:
            continue
        for s in STAGES:
            if b.get(s + "_status") == "failed" and r.get(s + "_status") == "done":
                flips[s][cnum(r)] += 1
    lines += ["", "## 2. Failed -> done since baseline", ""]
    for s in STAGES:
        tot = sum(flips[s].values())
        if tot:
            det = "  ".join(f"C{c}:{n}" for c, n in sorted(flips[s].items()))
            lines.append(f"- **{s}**: +{tot}  ({det})")

    # 3. failure clusters
    lines += ["", "## 3. Remaining failure clusters (top 15)", ""]
    clus = Counter()
    for r in cur.values():
        decade = (r.get("year") or "????")[:3] + "0s"
        for s in STAGES:
            if r.get(s + "_status") == "failed":
                clus[(s, r.get(s + "_error_type") or "?", f"C{cnum(r)}", decade)] += 1
    for (s, e, c, d), n in clus.most_common(15):
        lines.append(f"- {n:>4}  {s}/{e}  {c} {d}")

    # 4. grid method evolution
    lines += ["", "## 4. Grid methods by collection", ""]
    gm = defaultdict(Counter)
    for r in cur.values():
        if r.get("grid_status") == "done" and r.get("grid_method"):
            m = r["grid_method"]
            fam = ("envelope" if m.startswith("envelope")
                   else "anchor" if m.startswith("anchor")
                   else "fullpage_cv")
            gm[cnum(r)][fam] += 1
    for c in sorted(gm):
        det = "  ".join(f"{k}={v}" for k, v in gm[c].most_common())
        lines.append(f"- C{c}: {det}")

    # 5/6. recommendations
    lines += ["", "## 5. Auto-recommendations", ""]
    for c in sorted(by):
        rs = by[c]
        n = len(rs)
        loc = sum(1 for r in rs if r.get("location_status") == "done") * 100 // n
        dot = sum(1 for r in rs if r.get("dot_status") == "done") * 100 // n
        if n >= 50 and loc < 50:
            lines.append(f"- C{c}: location {loc}% — review str recipes / "
                         f"handwriting tier for this era")
        if n >= 50 and dot < 40:
            lines.append(f"- C{c}: dot {dot}% — U-Net labels needed for this "
                         f"grid style (inspect_dots.py)")

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:40]))
    print(f"\nfull report -> {OUT_MD}")


if __name__ == "__main__":
    main()

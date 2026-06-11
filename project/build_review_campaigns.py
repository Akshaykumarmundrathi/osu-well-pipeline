"""
build_review_campaigns.py -- issue-targeted manual review sampling
====================================================================

Builds stratified review manifests for the gaps found in the Jun 2026 runs.
Sampling is proportional to folder size (year/month), base density 1 PDF per
100 files in scope, randomised but seeded (reproducible). Campaigns:

| campaign      | scope                | density | why (measured evidence)          |
|---------------|----------------------|---------|----------------------------------|
| c8_layout     | C8 all (1980-82)     | 1/100   | grid 48%, loc 33% — worst CV era |
| c12_modern    | C12 all (2013-18)    | 1/100   | county 8%, lat/lon 37% vs ~100%  |
| c7_grid       | C7 all (1971-79)     | 1/300   | grid 69% — narrower question     |
| early30s_loc  | C2-C3 1930-1938      | 1/100   | location dip era (28-48%)        |
| c6_county     | C6 all (1961-70)     | 1/300   | county 39% with grid 97%         |

Every campaign shares the same annotation schema (see annotate_campaigns.py)
so insights aggregate; campaign-specific questions are extra columns.

Usage:
    python build_review_campaigns.py            # build all manifests
    python build_review_campaigns.py --campaign c8_layout
Output:
    D:\\review_campaigns\\{campaign}\\index.csv   (pdf paths + folder context)
    D:\\review_campaigns\\summary.txt
"""
import argparse
import csv
import random
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT_OUT = Path(r"D:\review_campaigns")
SEED     = 20260610

# campaign -> (collection numbers, year filter or None, 1/N density)
CAMPAIGNS: dict[str, tuple[list[int], tuple[int, int] | None, int]] = {
    "c8_layout":    ([8],     None,         100),
    "c12_modern":   ([12],    None,         100),
    "c7_grid":      ([7],     None,         300),
    "early30s_loc": ([2, 3],  (1930, 1938), 100),
    "c6_county":    ([6],     None,         300),
}


def sample_campaign(name: str, colls: list[int],
                    years: tuple[int, int] | None, density: int) -> list[dict]:
    rng = random.Random(f"{SEED}:{name}")
    picked: list[dict] = []
    for c in colls:
        root = Path(rf"D:\ExportedFolderContents ({c})")
        if not root.exists():
            continue
        for year_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if not year_dir.name.isdigit():
                continue
            y = int(year_dir.name)
            if years and not (years[0] <= y <= years[1]):
                continue
            for month_dir in sorted(p for p in year_dir.iterdir() if p.is_dir()):
                pdfs = list(month_dir.glob("*.pdf"))
                if not pdfs:
                    continue
                # Proportional: 1 per `density`, at least 1 only when the
                # folder itself is at least half a quota (keeps it sparse).
                k = len(pdfs) // density
                if k == 0 and len(pdfs) >= density // 2:
                    k = 1
                if k == 0:
                    continue
                for p in rng.sample(pdfs, min(k, len(pdfs))):
                    picked.append({
                        "pdf_path": str(p), "pdf_stem": p.stem,
                        "collection": c, "year": y, "month": month_dir.name,
                        "folder_total": len(pdfs),
                    })
    return picked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", default=None,
                    help="build only this campaign")
    args = ap.parse_args()

    ROOT_OUT.mkdir(parents=True, exist_ok=True)
    summary = []
    for name, (colls, years, density) in CAMPAIGNS.items():
        if args.campaign and name != args.campaign:
            continue
        rows = sample_campaign(name, colls, years, density)
        cdir = ROOT_OUT / name
        cdir.mkdir(parents=True, exist_ok=True)
        idx = cdir / "index.csv"
        with idx.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["pdf_path", "pdf_stem",
                                              "collection", "year", "month",
                                              "folder_total"])
            w.writeheader()
            w.writerows(rows)
        line = f"{name:<14} {len(rows):>5} files  (density 1/{density}, colls {colls}, years {years or 'all'})"
        summary.append(line)
        print(line)

    (ROOT_OUT / "summary.txt").write_text("\n".join(summary) + "\n",
                                          encoding="utf-8")
    print(f"\nManifests -> {ROOT_OUT}")
    print("Annotate with:  python annotate_campaigns.py --campaign <name>")


if __name__ == "__main__":
    main()

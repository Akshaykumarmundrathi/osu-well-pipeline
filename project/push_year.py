"""push_year.py -- publish the map after every (collection, year) completes.

For the campaign records, detects each (collection, year) group that is now
fully terminal (every record done/failed), resolves its grid-pipeline results
to coordinates, folds them NON-DESTRUCTIVELY into dot_coordinates, rebuilds the
map (monotonic), pushes, and refreshes the per-stage failure CSVs. Idempotent:
a `published_years.txt` marker prevents re-pushing the same year.

Low-memory: streams status once. Safe to run alongside the crash-proof chain
(the chain self-heals if a chunk is interrupted).

Usage: python push_year.py            (one pass; pushes any newly-complete years)
       python push_year.py --dry-run  (report only)
"""
import argparse, csv, os, re, subprocess, sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(2_000_000)
HERE = Path(__file__).parent
OUT = Path(r"D:\project_outputs")
CAMPAIGN_IDX = Path(r"D:\project_outputs_sample\campaign_index.csv")
MARKER = OUT / "published_years.txt"
STAGES = ["latlong", "grid", "location", "county", "dot"]

# load .env for RDS
_envf = HERE.parent / ".env"
if _envf.exists():
    for _l in _envf.read_text(encoding="utf-8", errors="replace").splitlines():
        _l = _l.strip()
        if "=" in _l and not _l.startswith("#"):
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
sys.path.insert(0, str(HERE))


def _grp(coll, year):
    m = re.search(r"\((\d+)\)", coll or "")
    return (f"C{m.group(1)}" if m else coll, year)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    # campaign stems -> (group); group -> set(stems)
    stem_grp, grp_stems = {}, defaultdict(set)
    with CAMPAIGN_IDX.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            g = _grp(r.get("collection", ""), r.get("year", ""))
            stem_grp[r["pdf_stem"]] = g
            grp_stems[g].add(r["pdf_stem"])

    # stream status: terminal? + resolution fields for campaign stems
    terminal = defaultdict(set)
    res_fields = {}
    files = [OUT / "processing_status.csv"] + list(OUT.glob("processing_status.*.csv"))
    for fp in files:
        if not fp.exists():
            continue
        with fp.open(newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                s = r.get("pdf_stem", "")
                g = stem_grp.get(s)
                if not g:
                    continue
                st = [(r.get(f"{x}_status") or "") for x in STAGES]
                if any(st) and all(v in ("done", "failed", "skipped") for v in st):
                    terminal[g].add(s)
                if r.get("dot_status") == "done":
                    res_fields[s] = r   # keep latest

    published = set(MARKER.read_text().split()) if MARKER.exists() else set()
    complete = [g for g, stems in grp_stems.items()
                if len(terminal[g]) >= len(stems) and f"{g[0]}|{g[1]}" not in published]
    complete.sort()
    print(f"{len(complete)} newly-complete year(s): "
          + ", ".join(f"{c}/{y}" for c, y in complete[:12]))
    if not complete or a.dry_run:
        return

    # resolve the new years' dot-done records
    from coord.plss_resolver import PLSSResolver
    R = PLSSResolver()

    def parse(v, dirs):
        m = re.match(r"(\d+)\s*([" + dirs + r"])", (v or "").upper())
        return (int(m.group(1)), m.group(2)) if m else (None, None)

    rows, resolved = [], 0
    for g in complete:
        for s in grp_stems[g]:
            r = res_fields.get(s)
            if not r:
                continue
            tw, ns = parse(r.get("location_township"), "NS")
            rg, ew = parse(r.get("location_range"), "EW")
            try:
                res = R.resolve(r.get("location_section"), tw, ns, rg, ew,
                                r.get("county_name", ""),
                                dot_row=r.get("dot_row") or 0,
                                dot_col=r.get("dot_col") or 0,
                                x_norm=r.get("dot_x_norm") or None,
                                y_norm=r.get("dot_y_norm") or None,
                                quadrant_label=r.get("location_quadrant_db") or None)
            except Exception:
                continue
            if res and res.get("lat") is not None and res.get("source") not in (
                    "rds_miss", "parse_failed", "bounds_invalid", None):
                rows.append({"pdf_stem": s, "collection": r.get("collection", ""),
                             "year": r.get("year", ""), "month": r.get("month", ""),
                             "county_name": r.get("county_name", ""),
                             "section": r.get("location_section", ""),
                             "township": r.get("location_township", ""),
                             "range": r.get("location_range", ""),
                             "resolved_lat": round(res["lat"], 7),
                             "resolved_lon": round(res["lon"], 7),
                             "resolution_source": res["source"]})
                resolved += 1
    print(f"resolved {resolved} grid records across {len(complete)} year(s)")

    if rows:
        ycsv = OUT / "year_coords.csv"
        with ycsv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        _union_into_dotcoords(ycsv)
        _build_and_push(complete)

    # mark published
    with MARKER.open("a", encoding="utf-8") as f:
        for c, y in complete:
            f.write(f"{c}|{y}\n")
    # refresh failure CSVs
    subprocess.run([sys.executable, str(HERE / "clean_failures.py")], cwd=str(HERE))


def _union_into_dotcoords(ycsv: Path):
    import shutil
    def load(p):
        with open(p, newline="", encoding="utf-8", errors="replace") as f:
            rd = csv.DictReader(f); return rd.fieldnames, list(rd)
    dcols, dc = load(OUT / "dot_coordinates.csv")
    ycols, yc = load(ycsv)
    allcols = list(dcols)
    for c in ycols:
        if c not in allcols:
            allcols.append(c)
    by = {r["pdf_stem"]: r for r in dc}
    added = 0
    for r in yc:
        s = r["pdf_stem"]
        if s in by and (by[s].get("resolved_lat") or "").strip():
            continue   # never overwrite an existing coordinate
        by[s] = r; added += 1
    shutil.copy2(OUT / "dot_coordinates.csv", OUT / "dot_coordinates.csv.preyear_bak")
    tmp = OUT / "dot_coordinates.csv.t"
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=allcols); w.writeheader()
        for r in by.values():
            w.writerow({k: r.get(k, "") for k in allcols})
    os.replace(tmp, OUT / "dot_coordinates.csv")
    print(f"  union: +{added} into dot_coordinates ({len(by):,} total)")


def _build_and_push(complete):
    repo = HERE.parent
    subprocess.run([sys.executable, str(HERE / "build_map_data.py"),
                    "--output", str(OUT)], cwd=str(HERE))
    years = ", ".join(f"{c}/{y}" for c, y in complete[:8])
    subprocess.run(["git", "add", "docs/data/well_locations.json"], cwd=str(repo))
    subprocess.run(["git", "commit", "-q", "-m",
                    f"data: per-year map publish ({years}{'...' if len(complete)>8 else ''})"],
                   cwd=str(repo))
    subprocess.run(["git", "pull", "--rebase"], cwd=str(repo))
    subprocess.run(["git", "push"], cwd=str(repo))
    print(f"  pushed map for {len(complete)} year(s)")


if __name__ == "__main__":
    main()

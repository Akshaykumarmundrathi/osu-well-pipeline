"""reconcile.py -- single source-of-truth ledger across all tracking files.

Joins the source universe (dataset_index.csv) with the status tracker
(processing_status.csv + any live shards), the coordinate output
(dot_coordinates.csv) and the published site (docs/data/well_locations.json),
and emits ONE row per source PDF with its complete state:

  master_ledger.csv   one row/stem: per-stage status, extracted fields,
                      image path, final coords, mapped flag, overall_state
  reconcile_report.md human-readable counts + orphan/mismatch lists

Read-only: never rewrites the master CSVs (the consolidate truncation bug
taught us to treat them as fragile).

Usage: python reconcile.py [--check-s3 N]   (N = sample size for S3 existence)
"""
import argparse, csv, glob, json, os, random
from pathlib import Path

csv.field_size_limit(2_000_000)
OUT   = Path(r"D:\project_outputs")
GEO   = Path(r"D:\project_modular\docs\data\well_locations.json")
STAGES = ["latlong", "grid", "location", "county", "dot"]


def _rows(p):
    if not os.path.exists(p):
        return []
    with open(p, newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _status_index():
    """stem -> status row, base master overlaid by any live shards (newest wins)."""
    idx = {}
    files = [OUT / "processing_status.csv"] + [Path(p) for p in
             glob.glob(str(OUT / "processing_status.*.csv"))]
    for fp in files:
        for r in _rows(fp):
            s = r.get("pdf_stem", "")
            if s:
                idx[s] = r
    return idx


def _overall(st: dict | None) -> tuple[str, str]:
    if st is None:
        return "not_processed", ""
    states = {s: (st.get(f"{s}_status") or "").strip() for s in STAGES}
    # first stage that failed
    for s in STAGES:
        if states[s] == "failed":
            return f"failed@{s}", s
    done = [s for s in STAGES if states[s] == "done"]
    if len(done) == len(STAGES):
        return "success", ""
    if done:
        return "partial", ""
    return "queued", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-s3", type=int, default=0)
    a = ap.parse_args()

    idx = _rows(OUT / "dataset_index.csv")
    status = _status_index()
    dc = {r.get("pdf_stem", ""): r for r in _rows(OUT / "dot_coordinates.csv")}
    geo = json.load(open(GEO, encoding="utf-8"))
    site = {f["properties"].get("pdf_stem", "") for f in geo["features"]}

    out_rows = []
    counts = {"not_processed": 0, "queued": 0, "partial": 0, "success": 0, "mapped": 0}
    fail_by_stage = {s: 0 for s in STAGES}
    src_stems = set()

    for r in idx:
        stem = r.get("pdf_stem", "")
        if not stem:
            continue
        src_stems.add(stem)
        st = status.get(stem)
        co = dc.get(stem, {})
        overall, fstage = _overall(st)
        if overall.startswith("failed@"):
            fail_by_stage[fstage] += 1
        mapped = "Y" if stem in site else "N"
        if overall in counts:
            counts[overall] += 1
        if mapped == "Y":
            counts["mapped"] += 1
        out_rows.append({
            "pdf_stem": stem,
            "collection": r.get("collection", ""), "year": r.get("year", ""),
            "month": r.get("month", ""), "pdf_path": r.get("pdf_path", ""),
            **{f"{s}_status": (st.get(f"{s}_status", "") if st else "") for s in STAGES},
            "first_failed_stage": fstage,
            "error": (st.get("dot_error_type") or st.get("county_error_type")
                      or st.get("location_error_type") or st.get("grid_error_type")
                      or "") if st else "",
            "section": st.get("location_section", "") if st else "",
            "township": st.get("location_township", "") if st else "",
            "range": st.get("location_range", "") if st else "",
            "county_name": st.get("county_name", "") if st else "",
            "latlong_lat": st.get("latlong_lat", "") if st else "",
            "latlong_lon": st.get("latlong_lon", "") if st else "",
            "grid_image_path": st.get("grid_image_path", "") if st else "",
            "resolved_lat": co.get("resolved_lat", ""),
            "resolved_lon": co.get("resolved_lon", ""),
            "resolution_source": co.get("resolution_source", ""),
            "mapped": mapped,
            "overall_state": overall,
        })

    cols = list(out_rows[0].keys())
    led = OUT / "master_ledger.csv"
    with led.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(out_rows)

    # orphans / mismatches
    orphan_status = sorted(set(status) - src_stems)
    orphan_site   = sorted(site - src_stems)
    site_unmapped = sorted(site - set(dc))

    # optional S3 sample check
    s3_note = ""
    if a.check_s3:
        try:
            import boto3, re
            cli = boto3.client("s3")
            bkt = os.environ.get("S3_BUCKET", "osu-well-records-225989338968")
            sample = random.Random(1).sample(out_rows, min(a.check_s3, len(out_rows)))
            miss = 0
            for r in sample:
                cn = re.search(r"\((\d+)\)", r["collection"])
                if not cn:
                    continue
                key = (f"pdfs/ExportedFolderContents_{cn.group(1)}/"
                       f"{r['year']}/{r['month']}/{r['pdf_stem']}.pdf")
                try:
                    cli.head_object(Bucket=bkt, Key=key)
                except Exception:
                    miss += 1
            s3_note = f"S3 sample {len(sample)}: {miss} missing"
        except Exception as exc:
            s3_note = f"S3 check skipped: {exc}"

    total = len(out_rows)
    rep = [f"# Reconciliation report", "",
           f"Source universe (dataset_index): **{total:,}**", "",
           "## Overall state",
           f"- not_processed : {counts['not_processed']:,}",
           f"- queued        : {counts['queued']:,}",
           f"- partial       : {counts['partial']:,}",
           f"- success (all 5 stages): {counts['success']:,}",
           f"- **mapped on site**    : {counts['mapped']:,}", "",
           "## Failures by first-failed stage"]
    rep += [f"- {s}: {fail_by_stage[s]:,}" for s in STAGES]
    rep += ["", "## Cross-source integrity",
            f"- status stems NOT in source (orphans): {len(orphan_status):,}",
            f"- site wells NOT in source (orphans)  : {len(orphan_site):,}",
            f"- site wells NOT in dot_coordinates   : {len(site_unmapped):,}",
            f"- live shards folded into view: "
            f"{[os.path.basename(p) for p in glob.glob(str(OUT/'processing_status.*.csv'))]}"]
    if s3_note:
        rep += ["", f"## S3", f"- {s3_note}"]
    if orphan_status[:20]:
        rep += ["", "## Sample orphan status stems",
                *[f"- {s}" for s in orphan_status[:20]]]
    (OUT / "reconcile_report.md").write_text("\n".join(rep) + "\n", encoding="utf-8")
    print("\n".join(rep))
    print(f"\nledger -> {led}")


if __name__ == "__main__":
    main()

"""
analyze_pipeline_output.py
--------------------------
Aggregates ALL completed S3 slice results into:

  OUTPUT FILES (uploaded to s3://…/analysis/):
    success.csv            — every PDF with valid lat/lon (full detail)
    failure_all.csv        — every failed PDF with root cause
    failure_grid.csv       — grid detection failures
    failure_location.csv   — PLSS location (section/township/range) failures
    failure_county.csv     — county extraction failures
    failure_dot.csv        — dot detection failures
    failure_latlong.csv    — embedded lat/long field failures
    failure_bounds.csv     — lat/lon resolved but outside Oklahoma
    pipeline_summary.json  — aggregate error rates, counts, percentages

  LOCAL: D:/project_modular/visualizer/analysis/

Usage:
    python analyze_pipeline_output.py              # incremental (new slices only)
    python analyze_pipeline_output.py --force      # reprocess all slices
    python analyze_pipeline_output.py --no-upload  # local only
"""

import boto3, csv, io, json, os, sys
from datetime import datetime, timezone
from collections import defaultdict

# ─── Config ───────────────────────────────────────────────────────────────────
BUCKET       = "osu-well-records-225989338968"
RESULTS_PFX  = "results/"
ANALYSIS_PFX = "analysis/"
OUT_DIR      = os.path.join(os.path.dirname(__file__), "analysis")
STATE_FILE   = os.path.join(OUT_DIR, "analysis_state.json")
REGION       = "us-east-1"

# Oklahoma bounding box
LAT_MIN, LAT_MAX = 33.5, 37.1
LON_MIN, LON_MAX = -103.1, -94.4

# ─── Failure root-cause hierarchy ─────────────────────────────────────────────
# Priority order: the FIRST failing stage that prevented lat/lon output
STAGE_PRIORITY = ["latlong", "grid", "location", "dot", "county", "bounds"]

FAILURE_DOMAINS = {
    "latlong":  "Lat/Long field parse failure (embedded coordinates in PDF)",
    "grid":     "Grid/map image not detected or extracted",
    "location": "PLSS location not found (section/township/range missing)",
    "dot":      "Well dot not detected on grid image",
    "county":   "County extraction failed (Gemini/OCR error)",
    "bounds":   "Coordinates resolved but outside Oklahoma bounding box",
    "open":     "PDF could not be opened / read error",
    "unknown":  "Unclassified failure",
}

SUCCESS_FIELDS = [
    "pdf_stem","pdf_path","collection","year","month","model_tier","decade",
    "section","township","range","county_name",
    "resolved_lat","resolved_lon","resolution_source","dot_confidence",
    "ocr_quadrant_db","unet_nw","effective_quad","quadrant_source","flags",
    "grid_confidence","location_confidence","county_confidence",
]

FAILURE_FIELDS = [
    "pdf_stem","pdf_path","collection","year","month","model_tier","decade",
    "failure_domain","failure_stage","failure_error_type","failure_detail",
    "grid_status","grid_error_type","grid_confidence",
    "location_status","location_error_type","location_section","location_township","location_range",
    "county_status","county_error_type","county_name",
    "dot_status","dot_error_type","dot_confidence",
    "latlong_status","latlong_error_type",
    "resolution_source","flags",
]

os.makedirs(OUT_DIR, exist_ok=True)


# ─── S3 helpers ───────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"processed_slices": [], "last_run_utc": None}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def list_completed_slices(s3):
    """Return list of (prefix, slice_name) tuples that have job_status.json."""
    paginator = s3.get_paginator("list_objects_v2")
    completed = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=RESULTS_PFX, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            pfx = cp["Prefix"]
            name = pfx.rstrip("/").split("/")[-1]
            if not name.startswith("slice-"):
                continue
            try:
                s3.head_object(Bucket=BUCKET, Key=pfx + "job_status.json")
                completed.append((pfx, name))
            except Exception:
                pass
    return sorted(completed)

def read_s3_csv(s3, key):
    """Download a CSV from S3, return list of dicts. Empty list on error."""
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        text = obj["Body"].read().decode("utf-8", errors="replace")
        return list(csv.DictReader(io.StringIO(text)))
    except Exception:
        return []

def read_s3_json(s3, key):
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return {}


# ─── Classification logic ──────────────────────────────────────────────────────
def classify_failure(row_proc, row_dot=None):
    """
    Determine root cause from processing_status.csv row (and optionally
    the dot_coordinates.csv row if the PDF made it that far).

    Returns (domain, stage, error_type, detail)
    """
    # 1. Lat/long field failure (rare — embedded coords in PDF)
    ll_status = row_proc.get("latlong_status","")
    if ll_status == "failed":
        return ("latlong", "latlong", row_proc.get("latlong_error_type",""), "Embedded lat/long parse failed")

    # 2. Grid not detected
    g_status = row_proc.get("grid_status","")
    g_err    = row_proc.get("grid_error_type","")
    if g_status == "failed":
        return ("grid", "grid", g_err, f"Grid image extraction failed: {g_err}")

    # 3. Location (PLSS) not found
    l_status = row_proc.get("location_status","")
    l_err    = row_proc.get("location_error_type","")
    if l_status == "failed":
        sec = row_proc.get("location_section","")
        twp = row_proc.get("location_township","")
        rng = row_proc.get("location_range","")
        missing = [x for x,v in [("section",sec),("township",twp),("range",rng)] if not v]
        detail = f"PLSS not found: missing {', '.join(missing)}" if missing else f"Location failed: {l_err}"
        return ("location", "location", l_err, detail)

    # 4. Dot not detected
    d_status = row_proc.get("dot_status","")
    d_err    = row_proc.get("dot_error_type","")
    if d_status == "failed":
        return ("dot", "dot", d_err, f"Well dot not detected: {d_err}")

    # 5. Bounds invalid (made it to dot_coordinates but lat/lon outside OK)
    if row_dot is not None:
        res_src = row_dot.get("resolution_source","")
        if "bounds_invalid" in res_src or "invalid" in res_src.lower():
            flags = row_dot.get("flags","")
            return ("bounds", "resolution", "bounds_invalid", f"Resolved coords outside Oklahoma: {flags}")
        if not row_dot.get("resolved_lat"):
            return ("bounds", "resolution", "no_coords", "Lat/lon could not be resolved")

    # 6. County failure (soft — doesn't always block lat/lon)
    c_status = row_proc.get("county_status","")
    c_err    = row_proc.get("county_error_type","")
    if c_status == "failed":
        return ("county", "county", c_err, f"County extraction failed: {c_err}")

    return ("unknown", "unknown", "", "Unclassified failure")


def is_valid_latlon(lat_str, lon_str):
    try:
        lat = float(lat_str)
        lon = float(lon_str)
        return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX
    except (ValueError, TypeError):
        return False


# ─── Main aggregation ─────────────────────────────────────────────────────────
def main():
    force     = "--force"     in sys.argv
    no_upload = "--no-upload" in sys.argv

    s3    = boto3.client("s3", region_name=REGION)
    state = load_state()

    already_done = set(state.get("processed_slices", []))
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"[{ts_now}] Analyzing pipeline output...")
    print(f"  Already processed: {len(already_done)} slices")

    all_slices = list_completed_slices(s3)
    to_process = [(pfx, name) for pfx, name in all_slices
                  if force or name not in already_done]

    print(f"  Total completed:   {len(all_slices)} slices")
    print(f"  New to process:    {len(to_process)} slices")

    if not to_process and not force:
        print("  Nothing new — loading existing CSVs for summary.")

    # ── Accumulators ──────────────────────────────────────────────────────────
    success_rows   = []
    failure_rows   = []
    domain_buckets = defaultdict(list)  # domain -> list of failure rows
    slice_stats    = []

    # Load existing CSVs if incremental
    def load_existing_csv(filename, fields):
        path = os.path.join(OUT_DIR, filename)
        if os.path.exists(path) and not force:
            with open(path, encoding="utf-8") as f:
                return list(csv.DictReader(f))
        return []

    if not force:
        success_rows   = load_existing_csv("success.csv", SUCCESS_FIELDS)
        failure_rows   = load_existing_csv("failure_all.csv", FAILURE_FIELDS)
        for domain in FAILURE_DOMAINS:
            domain_buckets[domain] = load_existing_csv(f"failure_{domain}.csv", FAILURE_FIELDS)

    # ── Process each new slice ─────────────────────────────────────────────────
    for i, (pfx, name) in enumerate(to_process):
        print(f"  [{i+1}/{len(to_process)}] {name}...", flush=True)

        # Load processing_status.csv (per-PDF per-stage status)
        proc_rows = read_s3_csv(s3, pfx + "processing_status.csv")
        # Load dot_coordinates.csv (only successfully resolved PDFs)
        dot_rows  = read_s3_csv(s3, pfx + "dot_coordinates.csv")
        dot_index = {r["pdf_stem"]: r for r in dot_rows}

        # Load run_insights for slice-level stats
        insights = read_s3_json(s3, pfx + "run_insights.json")

        slice_total = len(proc_rows)
        slice_ok    = 0
        slice_fail  = 0

        for row in proc_rows:
            stem = row.get("pdf_stem","")
            dot_row = dot_index.get(stem)

            # Determine if this PDF succeeded
            has_coords = dot_row and is_valid_latlon(
                dot_row.get("resolved_lat",""), dot_row.get("resolved_lon","")
            )
            res_src = (dot_row or {}).get("resolution_source","")
            is_success = has_coords and "bounds_invalid" not in res_src and "invalid" not in res_src.lower()

            if is_success:
                slice_ok += 1
                s_row = {f: "" for f in SUCCESS_FIELDS}
                s_row.update({
                    "pdf_stem":          dot_row.get("pdf_stem",""),
                    "pdf_path":          dot_row.get("pdf_path",""),
                    "collection":        dot_row.get("collection","").replace("ExportedFolderContents_",""),
                    "year":              dot_row.get("year",""),
                    "month":             dot_row.get("month",""),
                    "model_tier":        dot_row.get("model_tier",""),
                    "decade":            dot_row.get("decade",""),
                    "section":           dot_row.get("section",""),
                    "township":          dot_row.get("township",""),
                    "range":             dot_row.get("range",""),
                    "county_name":       dot_row.get("county_name",""),
                    "resolved_lat":      dot_row.get("resolved_lat",""),
                    "resolved_lon":      dot_row.get("resolved_lon",""),
                    "resolution_source": dot_row.get("resolution_source",""),
                    "dot_confidence":    dot_row.get("dot_confidence",""),
                    "ocr_quadrant_db":   dot_row.get("ocr_quadrant_db",""),
                    "unet_nw":           dot_row.get("unet_nw",""),
                    "effective_quad":    dot_row.get("effective_quad",""),
                    "quadrant_source":   dot_row.get("quadrant_source",""),
                    "flags":             dot_row.get("flags",""),
                    "grid_confidence":   row.get("grid_confidence",""),
                    "location_confidence": row.get("location_confidence",""),
                    "county_confidence": row.get("county_confidence",""),
                })
                success_rows.append(s_row)

            else:
                slice_fail += 1
                domain, stage, err_type, detail = classify_failure(row, dot_row)

                f_row = {f: "" for f in FAILURE_FIELDS}
                f_row.update({
                    "pdf_stem":            stem,
                    "pdf_path":            row.get("pdf_path",""),
                    "collection":          row.get("collection","").replace("ExportedFolderContents_",""),
                    "year":                row.get("year",""),
                    "month":               row.get("month",""),
                    "model_tier":          row.get("model_tier",""),
                    "decade":              row.get("decade",""),
                    "failure_domain":      domain,
                    "failure_stage":       stage,
                    "failure_error_type":  err_type,
                    "failure_detail":      detail,
                    "grid_status":         row.get("grid_status",""),
                    "grid_error_type":     row.get("grid_error_type",""),
                    "grid_confidence":     row.get("grid_confidence",""),
                    "location_status":     row.get("location_status",""),
                    "location_error_type": row.get("location_error_type",""),
                    "location_section":    row.get("location_section",""),
                    "location_township":   row.get("location_township",""),
                    "location_range":      row.get("location_range",""),
                    "county_status":       row.get("county_status",""),
                    "county_error_type":   row.get("county_error_type",""),
                    "county_name":         row.get("county_name",""),
                    "dot_status":          row.get("dot_status",""),
                    "dot_error_type":      row.get("dot_error_type",""),
                    "dot_confidence":      row.get("dot_confidence",""),
                    "latlong_status":      row.get("latlong_status",""),
                    "latlong_error_type":  row.get("latlong_error_type",""),
                    "resolution_source":   (dot_row or {}).get("resolution_source",""),
                    "flags":               (dot_row or {}).get("flags",""),
                })
                failure_rows.append(f_row)
                domain_buckets[domain].append(f_row)

        already_done.add(name)
        slice_stats.append({
            "slice":   name,
            "total":   slice_total,
            "success": slice_ok,
            "failure": slice_fail,
            "rate":    round(100 * slice_ok / max(1, slice_total), 1),
            "insights": {
                stage: {
                    "detected": info.get("detected",0),
                    "failed":   info.get("failed",0),
                    "by_error": info.get("by_error",{}),
                }
                for stage, info in insights.get("stages",{}).items()
            }
        })

    # ── Aggregate summary ──────────────────────────────────────────────────────
    total_pdfs    = len(success_rows) + len(failure_rows)
    total_success = len(success_rows)
    total_failure = len(failure_rows)
    success_rate  = round(100 * total_success / max(1, total_pdfs), 2)
    failure_rate  = round(100 * total_failure / max(1, total_pdfs), 2)

    # Stage-level rates (from all collected failure rows)
    stage_counts = defaultdict(int)
    error_type_counts = defaultdict(lambda: defaultdict(int))
    for r in failure_rows:
        stage_counts[r["failure_domain"]] += 1
        error_type_counts[r["failure_domain"]][r["failure_error_type"]] += 1

    # Resolution source breakdown from success rows
    resolution_breakdown = defaultdict(int)
    for r in success_rows:
        resolution_breakdown[r["resolution_source"]] += 1

    summary = {
        "generated_utc":    ts_now,
        "slices_analyzed":  len(already_done),
        "total_pdfs":       total_pdfs,
        "total_success":    total_success,
        "total_failure":    total_failure,
        "success_rate_pct": success_rate,
        "failure_rate_pct": failure_rate,
        "failure_by_domain": {
            domain: {
                "count":   stage_counts.get(domain, 0),
                "pct_of_failures": round(100 * stage_counts.get(domain,0) / max(1, total_failure), 1),
                "pct_of_total":    round(100 * stage_counts.get(domain,0) / max(1, total_pdfs), 1),
                "description": FAILURE_DOMAINS.get(domain,""),
                "error_types": dict(error_type_counts.get(domain,{})),
            }
            for domain in FAILURE_DOMAINS
        },
        "resolution_source_breakdown": dict(resolution_breakdown),
        "plausibility": {
            "quadrant_direct_pct": round(100*resolution_breakdown.get("quadrant_direct",0)/max(1,total_success),1),
            "rds_lookup_pct":      round(100*resolution_breakdown.get("rds_lookup",0)/max(1,total_success),1),
            "note": "quadrant_direct = full 8-level PLSS resolution; rds_lookup = county-centroid fallback",
        },
        "error_plausibility_assessment": _plausibility_narrative(
            total_pdfs, success_rate, stage_counts, total_failure
        ),
    }

    # ── Write local CSVs ───────────────────────────────────────────────────────
    def write_csv(filename, rows, fields):
        path = os.path.join(OUT_DIR, filename)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        sz = os.path.getsize(path)
        print(f"  Wrote {filename}  ({len(rows):,} rows, {sz//1024} KB)")
        return path

    print("\nWriting output files...")
    write_csv("success.csv",          success_rows, SUCCESS_FIELDS)
    write_csv("failure_all.csv",      failure_rows, FAILURE_FIELDS)
    for domain in FAILURE_DOMAINS:
        if domain_buckets.get(domain):
            write_csv(f"failure_{domain}.csv", domain_buckets[domain], FAILURE_FIELDS)

    summary_path = os.path.join(OUT_DIR, "pipeline_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"  Wrote pipeline_summary.json")

    # ── Upload to S3 ──────────────────────────────────────────────────────────
    if not no_upload:
        print("\nUploading to S3...")
        files_to_upload = [
            ("success.csv",           "text/csv"),
            ("failure_all.csv",       "text/csv"),
            ("pipeline_summary.json", "application/json"),
        ]
        for domain in FAILURE_DOMAINS:
            fn = f"failure_{domain}.csv"
            if os.path.exists(os.path.join(OUT_DIR, fn)):
                files_to_upload.append((fn, "text/csv"))

        for fname, ctype in files_to_upload:
            local = os.path.join(OUT_DIR, fname)
            if os.path.exists(local):
                with open(local, "rb") as fh:
                    s3.put_object(Bucket=BUCKET, Key=ANALYSIS_PFX+fname,
                                  Body=fh, ContentType=ctype)
                print(f"  → s3://{BUCKET}/{ANALYSIS_PFX}{fname}")

    # ── Save state ────────────────────────────────────────────────────────────
    state["processed_slices"] = list(already_done)
    state["last_run_utc"]     = ts_now
    state["last_summary"]     = {
        "total_pdfs":    total_pdfs,
        "success":       total_success,
        "failure":       total_failure,
        "success_rate":  success_rate,
        "slices":        len(already_done),
    }
    save_state(state)

    # ── Print summary ──────────────────────────────────────────────────────────
    sep = "=" * 56
    lines = [
        sep,
        f"  Pipeline Output Analysis -- {ts_now[:10]}",
        f"  Slices analyzed : {len(already_done):,}",
        f"  Total PDFs      : {total_pdfs:,}",
        f"  SUCCESS         : {total_success:,}  ({success_rate}%)",
        f"  FAILURE         : {total_failure:,}  ({failure_rate}%)",
        "",
        "  Failure breakdown:",
        _format_domain_table(stage_counts, total_failure),
        "",
        "  Resolution quality (successes only):",
        f"    quadrant_direct  : {resolution_breakdown.get('quadrant_direct',0):,}  ({summary['plausibility']['quadrant_direct_pct']}%)",
        f"    rds_lookup       : {resolution_breakdown.get('rds_lookup',0):,}  ({summary['plausibility']['rds_lookup_pct']}%)",
        sep,
    ]
    print("\n".join(lines).encode("ascii", errors="replace").decode("ascii"))

    return summary


def _format_domain_table(stage_counts, total_failure):
    lines = []
    for domain, desc in FAILURE_DOMAINS.items():
        c = stage_counts.get(domain, 0)
        if c:
            pct = 100 * c / max(1, total_failure)
            lines.append(f"    {domain:12s}: {c:5,}  ({pct:.1f}% of failures)  — {desc[:45]}")
    return "\n".join(lines) if lines else "    (none)"


def _plausibility_narrative(total, success_rate, stage_counts, total_failure):
    """Generate a plain-English error plausibility assessment."""
    loc_fail  = stage_counts.get("location", 0)
    dot_fail  = stage_counts.get("dot", 0)
    cty_fail  = stage_counts.get("county", 0)
    grid_fail = stage_counts.get("grid", 0)
    bnd_fail  = stage_counts.get("bounds", 0)

    loc_pct  = 100 * loc_fail  / max(1, total_failure)
    dot_pct  = 100 * dot_fail  / max(1, total_failure)
    cty_pct  = 100 * cty_fail  / max(1, total_failure)
    grid_pct = 100 * grid_fail / max(1, total_failure)

    lines = [
        f"Overall pipeline success rate: {success_rate}% of {total:,} PDFs produced a valid Oklahoma coordinate.",
        "",
        "Error plausibility assessment:",
        f"  • LOCATION failures ({loc_pct:.0f}% of failures): Section/Township/Range not extracted from PDF text.",
        f"    These are historically expected — early (pre-1940) documents often omit formal PLSS notation.",
        f"    Root causes: abbreviated forms, non-standard layout, or handwritten text beyond OCR capability.",
        f"  • DOT failures ({dot_pct:.0f}% of failures): UNet model did not detect a well dot on the grid.",
        f"    Expected for poor-quality scans, faded ink, or grids without a clear dot marker.",
        f"  • COUNTY failures ({cty_pct:.0f}% of failures): Gemini extraction failed or returned exception.",
        f"    Most common cause: Gemini API quota exhaustion mid-slice (recoverable on re-run).",
        f"    Note: county failure does NOT block lat/lon resolution — it is supplementary metadata.",
        f"  • GRID failures ({grid_pct:.0f}% of failures): Grid image could not be extracted from the PDF page.",
        f"    Caused by non-standard page layouts, blank pages, or fully scanned text-only documents.",
        f"  • BOUNDS failures: Coordinates were mathematically resolved but placed outside Oklahoma.",
        f"    Indicates an incorrect Township/Range value in the source document (data entry error).",
        "",
        "Manual review priority: location + dot failures with grid_confidence > 90 and dot_confidence > 80",
        "are the most likely to be recoverable with additional preprocessing or prompt tuning.",
    ]
    return lines


if __name__ == "__main__":
    main()

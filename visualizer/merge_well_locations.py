"""
merge_well_locations.py
-----------------------
Merges dot_coordinates.csv from ALL completed S3 slices into:
  - well_locations.json  (GeoJSON FeatureCollection — used by the map viewer)
  - well_locations.csv   (flat CSV for analysis)

Uploads both files to:
  s3://osu-well-records-225989338968/viewer/well_locations.json
  s3://osu-well-records-225989338968/viewer/well_locations.csv

Run after quota reset or at any time to refresh the dataset.
"""

import boto3, csv, json, io, sys, os
from datetime import datetime

BUCKET      = "osu-well-records-225989338968"
RESULTS_PFX = "results/"
OUT_JSON    = "viewer/well_locations.json"
OUT_CSV     = "viewer/well_locations.csv"

# Oklahoma bounding box (generous)
LAT_MIN, LAT_MAX = 33.5, 37.1
LON_MIN, LON_MAX = -103.1, -94.4

FIELDS_KEEP = [
    "pdf_stem", "pdf_path", "collection", "year", "month",
    "model_tier", "decade", "section", "township", "range",
    "county_name", "resolved_lat", "resolved_lon",
    "resolution_source", "dot_confidence",
    "ocr_quadrant_db", "unet_nw", "flags"
]

def list_completed_slices(s3):
    """Return list of slice prefixes that have job_status.json."""
    paginator = s3.get_paginator("list_objects_v2")
    slices = []
    print("Listing S3 results prefix...", flush=True)
    for page in paginator.paginate(Bucket=BUCKET, Prefix=RESULTS_PFX, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            pfx = cp["Prefix"]  # e.g. results/slice-00007/
            slices.append(pfx)
    print(f"  Found {len(slices)} slice directories", flush=True)

    completed = []
    for pfx in sorted(slices):
        key = pfx + "job_status.json"
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
            completed.append(pfx)
        except s3.exceptions.ClientError:
            pass
    print(f"  {len(completed)} completed (have job_status.json)", flush=True)
    return completed


def download_dot_coords(s3, prefix):
    """Download dot_coordinates.csv for a slice, return list of dicts."""
    key = prefix + "dot_coordinates.csv"
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        text = obj["Body"].read().decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        return list(reader)
    except Exception:
        return []


def is_valid(row):
    """Return True if row has a real resolved lat/lon inside Oklahoma."""
    try:
        lat = float(row.get("resolved_lat", "") or "")
        lon = float(row.get("resolved_lon", "") or "")
        return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX
    except (ValueError, TypeError):
        return False


def extract_well_name(pdf_stem):
    """Pull human-readable well name from stem like '11117524_A FRANKLIN 4_1031125'."""
    parts = pdf_stem.split("_")
    if len(parts) >= 2:
        return parts[1]          # middle segment is well name
    return pdf_stem


def build_feature(row):
    """Convert a dot_coordinates row to a GeoJSON Feature."""
    lat = float(row["resolved_lat"])
    lon = float(row["resolved_lon"])

    # Clean collection number
    col_raw = row.get("collection", "")
    col_num = col_raw.replace("ExportedFolderContents_", "") if col_raw else "?"

    # Parse confidence safely
    try:
        confidence = int(float(row.get("dot_confidence", 0) or 0))
    except (ValueError, TypeError):
        confidence = 0

    props = {
        "well_name":   extract_well_name(row.get("pdf_stem", "")),
        "pdf_stem":    row.get("pdf_stem", ""),
        "pdf_path":    row.get("pdf_path", ""),
        "collection":  col_num,
        "year":        row.get("year", ""),
        "month":       row.get("month", ""),
        "decade":      row.get("decade", ""),
        "model_tier":  row.get("model_tier", ""),
        "county":      row.get("county_name", ""),
        "section":     row.get("section", ""),
        "township":    row.get("township", ""),
        "range":       row.get("range", ""),
        "lat":         lat,
        "lon":         lon,
        "resolution":  row.get("resolution_source", ""),
        "confidence":  confidence,
        "quadrant":    row.get("ocr_quadrant_db", "") or row.get("unet_nw", ""),
        "flags":       row.get("flags", ""),
    }

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props
    }


def main():
    s3 = boto3.client("s3", region_name="us-east-1")
    completed = list_completed_slices(s3)

    features = []
    csv_rows  = []
    skipped   = 0

    for i, pfx in enumerate(completed):
        rows = download_dot_coords(s3, pfx)
        slice_ok = 0
        for row in rows:
            if is_valid(row):
                features.append(build_feature(row))
                # Flat CSV row
                flat = {f: row.get(f, "") for f in FIELDS_KEEP}
                flat["well_name"] = extract_well_name(row.get("pdf_stem", ""))
                csv_rows.append(flat)
                slice_ok += 1
            else:
                skipped += 1
        if (i + 1) % 20 == 0 or i == len(completed) - 1:
            print(f"  [{i+1}/{len(completed)}] {pfx.strip('/')} -> {slice_ok} coords  |  total so far: {len(features)}", flush=True)

    print(f"\nTotal valid well locations: {len(features)}", flush=True)
    print(f"Skipped (no valid coords):  {skipped}", flush=True)

    # Build GeoJSON
    geojson = {
        "type": "FeatureCollection",
        "generated": datetime.utcnow().isoformat() + "Z",
        "well_count": len(features),
        "features": features
    }

    # Save locally first
    local_json = os.path.join(os.path.dirname(__file__), "well_locations.json")
    local_csv  = os.path.join(os.path.dirname(__file__), "well_locations.csv")

    with open(local_json, "w", encoding="utf-8") as f:
        json.dump(geojson, f, separators=(",", ":"))
    print(f"Saved locally:  {local_json}  ({os.path.getsize(local_json)//1024} KB)", flush=True)

    # Upload GeoJSON to S3 (private — access via presigned URL or standalone HTML)
    json_bytes = open(local_json, "rb").read()
    s3.put_object(Bucket=BUCKET, Key=OUT_JSON, Body=json_bytes, ContentType="application/json")
    # Generate 7-day presigned URL
    url = s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": OUT_JSON}, ExpiresIn=604800)
    print(f"Uploaded GeoJSON → s3://{BUCKET}/{OUT_JSON}  ({len(json_bytes)//1024} KB)", flush=True)
    print(f"Presigned URL (7d): {url}", flush=True)

    # Upload CSV
    if csv_rows:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["well_name"] + FIELDS_KEEP)
        writer.writeheader()
        writer.writerows(csv_rows)
        csv_bytes = buf.getvalue().encode("utf-8")
        with open(local_csv, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
        s3.put_object(Bucket=BUCKET, Key=OUT_CSV, Body=csv_bytes, ContentType="text/csv")
        print(f"Uploaded CSV    → s3://{BUCKET}/{OUT_CSV}  ({len(csv_bytes)//1024} KB)", flush=True)

    print(f"\nDone. {len(features)} wells with verified Oklahoma coordinates.", flush=True)
    print(f"Map viewer URL: https://{BUCKET}.s3.amazonaws.com/viewer/well_map.html", flush=True)
    return len(features)


if __name__ == "__main__":
    main()

"""
auto_refresh_map.py
-------------------
Checks S3 for newly completed pipeline slices since the last run.
If new completions are found, merges all dot_coordinates.csv files into a
fresh well_locations.json and uploads it to the public S3 viewer prefix.
The live map at:
  https://osu-well-records-225989338968.s3.amazonaws.com/viewer/well_map.html
polls for this file every 10 minutes and auto-reloads when the timestamp changes.

Usage:
  python auto_refresh_map.py            # check and refresh if needed
  python auto_refresh_map.py --force    # always refresh regardless of new slices
  python auto_refresh_map.py --status   # print current state and exit

State is saved to: D:/project_modular/visualizer/refresh_state.json
"""

import boto3, json, os, sys, subprocess
from datetime import datetime, timezone

BUCKET       = "osu-well-records-225989338968"
RESULTS_PFX  = "results/"
STATE_FILE   = os.path.join(os.path.dirname(__file__), "refresh_state.json")
MERGE_SCRIPT   = os.path.join(os.path.dirname(__file__), "merge_well_locations.py")
ANALYZE_SCRIPT = os.path.join(os.path.dirname(__file__), "analyze_pipeline_output.py")
MAP_HTML     = os.path.join(os.path.dirname(__file__), "well_map.html")
REGION       = "us-east-1"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_completed_count": 0, "last_refresh_utc": None, "last_well_count": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def count_completed_slices(s3):
    """Count S3 slice directories that have a job_status.json (completed)."""
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=BUCKET, Prefix=RESULTS_PFX, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            pfx = cp["Prefix"]
            try:
                s3.head_object(Bucket=BUCKET, Key=pfx + "job_status.json")
                count += 1
            except Exception:
                pass
    return count


def upload_map_html(s3):
    """Upload well_map.html to S3 public viewer prefix."""
    if not os.path.exists(MAP_HTML):
        print(f"  Skipping HTML upload: {MAP_HTML} not found")
        return
    with open(MAP_HTML, "rb") as f:
        s3.put_object(
            Bucket=BUCKET,
            Key="viewer/well_map.html",
            Body=f,
            ContentType="text/html",
        )
    size = os.path.getsize(MAP_HTML)
    print(f"  Uploaded viewer/well_map.html  ({size // 1024} KB)")


def run_merge(force=False):
    """Run merge_well_locations.py to rebuild well_locations.json from S3."""
    print(f"  Running merge_well_locations.py...")
    result = subprocess.run(
        [sys.executable, MERGE_SCRIPT],
        cwd=os.path.dirname(__file__),
        capture_output=False,
    )
    return result.returncode == 0


def main():
    force  = "--force"  in sys.argv
    status = "--status" in sys.argv

    s3    = boto3.client("s3", region_name=REGION)
    state = load_state()

    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{ts_now}] auto_refresh_map.py")
    print(f"  Last refresh:       {state.get('last_refresh_utc', 'never')}")
    print(f"  Last slice count:   {state.get('last_completed_count', 0)}")
    print(f"  Last well count:    {state.get('last_well_count', 0)}")

    if status:
        return

    # Count current completed slices
    print("  Counting completed S3 slices...", flush=True)
    current_count = count_completed_slices(s3)
    prev_count    = state.get("last_completed_count", 0)
    new_slices    = current_count - prev_count

    print(f"  Completed slices:   {current_count}  (new since last refresh: {new_slices})")

    if new_slices <= 0 and not force:
        print("  No new completions — skipping refresh.")
        return

    print(f"  {'Forced refresh' if force else f'{new_slices} new slices'} — rebuilding well_locations.json ...")

    ok = run_merge()
    if not ok:
        print("  ERROR: merge_well_locations.py failed — aborting.")
        return

    # Run incremental failure analysis (only new slices)
    print(f"  Running analyze_pipeline_output.py (incremental)...")
    subprocess.run(
        [sys.executable, ANALYZE_SCRIPT],
        cwd=os.path.dirname(__file__),
        capture_output=False,
    )

    # Read the well count from the freshly written JSON
    local_json = os.path.join(os.path.dirname(__file__), "well_locations.json")
    well_count = 0
    if os.path.exists(local_json):
        try:
            with open(local_json, encoding="utf-8") as f:
                d = json.load(f)
            well_count = d.get("well_count", len(d.get("features", [])))
        except Exception:
            pass

    # Also upload the updated map HTML so any HTML changes are live too
    upload_map_html(s3)

    # Update state
    state["last_completed_count"] = current_count
    state["last_refresh_utc"]     = ts_now
    state["last_well_count"]      = well_count
    save_state(state)

    map_url = f"https://{BUCKET}.s3.amazonaws.com/viewer/well_map.html"
    print(f"\n  Done. {well_count} wells now live at:\n  {map_url}")


if __name__ == "__main__":
    main()

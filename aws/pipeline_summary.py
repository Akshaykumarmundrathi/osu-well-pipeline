"""
Pipeline progress summary — per-phase row counts, quota health, final CSV breakdown.

Downloads every processing_status.csv from S3, merges them by pdf_stem, and
prints a full breakdown:

  - Per-phase counts  : latlong / grid / location / county / dot
                        (done / failed / skipped / pending for each)
  - Failure types     : what broke and in which stage
  - Final status      : success / needs-review / failed / in-progress
  - Threshold alerts  : WARN if failure rates breach configured limits
  - Quota events      : API key rotations per slice (from job_status.json)

Usage:
    python aws/pipeline_summary.py
    python aws/pipeline_summary.py --bucket osu-pipeline-results --prefix results/
    python aws/pipeline_summary.py --local /path/to/processing_status.csv
    python aws/pipeline_summary.py --no-download      # skip S3, show cached counts
"""

import argparse
import csv
import io
import json
import sys
from collections import defaultdict

import boto3

# ---------------------------------------------------------------------------
# Config — mirrors project/config.py thresholds
# ---------------------------------------------------------------------------
BUCKET = "osu-pipeline-results"
PREFIX = "results/"
REGION = "us-east-1"

STAGES = ("latlong", "grid", "location", "county", "dot")

# Review thresholds: a done stage below these confidence values flags a record
# for manual review.  Mirrors config.py REVIEW_BELOW constants.
REVIEW_BELOW = {
    "latlong":  80,
    "grid":     80,
    "location": 100,   # 100 = any missing STR field
    "county":   86,
    "dot":      70,
}

# Failure-rate thresholds for the terminal WARN/ABORT markers
FAIL_WARN_PCT = {
    "county":   40,   # free-tier Gemini struggles; >40% = quota likely gone
    "grid":     20,
    "location": 30,
    "dot":      50,   # older PDFs are harder
    "latlong":  90,   # most records have no lat/lon — high fail rate is normal
}
API_FAIL_WARN_PCT   = 20   # county api_error rate that indicates quota exhaustion
KEY_ROTATION_WARN   = 1    # any key rotation warrants attention


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------
def _s3():
    return boto3.client("s3", region_name=REGION)


def fetch_all_rows(bucket: str, prefix: str) -> tuple[list[dict], dict]:
    """
    Download every processing_status.csv from S3 and return
    (merged_rows, quota_events_by_slice).

    Rows are keyed by pdf_stem so a record updated in a later checkpoint
    always wins over an earlier one.
    """
    s3 = _s3()
    paginator = s3.get_paginator("list_objects_v2")
    all_rows: dict[str, dict] = {}
    quota_events: dict[str, dict] = {}
    csv_count = 0

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]

            if key.endswith("processing_status.csv"):
                try:
                    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
                    for row in csv.DictReader(io.StringIO(body)):
                        stem = row.get("pdf_stem", "")
                        if stem:
                            all_rows[stem] = row
                    csv_count += 1
                except Exception as exc:
                    print(f"  WARN: could not read {key}: {exc}", file=sys.stderr)

            elif key.endswith("job_status.json"):
                try:
                    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                    data = json.loads(body)
                    kr = data.get("analysis", {}).get("key_rotations", 0)
                    if kr:
                        slice_key = key.split("/")[1] if "/" in key else key
                        quota_events[slice_key] = {"key_rotations": kr}
                except Exception:
                    pass

    print(f"  Loaded {len(all_rows):,} records from {csv_count} checkpoint CSV(s)")
    return list(all_rows.values()), quota_events


def load_local_rows(path: str) -> tuple[list[dict], dict]:
    rows: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            stem = row.get("pdf_stem", "")
            if stem:
                rows[stem] = row
    print(f"  Loaded {len(rows):,} records from {path}")
    return list(rows.values()), {}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate(rows: list[dict]) -> dict:
    """Return a stats dict from a flat list of processing_status rows."""
    stage_counts: dict[str, dict[str, int]] = {
        s: {"done": 0, "failed": 0, "skipped": 0, "pending": 0}
        for s in STAGES
    }
    fail_by_type: dict[str, dict[str, int]] = {s: defaultdict(int) for s in STAGES}
    n_success = n_review = n_failed_terminal = n_in_progress = 0

    for row in rows:
        # Per-stage status counts
        for stage in STAGES:
            st = (row.get(f"{stage}_status") or "pending").lower()
            if st not in stage_counts[stage]:
                st = "pending"
            stage_counts[stage][st] += 1

            if st == "failed":
                et = (row.get(f"{stage}_error_type") or "unknown").lower()
                fail_by_type[stage][et] += 1

        # Final record classification
        has_pending = any(
            (row.get(f"{s}_status") or "pending").lower() == "pending"
            for s in STAGES
        )
        if has_pending:
            n_in_progress += 1
            continue

        has_failure = any(
            (row.get(f"{s}_status") or "").lower() == "failed"
            for s in STAGES
        )

        # Low-confidence check for review flagging
        needs_review = False
        for stage in STAGES:
            if (row.get(f"{stage}_status") or "").lower() != "done":
                continue
            # county uses county_score, others use {stage}_confidence
            conf_raw = (row.get(f"{stage}_confidence") or
                        row.get(f"{stage}_score") or "")
            try:
                if float(conf_raw) < REVIEW_BELOW[stage]:
                    needs_review = True
                    break
            except (ValueError, TypeError):
                pass

        if has_failure:
            n_failed_terminal += 1
        elif needs_review:
            n_review += 1
        else:
            n_success += 1

    return {
        "total":            len(rows),
        "stage_counts":     stage_counts,
        "fail_by_type":     {s: dict(v) for s, v in fail_by_type.items()},
        "n_success":        n_success,
        "n_review":         n_review,
        "n_failed_terminal": n_failed_terminal,
        "n_in_progress":    n_in_progress,
    }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
W = 74   # table width

def _bar(n: int, total: int, width: int = 20) -> str:
    """Tiny ASCII progress bar."""
    if total == 0:
        return " " * width
    filled = round(width * n / total)
    return "#" * filled + "." * (width - filled)


def _pct(n: int, denom: int) -> str:
    return f"{100 * n / denom:.1f}%" if denom else "—"


def print_summary(stats: dict, quota_events: dict) -> bool:
    """
    Print the full summary table.
    Returns True if all thresholds are OK, False if any WARN triggered.
    """
    total = stats["total"]
    sc    = stats["stage_counts"]
    fb    = stats["fail_by_type"]

    print()
    print("=" * W)
    print(f"  PIPELINE SUMMARY   {total:,} PDFs in processing_status")
    print("=" * W)

    # ── Per-stage table ──────────────────────────────────────────────────
    print()
    print(f"  {'Stage':<12}  {'done':>7}  {'failed':>7}  {'skipped':>8}  "
          f"{'pending':>8}  {'fail%':>6}")
    print(f"  {'-'*12}  {'-'*7}  {'-'*7}  {'-'*8}  {'-'*8}  {'-'*6}")

    for stage in STAGES:
        c       = sc[stage]
        done    = c.get("done",    0)
        failed  = c.get("failed",  0)
        skipped = c.get("skipped", 0)
        pending = c.get("pending", 0)
        touched = done + failed
        fp      = _pct(failed, touched)
        print(f"  {stage:<12}  {done:>7,}  {failed:>7,}  {skipped:>8,}  "
              f"{pending:>8,}  {fp:>6}")

    # ── Failure breakdown ────────────────────────────────────────────────
    print()
    print(f"  Failure breakdown (by error type)")
    print(f"  {'Stage':<12}  {'error_type':<26}  {'count':>6}  {'of touched':>10}")
    print(f"  {'-'*12}  {'-'*26}  {'-'*6}  {'-'*10}")
    any_fail = False
    for stage in STAGES:
        touched = sc[stage].get("done", 0) + sc[stage].get("failed", 0)
        for etype, cnt in sorted(fb[stage].items(), key=lambda x: -x[1]):
            pct = _pct(cnt, touched)
            print(f"  {stage:<12}  {etype:<26}  {cnt:>6,}  {pct:>10}")
            any_fail = True
    if not any_fail:
        print(f"  (no failures)")

    # ── Final status ─────────────────────────────────────────────────────
    n_ok  = stats["n_success"]
    n_rev = stats["n_review"]
    n_fai = stats["n_failed_terminal"]
    n_wip = stats["n_in_progress"]
    term  = n_ok + n_rev + n_fai

    print()
    print(f"  Final status  (terminal: {term:,}  in-progress: {n_wip:,})")
    print(f"  {'-'*50}")
    rows_disp = [
        ("success",      n_ok,  "all stages complete, confidence OK"),
        ("needs review",  n_rev, "complete but low confidence in 1+ stage"),
        ("failed",        n_fai, "complete but 1+ stage failed"),
        ("in-progress",   n_wip, "has pending stages"),
    ]
    for label, count, note in rows_disp:
        pct = _pct(count, total)
        bar = _bar(count, total, 16)
        print(f"  {label:<15}  {count:>7,}  {pct:>6}  [{bar}]  {note}")

    # ── Review thresholds ────────────────────────────────────────────────
    print()
    print(f"  Threshold checks")
    print(f"  {'-'*60}")
    all_ok = True

    for stage in ("county", "grid", "location", "dot"):
        c       = sc[stage]
        touched = c.get("done", 0) + c.get("failed", 0)
        if touched == 0:
            continue
        fail_pct = 100 * c.get("failed", 0) / touched
        warn_pct = FAIL_WARN_PCT[stage]
        is_warn  = fail_pct >= warn_pct
        flag     = "!! WARN" if is_warn else "  OK  "
        if is_warn:
            all_ok = False
        print(f"  {flag}  {stage:<12}  failure {fail_pct:5.1f}%  (warn >= {warn_pct}%)")

    # County API quota check
    county_api = fb.get("county", {}).get("api_error", 0)
    county_touched = sc["county"].get("done", 0) + sc["county"].get("failed", 0)
    if county_touched > 0:
        api_pct  = 100 * county_api / county_touched
        is_warn  = api_pct >= API_FAIL_WARN_PCT
        flag     = "!! WARN" if is_warn else "  OK  "
        if is_warn:
            all_ok = False
        print(f"  {flag}  {'county api_error':<12}  {api_pct:5.1f}%  "
              f"(warn >= {API_FAIL_WARN_PCT}% — possible quota exhaustion)")

    # Key rotation events
    total_rotations = sum(v.get("key_rotations", 0) for v in quota_events.values())
    if total_rotations >= KEY_ROTATION_WARN:
        all_ok = False
        print(f"  !! WARN  Gemini key rotations: {total_rotations} "
              f"across {len(quota_events)} slice(s) — add more API keys")
        for slice_id, ev in sorted(quota_events.items()):
            if ev.get("key_rotations", 0):
                print(f"           {slice_id}: {ev['key_rotations']} rotation(s)")
    else:
        print(f"  OK     Gemini key rotations: {total_rotations}  (none — quota healthy)")

    print()
    if all_ok:
        print(f"  All thresholds OK")
    else:
        print(f"  !! One or more thresholds exceeded — see WARN lines above")
    print("=" * W)

    return all_ok


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="OSU pipeline progress summary")
    parser.add_argument("--bucket",  default=BUCKET,  help="S3 results bucket")
    parser.add_argument("--prefix",  default=PREFIX,  help="S3 results prefix")
    parser.add_argument("--local",   default="",      help="Local processing_status.csv path")
    args = parser.parse_args()

    print(f"\nFetching pipeline status...")
    if args.local:
        rows, quota_events = load_local_rows(args.local)
    else:
        rows, quota_events = fetch_all_rows(args.bucket, args.prefix)

    if not rows:
        print("No data found.")
        sys.exit(0)

    stats = aggregate(rows)
    ok    = print_summary(stats, quota_events)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

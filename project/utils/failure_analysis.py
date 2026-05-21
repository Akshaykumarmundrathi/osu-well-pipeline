"""
Failure taxonomy and analysis for the processing pipeline.

Reads processing_status.csv and writes failure_analysis.csv containing
per-stage × error_type × tier breakdowns. Also appends a run summary
to run_history.jsonl for the evolutionary learner to consume.

Output schema for failure_analysis.csv:
  stage | error_type | tier | collection_range | year_range |
  count | example_stems
"""

import csv
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import ALL_STAGES, tier_for
from utils.processing_status import FAILED

_FAILURE_FIELDNAMES = [
    "stage", "error_type", "tier", "collection_range", "year_range",
    "count", "example_stems",
]

# Collection → tier label for display
_TIER_COL_RANGES = {
    "early":      "1–6",
    "transition": "7–8",
    "mid":        "9–10",
    "late":       "11–12",
    "modern":     "13+",
}


def _year_range(years: list[str]) -> str:
    """Return 'YYYY–YYYY' from a list of year strings, or '' if empty."""
    parsed = []
    for y in years:
        try:
            parsed.append(int(str(y)[:4]))
        except (ValueError, TypeError):
            pass
    if not parsed:
        return ""
    return f"{min(parsed)}–{max(parsed)}"


def generate_failure_analysis(status_csv: Path, output_csv: Path) -> dict:
    """
    Parse processing_status.csv and emit failure_analysis.csv.

    Returns a summary dict for callers (e.g. to embed in run_insights.json).
    """
    if not status_csv.exists():
        return {}

    # {(stage, error_type, tier): {stems: [...], years: [...]}}
    buckets: dict[tuple, dict] = defaultdict(lambda: {"stems": [], "years": []})

    with status_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            col_num = 0
            try:
                col_num = int(row.get("collection_num") or 0)
            except (ValueError, TypeError):
                pass
            tier = row.get("model_tier") or tier_for(col_num)
            year = row.get("year", "")
            stem = row.get("pdf_stem", "")

            for stage in ALL_STAGES:
                if row.get(f"{stage}_status") != FAILED:
                    continue
                et = row.get(f"{stage}_error_type") or "unknown"
                key = (stage, et, tier)
                bucket = buckets[key]
                if len(bucket["stems"]) < 5:
                    bucket["stems"].append(stem)
                bucket["years"].append(year)

    if not buckets:
        return {}

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for (stage, et, tier), data in sorted(buckets.items(), key=lambda x: (-len(x[1]["stems"]), x[0])):
        rows.append({
            "stage":            stage,
            "error_type":       et,
            "tier":             tier,
            "collection_range": _TIER_COL_RANGES.get(tier, "?"),
            "year_range":       _year_range(data["years"]),
            "count":            len(data["years"]),
            "example_stems":    " | ".join(data["stems"][:5]),
        })

    with output_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_FAILURE_FIELDNAMES)
        w.writeheader()
        w.writerows(rows)

    total_failures = sum(r["count"] for r in rows)
    summary = {
        "total_failures": total_failures,
        "breakdown":      [
            {"stage": r["stage"], "error_type": r["error_type"], "tier": r["tier"], "count": r["count"]}
            for r in rows
        ],
    }
    return summary


def append_run_history(output_root: Path, run_summary: dict) -> None:
    """
    Append a one-line JSON record to run_history.jsonl for the evolutionary
    learner. Each record captures aggregate stage counts and run metadata.
    """
    history_file = output_root / "run_history.jsonl"
    record = {
        "ts":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed": run_summary.get("elapsed_s", 0),
        "counts":  run_summary.get("counts", {}),
        "cache":   run_summary.get("cache_stats", {}),
    }
    with history_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

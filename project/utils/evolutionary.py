"""
Self-learning parameter advisor for the processing pipeline.

Reads run_history.jsonl (appended by failure_analysis.append_run_history)
and produces parameter_suggestions.json with threshold adjustment
recommendations. Conservative: only fires when a trend is clear across
at least MIN_RUNS runs and the failure rate improvement is substantial.

Output format (parameter_suggestions.json):
{
  "generated_at": "YYYY-MM-DDTHH:MM:SS",
  "based_on_runs": N,
  "suggestions": [
    {
      "parameter": "FUZZY_MATCH_THRESHOLD",
      "current": 72,
      "suggested": 68,
      "reason": "county no_match failure rate >30% in early tier — lowering threshold may recover more"
    },
    ...
  ]
}
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

MIN_RUNS = 3          # minimum run history before making suggestions
HIGH_FAIL_RATE = 0.30 # flag when a stage/tier failure rate exceeds this

# Map from (stage, error_type) → readable parameter to tune
_PARAM_HINTS = {
    ("county",   "no_match"):         "FUZZY_MATCH_THRESHOLD",
    ("location", "keyword_not_found"): "LOCATION_MIN_OVERLAP",
    ("grid",     "not_detected"):      "GRID_W_LOOSE / GRID_H_LOOSE",
    ("dot",      "no_dot_found"):      "U-Net threshold / more training data",
    ("latlong",  "no_match"):          "LOCATION_LINE_KEYWORDS",
}


def learn_from_run(output_root: Path) -> list[dict]:
    """
    Analyse run_history.jsonl and write parameter_suggestions.json.
    Returns the suggestion list (may be empty).
    """
    history_file = output_root / "run_history.jsonl"
    if not history_file.exists():
        return []

    runs = []
    with history_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    runs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if len(runs) < MIN_RUNS:
        return []

    # Aggregate failure counts across all runs
    # counts[stage][status] += n
    agg: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for run in runs:
        for stage, stage_counts in run.get("counts", {}).items():
            for status, n in stage_counts.items():
                agg[stage][status] += int(n)

    # Also aggregate from breakdown if present (from failure_analysis)
    fail_by_key: dict[tuple, int] = defaultdict(int)
    for run in runs:
        for item in run.get("failure_breakdown", []):
            key = (item.get("stage"), item.get("error_type"), item.get("tier"))
            fail_by_key[key] += int(item.get("count", 0))

    suggestions = []

    # Check per-stage overall failure rates
    for stage, counts in agg.items():
        total = sum(counts.values())
        if total < 10:
            continue
        failed = counts.get("failed", 0)
        rate = failed / total
        if rate < HIGH_FAIL_RATE:
            continue
        # Find the dominant error_type for this stage
        best_key = None
        best_count = 0
        for (s, et, tier), cnt in fail_by_key.items():
            if s == stage and cnt > best_count:
                best_count = cnt
                best_key = (s, et, tier)

        param = _PARAM_HINTS.get((stage, best_key[1] if best_key else ""), "unknown")
        suggestions.append({
            "parameter": param,
            "stage":     stage,
            "error_type": best_key[1] if best_key else "mixed",
            "tier":       best_key[2] if best_key else "all",
            "failure_rate": round(rate, 3),
            "reason": (
                f"{stage} failure rate {rate:.0%} over {len(runs)} runs "
                f"(dominant error: {best_key[1] if best_key else 'mixed'}) — "
                f"consider tuning {param}"
            ),
        })

    result = {
        "generated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "based_on_runs": len(runs),
        "suggestions":   suggestions,
    }

    out_path = output_root / "parameter_suggestions.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return suggestions

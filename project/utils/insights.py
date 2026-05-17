"""
Run-insights collector.

Aggregates per-stage, per-collection, per-method statistics across every
record processed in a single pipeline run. At the end emits two files into
the output root:

  run_insights.md    — human-readable, designed for review by Claude or
                       a human operator. Includes per-stage outcomes,
                       confidence distributions, anchor-phrase hit rates,
                       error-type tallies, and auto-recommendations.
  run_insights.json  — machine-readable; the same data plus raw counters
                       for diffs across runs.

Wire-up: main.py creates one collector per run, calls `.add(record, results)`
inside _consume() after _apply_results, and invokes `.write()` at run end.
"""

import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import ALL_STAGES
from utils.processing_status import _classify_error

# Threshold above which a per-stage failure rate triggers an auto-suggestion.
_FAILURE_FLAG_PCT = 10.0


class InsightsCollector:
    """Aggregates stage/collection/method counters across a pipeline run."""

    def __init__(self, output_root: Path, workers: int = 1,
                 total_records: int = 0):
        """Snap the start time and prepare empty counters."""
        self.output_root   = Path(output_root)
        self.workers       = workers
        self.total_records = total_records
        self.t0            = time.time()

        # Per-stage rollup. Each stage gets the same shape so the markdown
        # can iterate without conditional logic.
        self.stages: dict[str, dict] = {
            s: {
                "detected":    0,
                "failed":      0,
                "skipped":     0,
                "already":     0,
                "by_method":   Counter(),
                "by_error":    Counter(),
                "conf_sum":    0,
                "conf_count":  0,
                "time_sum":    0.0,
                "time_count":  0,
            }
            for s in ALL_STAGES
        }

        # Cross-cutting counters.
        self.records_seen          = 0
        self.collection_records    = Counter()
        self.collection_outcomes   = defaultdict(Counter)  # col -> {stage:status}
        self.anchor_phrase_hits    = Counter()
        self.county_method_hits    = Counter()
        self.location_low_conf     = 0                     # 66% records
        self.open_failures         = 0                     # PDFs that failed to open

    # -- ingest ----------------------------------------------------------------

    def add(self, record, results: dict):
        """Fold one record's stage results into the collector."""
        self.records_seen += 1
        col = record.collection or "unknown"
        self.collection_records[col] += 1

        for stage, r in results.items():
            if stage not in self.stages:
                continue
            s = self.stages[stage]

            # SKIPPED sentinel (string)
            if r == "skipped":
                s["skipped"] += 1
                self.collection_outcomes[col][f"{stage}_skipped"] += 1
                continue
            if not isinstance(r, dict):
                continue
            if r.get("_was_done"):
                s["already"] += 1
                continue

            elapsed = r.get("_elapsed")
            if isinstance(elapsed, (int, float)):
                s["time_sum"]   += float(elapsed)
                s["time_count"] += 1

            if r.get("detected"):
                s["detected"] += 1
                self.collection_outcomes[col][f"{stage}_ok"] += 1

                method = r.get("method") or ""
                if method:
                    s["by_method"][method] += 1
                if stage == "grid" and isinstance(method, str) \
                        and method.startswith("anchor_"):
                    self.anchor_phrase_hits[method] += 1
                if stage == "county" and method:
                    self.county_method_hits[method] += 1

                conf = r.get("confidence", r.get("fuzzy_score", 0))
                try:
                    c = int(conf)
                    s["conf_sum"]   += c
                    s["conf_count"] += 1
                    if stage == "location" and c <= 66:
                        self.location_low_conf += 1
                except (TypeError, ValueError):
                    pass

            elif r.get("error"):
                err = r["error"]
                if "open_failed" in str(err):
                    self.open_failures += 1
                s["failed"] += 1
                self.collection_outcomes[col][f"{stage}_failed"] += 1
                s["by_error"][_classify_error(str(err))] += 1

    # -- emit ------------------------------------------------------------------

    def _stage_summary_line(self, stage: str) -> str:
        """One-line summary used in the markdown's headline table."""
        s = self.stages[stage]
        total = s["detected"] + s["failed"] + s["skipped"] + s["already"]
        if total == 0:
            return f"{stage:<9}  (no records)"
        pct = lambda n: 100.0 * n / total
        return (f"{stage:<9}  total={total:>6,}  "
                f"detected={s['detected']:>6,} ({pct(s['detected']):5.1f}%)  "
                f"failed={s['failed']:>5,} ({pct(s['failed']):4.1f}%)  "
                f"skipped={s['skipped']:>5,}  already={s['already']:>6,}")

    def _recommendations(self) -> list[str]:
        """Generate actionable suggestions from observed counters."""
        recs = []
        for stage in ALL_STAGES:
            s = self.stages[stage]
            total = s["detected"] + s["failed"]
            if total == 0:
                continue
            fail_pct = 100.0 * s["failed"] / total
            if fail_pct < _FAILURE_FLAG_PCT:
                continue
            top_err, n = (s["by_error"].most_common(1) or [(None, 0)])[0]
            recs.append(
                f"- **{stage}** failure rate {fail_pct:.1f}% "
                f"(most common: `{top_err}` x {n:,}). "
                + _stage_specific_hint(stage, top_err)
            )

        # Anchor-phrase coverage: if 'spot well located' dominates we know
        # forms are mostly post-1920s style; sparse hits = older anchor variants
        # missing.
        total_anchor = sum(self.anchor_phrase_hits.values())
        total_grid_ok = self.stages["grid"]["detected"]
        if total_grid_ok and total_anchor / max(1, total_grid_ok) < 0.4:
            recs.append(
                "- Grid anchor used on <40% of detected grids. The current "
                "phrases ('Spot well located' / 'Locate Well') may not match "
                "the forms in this collection. Sample a few PDFs and look "
                "for the printed text adjacent to the grid box."
            )

        if self.location_low_conf and self.stages["location"]["detected"]:
            ratio = self.location_low_conf / self.stages["location"]["detected"]
            if ratio > 0.5:
                recs.append(
                    f"- {self.location_low_conf:,} location detections at <=66% "
                    f"({ratio*100:.0f}% of OKs). On post-(11) forms the layout "
                    "may use a 'Location:' line without sec/twp/rge keywords — "
                    "consider a new strategy that reads the line after 'Location'."
                )

        if not recs:
            recs.append("- No stage exceeded the failure-flag threshold. "
                        "Looking good for this slice; move to the next.")
        return recs

    def write(self) -> tuple[Path, Path]:
        """Emit run_insights.md + run_insights.json. Returns both paths."""
        elapsed_s = time.time() - self.t0
        wall = _fmt_duration(elapsed_s)
        rpm  = (self.records_seen / elapsed_s * 60.0) if elapsed_s > 0 else 0

        md_path   = self.output_root / "run_insights.md"
        json_path = self.output_root / "run_insights.json"
        md_path.parent.mkdir(parents=True, exist_ok=True)

        # -- markdown ---------------------------------------------------------
        out = []
        out.append(f"# Run Insights — {datetime.now(timezone.utc).isoformat(timespec='seconds')}Z")
        out.append("")
        out.append("## Throughput")
        out.append(f"- Records processed this run: **{self.records_seen:,}** "
                   f"of {self.total_records:,} total")
        out.append(f"- Wall time: **{wall}**")
        out.append(f"- Throughput: **{rpm:.1f} records/min**")
        out.append(f"- Workers: **{self.workers}**")
        if self.open_failures:
            out.append(f"- PDFs that failed to open: **{self.open_failures}**")
        out.append("")

        out.append("## Stage outcomes")
        out.append("```")
        for stage in ALL_STAGES:
            out.append(self._stage_summary_line(stage))
        out.append("```")
        out.append("")

        # Per-stage detail
        for stage in ALL_STAGES:
            s = self.stages[stage]
            if not any([s["detected"], s["failed"], s["skipped"], s["already"]]):
                continue
            out.append(f"### {stage}")
            avg_conf = (s["conf_sum"] / s["conf_count"]) if s["conf_count"] else 0
            avg_time = (s["time_sum"] / s["time_count"]) if s["time_count"] else 0
            out.append(f"- Detected: {s['detected']:,}  "
                       f"(avg confidence {avg_conf:.0f}%, "
                       f"avg time {avg_time:.2f}s)")
            out.append(f"- Failed:   {s['failed']:,}")
            if s["by_method"]:
                out.append("- Method breakdown:")
                for m, n in s["by_method"].most_common(10):
                    out.append(f"    - `{m}`: {n:,}")
            if s["by_error"]:
                out.append("- Error breakdown:")
                for e, n in s["by_error"].most_common(10):
                    out.append(f"    - `{e}`: {n:,}")
            out.append("")

        # Grid anchor phrases
        if self.anchor_phrase_hits:
            out.append("## Grid anchor-phrase hits")
            for m, n in self.anchor_phrase_hits.most_common():
                out.append(f"- `{m}`: {n:,}")
            out.append("")

        # County methods
        if self.county_method_hits:
            out.append("## County extraction method breakdown")
            for m, n in self.county_method_hits.most_common():
                out.append(f"- `{m}`: {n:,}")
            anchor_only = self.county_method_hits.get("anchor", 0)
            total = sum(self.county_method_hits.values())
            if total:
                pct = 100.0 * anchor_only / total
                out.append(f"\n_Gemini-free path (anchor) carried "
                           f"**{pct:.1f}%** of county detections this run._")
            out.append("")

        # Per-collection summary
        if self.collection_records:
            out.append("## Per-collection outcomes")
            out.append("| Collection | Records | latlong OK | grid OK | loc OK | county OK | "
                       "grid fail | loc fail | county fail |")
            out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
            for col in sorted(self.collection_records,
                              key=lambda c: self.collection_records[c],
                              reverse=True):
                co = self.collection_outcomes.get(col, {})
                out.append(
                    f"| {col} | {self.collection_records[col]:,} | "
                    f"{co.get('latlong_ok', 0):,} | "
                    f"{co.get('grid_ok', 0):,} | "
                    f"{co.get('location_ok', 0):,} | "
                    f"{co.get('county_ok', 0):,} | "
                    f"{co.get('grid_failed', 0):,} | "
                    f"{co.get('location_failed', 0):,} | "
                    f"{co.get('county_failed', 0):,} |"
                )
            out.append("")

        out.append("## Recommendations (auto-generated)")
        out.extend(self._recommendations())
        out.append("")
        out.append("---")
        out.append(f"_Generated at {datetime.now(timezone.utc).isoformat(timespec='seconds')}Z. "
                   "Diff vs prior `run_insights.md` to spot regressions._")

        md_path.write_text("\n".join(out), encoding="utf-8")

        # -- JSON (machine-readable) -----------------------------------------
        payload = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "wall_seconds":  round(elapsed_s, 1),
            "records_seen":  self.records_seen,
            "total_records": self.total_records,
            "records_per_min": round(rpm, 2),
            "workers":       self.workers,
            "open_failures": self.open_failures,
            "stages": {
                s: {
                    **{k: (dict(v) if isinstance(v, Counter) else v)
                       for k, v in self.stages[s].items()},
                    "avg_confidence": round(
                        self.stages[s]["conf_sum"] / self.stages[s]["conf_count"], 2
                    ) if self.stages[s]["conf_count"] else 0,
                    "avg_time_s": round(
                        self.stages[s]["time_sum"] / self.stages[s]["time_count"], 3
                    ) if self.stages[s]["time_count"] else 0,
                }
                for s in ALL_STAGES
            },
            "anchor_phrase_hits": dict(self.anchor_phrase_hits),
            "county_method_hits": dict(self.county_method_hits),
            "collection_records": dict(self.collection_records),
            "collection_outcomes": {k: dict(v) for k, v in self.collection_outcomes.items()},
            "location_low_conf": self.location_low_conf,
        }
        json_path.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8",
        )
        return md_path, json_path


# -- helpers -------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    """1234s -> '20m 34s'; 7261s -> '2h 01m 01s'."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _stage_specific_hint(stage: str, top_err: str) -> str:
    """Short suggestion tailored to (stage, error_type) pair."""
    hints = {
        ("grid",     "not_detected"):
            "Try `--relaxed` band wider or add a new grid-anchor phrase.",
        ("location", "not_found"):
            "Likely missing-keyword forms (post-(11) layout). Add a "
            "'Location:' line reader as a third strategy.",
        ("county",   "no_match"):
            "Bump COUNTY_RETRY_CROP_SCALE or accept anchor weak matches "
            "at FUZZY_MATCH_THRESHOLD.",
        ("county",   "keyword_not_found"):
            "Scan more pages by raising MAX_COUNTY_PAGES.",
        ("latlong",  "api_error"):
            "Vision API instability — _call_with_retry already retries 3x.",
    }
    return hints.get((stage, top_err),
                     "Inspect failed_records.csv for representative samples.")

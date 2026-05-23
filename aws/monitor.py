"""
aws/monitor.py — Live pipeline progress dashboard.

Polls S3 progress_map.json + AWS Batch job status every N seconds and prints
a formatted table showing per-slice state and aggregate counts.

Usage:
    python monitor.py                  # poll every 60s until done
    python monitor.py --interval 30    # faster refresh
    python monitor.py --once           # one-shot print and exit
    python monitor.py --job-ids JOB1 JOB2  # watch specific Batch array jobs
    python monitor.py --tail-logs      # also tail CloudWatch logs for running jobs
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Config — mirrors orchestrate_robust.py constants
# ---------------------------------------------------------------------------
REGION         = "us-east-1"
OUTPUT_BUCKET  = "osu-pipeline-results"
PROGRESS_KEY   = "state/progress_map.json"
JOB_QUEUE      = "osu-pipeline-queue"
LOG_GROUP      = "/aws/batch/osu-pipeline"

# --- Dashboard alert thresholds (can be overridden via env) ---
# Warn when logic_failed slices exceed this % of total.
LOGIC_FAIL_WARN_PCT  = float(os.environ.get("LOGIC_FAIL_WARN_PCT",  "10"))
LOGIC_FAIL_ERROR_PCT = float(os.environ.get("LOGIC_FAIL_ERROR_PCT", "30"))
# Warn if no progress in this many minutes.
STALL_WARN_MINUTES   = int(os.environ.get("STALL_WARN_MINUTES", "30"))

# ANSI colours (disabled when not a TTY)
_IS_TTY = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    if not _IS_TTY:
        return text
    return f"\033[{code}m{text}\033[0m"

GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
RED    = lambda t: _c("31", t)
CYAN   = lambda t: _c("36", t)
BOLD   = lambda t: _c("1",  t)
DIM    = lambda t: _c("2",  t)

# ---------------------------------------------------------------------------
# AWS clients (lazy, singleton)
# ---------------------------------------------------------------------------
_s3      = None
_batch   = None
_logs    = None

def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=REGION)
    return _s3

def batch():
    global _batch
    if _batch is None:
        _batch = boto3.client("batch", region_name=REGION)
    return _batch

def logs():
    global _logs
    if _logs is None:
        _logs = boto3.client("logs", region_name=REGION)
    return _logs


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------
def _fetch_progress_map() -> Optional[dict]:
    """Read s3://OUTPUT_BUCKET/PROGRESS_KEY; return None if missing."""
    try:
        obj = s3().get_object(Bucket=OUTPUT_BUCKET, Key=PROGRESS_KEY)
        return json.loads(obj["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def _fetch_slice_status(slice_idx: int) -> Optional[dict]:
    key = f"results/slice-{slice_idx:05d}/job_status.json"
    try:
        obj = s3().get_object(Bucket=OUTPUT_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except ClientError:
        return None


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------
def _batch_job_summary(job_ids: list[str]) -> dict[str, Counter]:
    """
    Given a list of Batch array-job IDs, return a dict:
      job_id -> Counter({status: count})
    where status is one of SUBMITTED/PENDING/RUNNABLE/STARTING/RUNNING/SUCCEEDED/FAILED.
    """
    result: dict[str, Counter] = {}
    if not job_ids:
        return result

    # describe_jobs can handle up to 100 IDs at a time
    for i in range(0, len(job_ids), 100):
        chunk = job_ids[i : i + 100]
        resp = batch().describe_jobs(jobs=chunk)
        for job in resp.get("jobs", []):
            jid    = job["jobId"]
            status = job.get("status", "UNKNOWN")

            # Array job: sum child statuses from arrayProperties
            ap = job.get("arrayProperties", {})
            status_summary = ap.get("statusSummary", {})
            if status_summary:
                result[jid] = Counter(
                    {k.upper(): v for k, v in status_summary.items() if v > 0}
                )
            else:
                result[jid] = Counter({status: 1})

    return result


def _get_running_log_streams(job_id: str, max_streams: int = 3) -> list[str]:
    """Return log stream names for running children of an array job."""
    streams = []
    try:
        paginator = batch().get_paginator("list_jobs")
        for page in paginator.paginate(
            arrayJobId=job_id,
            jobStatus="RUNNING",
            PaginationConfig={"MaxItems": max_streams},
        ):
            for j in page.get("jobSummaryList", []):
                ls = j.get("container", {}).get("logStreamName")
                if ls:
                    streams.append(ls)
            if len(streams) >= max_streams:
                break
    except Exception:
        pass
    return streams[:max_streams]


def _tail_log_stream(stream: str, lines: int = 5) -> list[str]:
    """Return last `lines` log events from a CloudWatch log stream."""
    try:
        resp = logs().get_log_events(
            logGroupName=LOG_GROUP,
            logStreamName=stream,
            limit=lines,
            startFromHead=False,
        )
        return [e["message"] for e in resp.get("events", [])]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
_STATE_COLOUR = {
    "done":         GREEN,
    "succeeded":    GREEN,
    "infra_failed": RED,
    "logic_failed": YELLOW,
    "failed":       RED,
    "pending":      DIM,
    "running":      CYAN,
    "submitted":    CYAN,
}

def _colour_state(state: str) -> str:
    fn = _STATE_COLOUR.get(state.lower(), lambda t: t)
    return fn(state)


def _bar(done: int, total: int, width: int = 30) -> str:
    if total == 0:
        return "[" + " " * width + "]"
    filled = int(width * done / total)
    # Use ASCII fallback on Windows consoles that can't render block characters.
    _enc = (sys.stdout.encoding or "").lower()
    fill_char  = "█" if "utf" in _enc else "#"
    empty_char = "░" if "utf" in _enc else "."
    bar = fill_char * filled + empty_char * (width - filled)
    return f"[{bar}]"


def _clear_screen():
    if _IS_TTY:
        print("\033[H\033[J", end="", flush=True)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Main display
# ---------------------------------------------------------------------------
def _print_dashboard(
    progress_map: Optional[dict],
    job_summaries: dict[str, Counter],
    tail_logs: bool = False,
    slice_detail: bool = False,
):
    _clear_screen()
    print(BOLD("=" * 70))
    print(BOLD(f"  OSU Well Pipeline — Live Monitor    {_ts()}"))
    print(BOLD("=" * 70))

    # ---- Batch job overview ----
    if job_summaries:
        print(BOLD("\nBatch Jobs"))
        print(f"  {'Job ID':<48}  {'SUCC':>5}  {'RUN':>5}  {'FAIL':>5}  {'PEND':>5}")
        print("  " + "-" * 66)
        grand = Counter()
        for jid, counts in sorted(job_summaries.items()):
            succ = counts.get("SUCCEEDED", 0)
            run  = counts.get("RUNNING",   0) + counts.get("STARTING", 0)
            fail = counts.get("FAILED",    0)
            pend = (counts.get("SUBMITTED", 0) + counts.get("PENDING", 0)
                    + counts.get("RUNNABLE", 0))
            total_j = sum(counts.values())
            grand.update(counts)
            pct  = f"{100*succ//total_j}%" if total_j else "—"
            print(f"  {jid:<48}  {GREEN(str(succ)):>14}  {CYAN(str(run)):>14}  "
                  f"{RED(str(fail)):>13}  {DIM(str(pend)):>14}  {pct}")
        # grand totals
        gs = grand.get("SUCCEEDED", 0)
        gr = grand.get("RUNNING", 0) + grand.get("STARTING", 0)
        gf = grand.get("FAILED", 0)
        gp = grand.get("SUBMITTED", 0) + grand.get("PENDING", 0) + grand.get("RUNNABLE", 0)
        gt = sum(grand.values())
        print("  " + "-" * 66)
        print(f"  {'TOTAL':<48}  {GREEN(str(gs)):>14}  {CYAN(str(gr)):>14}  "
              f"{RED(str(gf)):>13}  {DIM(str(gp)):>14}  "
              f"{100*gs//gt if gt else 0}%")
    else:
        print(DIM("\n  (no Batch job IDs being tracked — pass --job-ids to track live)"))

    # ---- S3 progress map ----
    if progress_map is None:
        print(DIM("\n  No progress map found in S3 yet (orchestrate_robust.py not started?)."))
        print(DIM(f"  Expected: s3://{OUTPUT_BUCKET}/{PROGRESS_KEY}"))
        return

    # Support both old field names (updated_utc/total_slices) and new ones
    updated  = progress_map.get("updated_at") or progress_map.get("updated_utc", "unknown")
    n_slices = progress_map.get("n_slices") or progress_map.get("total_slices", 0)
    states: dict = progress_map.get("states", {})

    # If per-slice states are present use them; fall back to aggregated counts
    if states:
        state_counts: Counter = Counter(states.values())
    else:
        state_counts = Counter(progress_map.get("counts", {}))

    n_done   = state_counts.get("done", 0) + state_counts.get("succeeded", 0)
    n_infra  = state_counts.get("infra_failed", 0)
    n_logic  = state_counts.get("logic_failed", 0)
    n_pend   = state_counts.get("pending", 0)
    n_run    = state_counts.get("running", 0)
    n_total  = n_slices or len(states) or sum(state_counts.values())

    pct_done = 100 * n_done // n_total if n_total else 0

    print(BOLD("\nSlice Progress (S3)"))
    print(f"  Updated: {DIM(updated)}")
    print(f"  Total slices : {n_total}")
    print(f"  {_bar(n_done, n_total)}  {pct_done}% complete")
    print()
    print(f"  {GREEN('Done         ')} {n_done:>6}")
    print(f"  {CYAN('Running      ')} {n_run:>6}")
    print(f"  {DIM('Pending      ')} {n_pend:>6}")
    print(f"  {RED('Infra-failed ')} {n_infra:>6}  (will be retried)")
    print(f"  {YELLOW('Logic-failed ')} {n_logic:>6}  (skipped on retry)")

    # --- Threshold alerts ---
    logic_fail_pct = 100.0 * n_logic / n_total if n_total else 0.0
    infra_fail_pct = 100.0 * n_infra / n_total if n_total else 0.0

    if logic_fail_pct >= LOGIC_FAIL_ERROR_PCT:
        print()
        print(RED(f"  !! ALERT: {logic_fail_pct:.1f}% of slices are logic_failed "
                  f"(threshold={LOGIC_FAIL_ERROR_PCT:.0f}%)"))
        print(RED(  "     Likely a systematic code bug. Inspect CloudWatch logs before retrying."))
        print(RED(  "     Use --retry-logic only after the root cause is fixed."))
    elif logic_fail_pct >= LOGIC_FAIL_WARN_PCT:
        print()
        print(YELLOW(f"  WARN: {logic_fail_pct:.1f}% logic_failed "
                     f"(alert threshold={LOGIC_FAIL_ERROR_PCT:.0f}%)"))

    if infra_fail_pct > 20:
        print(YELLOW(f"  WARN: {infra_fail_pct:.1f}% infra_failed — check Fargate capacity "
                     f"and network config"))

    # --- Stall detection ---
    updated_str = progress_map.get("updated_at") or progress_map.get("updated_utc", "")
    if updated_str:
        try:
            updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            stale_min = (datetime.now(timezone.utc) - updated).total_seconds() / 60
            if stale_min >= STALL_WARN_MINUTES and n_run > 0:
                print(YELLOW(
                    f"  STALL: progress_map last updated {stale_min:.0f} min ago "
                    f"(threshold={STALL_WARN_MINUTES} min) — orchestrator may be down"
                ))
        except Exception:
            pass

    # Estimate ETA
    if progress_map.get("started_at") and n_done > 0 and n_total > n_done:
        try:
            raw = progress_map["started_at"]
            # fromisoformat on Python <3.11 doesn't parse trailing 'Z'
            started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            elapsed = (now_utc - started).total_seconds()
            if elapsed > 0:
                rate  = n_done / elapsed
                eta_s = (n_total - n_done) / rate
                eta_m = int(eta_s / 60)
                eta_label = f"{eta_m // 60}h {eta_m % 60}m" if eta_m > 60 else f"{eta_m}m"
                behind = " — BEHIND SCHEDULE" if eta_m > 48 * 60 else ""
                colour = RED if behind else (YELLOW if eta_m > 24 * 60 else lambda t: t)
                print(colour(f"\n  Rate: {rate*60:.1f} slices/min  —  ETA: ~{eta_label}{behind}"))
        except Exception:
            pass

    # ---- Per-slice detail (optional) ----
    if slice_detail and states:
        print(BOLD("\nSlice Detail"))
        cols = 8
        items = sorted((int(k), v) for k, v in states.items())
        for i, (idx, state) in enumerate(items):
            label = f"{idx:05d}:{_colour_state(state):<12}"
            print(f"  {label}", end="\n" if (i + 1) % cols == 0 else "  ")
        if len(items) % cols:
            print()

    # ---- CloudWatch log tails ----
    if tail_logs and job_summaries:
        running_streams: list[str] = []
        for jid in job_summaries:
            running_streams.extend(_get_running_log_streams(jid, max_streams=2))
            if len(running_streams) >= 4:
                break

        if running_streams:
            print(BOLD("\nRecent log lines (running tasks)"))
            for stream in running_streams[:3]:
                lines = _tail_log_stream(stream, lines=4)
                if lines:
                    print(f"  {DIM(stream)}")
                    for ln in lines:
                        print(f"    {ln.rstrip()}")
                    print()

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OSU pipeline live monitor")
    p.add_argument("--interval",   type=int,  default=60,
                   help="Refresh interval in seconds (default 60)")
    p.add_argument("--once",       action="store_true",
                   help="Print once and exit")
    p.add_argument("--job-ids",    nargs="*", default=[],
                   help="AWS Batch array job IDs to track")
    p.add_argument("--tail-logs",  action="store_true",
                   help="Tail CloudWatch logs for running tasks")
    p.add_argument("--slice-detail", action="store_true",
                   help="Show per-slice state grid")
    p.add_argument("--no-colour",  action="store_true",
                   help="Disable ANSI colours")
    return p.parse_args()


def main():
    args = _parse_args()

    if args.no_colour:
        global _IS_TTY
        _IS_TTY = False

    job_ids: list[str] = args.job_ids or []

    # Try to load job IDs from progress map if not given on CLI
    _prog = _fetch_progress_map()
    if not job_ids and _prog:
        job_ids = _prog.get("batch_job_ids", [])

    print(f"Monitoring pipeline  —  interval={args.interval}s  "
          f"jobs tracked={len(job_ids)}", flush=True)

    while True:
        try:
            progress_map  = _fetch_progress_map()
            # Refresh job IDs from map each cycle in case new jobs were submitted
            if progress_map:
                for jid in progress_map.get("batch_job_ids", []):
                    if jid not in job_ids:
                        job_ids.append(jid)

            job_summaries = _batch_job_summary(job_ids)
            _print_dashboard(
                progress_map,
                job_summaries,
                tail_logs=args.tail_logs,
                slice_detail=args.slice_detail,
            )

            # Auto-exit when all slices are done
            if progress_map:
                states = progress_map.get("states", {})
                n_total = progress_map.get("n_slices", len(states))
                n_done  = sum(
                    1 for s in states.values()
                    if s in ("done", "succeeded")
                )
                n_failed = sum(
                    1 for s in states.values()
                    if s in ("infra_failed", "logic_failed", "failed")
                )
                if n_done + n_failed >= n_total and n_total > 0:
                    print(BOLD("Pipeline complete — all slices settled."))
                    break

        except KeyboardInterrupt:
            print("\nInterrupted.")
            break
        except Exception as exc:
            print(f"{RED('Error')}: {exc}", flush=True)

        if args.once:
            break

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            break


if __name__ == "__main__":
    main()

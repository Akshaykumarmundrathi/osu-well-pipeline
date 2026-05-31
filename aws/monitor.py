"""
monitor.py  —  Live Batch job progress dashboard
=================================================
Polls AWS Batch every 60s and prints a clean progress table.
Run any time after orchestrate.py has submitted jobs.

Usage:
    python aws/monitor.py
    python aws/monitor.py --prefix osu-pipeline-slice-   # only pipeline jobs
    python aws/monitor.py --once                         # print once and exit
    python aws/monitor.py --logs osu-pipeline-slice-0042 # tail one job's logs
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import boto3

REGION      = "us-east-1"
BATCH_QUEUE = "osu-pipeline-queue"
LOG_GROUP   = "/aws/batch/osu-pipeline"
STATES      = ["SUBMITTED","PENDING","RUNNABLE","STARTING","RUNNING","SUCCEEDED","FAILED"]


def _ts(): return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
def _p(msg): print(msg, flush=True)


def get_counts(batch, queue: str, prefix: str) -> dict:
    counts = {s: 0 for s in STATES}
    failed_names = []
    for status in STATES:
        try:
            pag = batch.get_paginator("list_jobs")
            for page in pag.paginate(jobQueue=queue, jobStatus=status):
                for job in page.get("jobSummaryList", []):
                    if job["jobName"].startswith(prefix):
                        counts[status] += 1
                        if status == "FAILED":
                            failed_names.append(job["jobName"])
        except Exception:
            pass
    counts["_failed_names"] = failed_names
    return counts


def print_dashboard(counts: dict, prefix: str):
    total = sum(counts[s] for s in STATES)
    done  = counts["SUCCEEDED"] + counts["FAILED"]
    pct   = int(100 * done / total) if total else 0
    bar_w = 40
    bar   = "█" * int(bar_w * done / total) + "░" * (bar_w - int(bar_w * done / total)) if total else ""

    _p(f"\n{'─'*60}")
    _p(f"  OSU Pipeline Monitor    {_ts()}")
    _p(f"  Prefix: {prefix}")
    _p(f"{'─'*60}")
    _p(f"  [{bar}] {pct}%")
    _p(f"  Total:     {total:>6,}")
    _p(f"  ✓ Done:    {counts['SUCCEEDED']:>6,}  ({100*counts['SUCCEEDED']//total if total else 0}%)")
    _p(f"  ✗ Failed:  {counts['FAILED']:>6,}")
    _p(f"  ⟳ Running: {counts['RUNNING']:>6,}")
    _p(f"  ◌ Pending: {counts['SUBMITTED']+counts['PENDING']+counts['RUNNABLE']:>6,}")
    if counts["_failed_names"]:
        _p(f"\n  Failed jobs (first 5):")
        for name in counts["_failed_names"][:5]:
            _p(f"    {name}")
        if len(counts["_failed_names"]) > 5:
            _p(f"    …and {len(counts['_failed_names'])-5} more")
    _p(f"{'─'*60}")


def tail_logs(logs_client, job_name: str, follow: bool):
    """Stream CloudWatch logs for a job."""
    try:
        streams = logs_client.describe_log_streams(
            logGroupName=LOG_GROUP,
            logStreamNamePrefix=f"slice/{job_name}",
            orderBy="LastEventTime",
            descending=True,
        )
        if not streams["logStreams"]:
            _p(f"No log stream found for job: {job_name}")
            return
        stream = streams["logStreams"][0]["logStreamName"]
        _p(f"Streaming: {LOG_GROUP}/{stream}\n")

        next_token = None
        while True:
            kwargs = {"logGroupName": LOG_GROUP, "logStreamName": stream,
                      "startFromHead": True}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = logs_client.get_log_events(**kwargs)
            for event in resp["events"]:
                print(event["message"])
            next_token = resp.get("nextForwardToken")
            if not follow:
                break
            time.sleep(5)
    except Exception as e:
        _p(f"Log error: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile",  default=os.environ.get("AWS_PROFILE", "mano"))
    ap.add_argument("--queue",    default=BATCH_QUEUE)
    ap.add_argument("--prefix",   default="osu-")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--once",     action="store_true")
    ap.add_argument("--logs",     metavar="JOB_NAME",
                    help="Stream CloudWatch logs for this job name")
    args = ap.parse_args()

    sess  = boto3.Session(profile_name=args.profile, region_name=REGION)
    batch = sess.client("batch")
    logs  = sess.client("logs")

    if args.logs:
        tail_logs(logs, args.logs, follow=not args.once)
        return

    _p(f"Monitoring queue: {args.queue}  prefix: {args.prefix}")
    _p(f"Refreshing every {args.interval}s  (Ctrl+C to stop)\n")

    while True:
        counts = get_counts(batch, args.queue, args.prefix)
        print_dashboard(counts, args.prefix)

        if args.once:
            break

        total = sum(counts[s] for s in STATES)
        done  = counts["SUCCEEDED"] + counts["FAILED"]
        running = counts["RUNNING"] + counts["STARTING"]
        pending = counts["SUBMITTED"] + counts["PENDING"] + counts["RUNNABLE"]

        if total > 0 and done == total and running == 0 and pending == 0:
            _p("\nAll jobs finished.")
            break

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            _p("\nStopped.")
            break


if __name__ == "__main__":
    main()

"""
aws/orchestrate_robust.py — Robust pipeline orchestrator.

Responsibilities
----------------
1. Reads current S3 state (job_status.json per slice, manifest).
2. Classifies each slice: done | infra_failed | logic_failed | pending.
3. Registers a NEW job-definition revision with the networking fix
   (assignPublicIp=ENABLED) if the current one lacks it.
4. Submits a fresh array job covering ONLY pending + infra-failed slices.
5. Monitors Batch until all jobs complete, auto-retrying infra failures.
6. Writes a live progress_map.json to S3 after every poll cycle.
7. Exits 0 when all slices are done, non-zero on permanent failure.

Usage
-----
    python aws/orchestrate_robust.py                 # run everything
    python aws/orchestrate_robust.py --dry-run       # show plan, don't submit
    python aws/orchestrate_robust.py --status-only   # print state, exit
    python aws/orchestrate_robust.py --slice-size 500 --workers 4
"""

import argparse
import json
import logging
import math
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ACCOUNT_ID    = "225989338968"
REGION        = "us-east-1"
INPUT_BUCKET  = "osu-well-records-225989338968"
OUTPUT_BUCKET = "osu-pipeline-results"
INDEX_KEY     = "index/dataset_index.csv"
JOB_QUEUE     = "osu-pipeline-queue"
JOB_DEF_NAME  = "osu-pipeline-job"
ECR_IMAGE     = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/osu-pipeline:v6-fixed"
EXEC_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/osu-batch-execution-role"
TASK_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/osu-batch-task-role"
LOG_GROUP     = "/aws/batch/osu-pipeline"

DEFAULT_SLICE_SIZE = 500
DEFAULT_WORKERS    = 4
POLL_INTERVAL_S    = 60       # seconds between Batch status polls
PROGRESS_MAP_KEY   = "state/progress_map.json"

# Infra-failure patterns — jobs with these reason substrings are safe to retry.
_INFRA_REASONS = (
    "ResourceInitializationError",
    "CannotPullContainerError",
    "i/o timeout",
    "connection refused",
    "dial tcp",
    "network",
    "ECR",
    "ecr",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("orchestrate")

# ---------------------------------------------------------------------------
# AWS clients (lazy singletons)
# ---------------------------------------------------------------------------
_CFG  = Config(retries={"mode": "adaptive", "max_attempts": 6})
_s3   = _batch = _logs = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=REGION, config=_CFG)
    return _s3


def batch():
    global _batch
    if _batch is None:
        _batch = boto3.client("batch", region_name=REGION, config=_CFG)
    return _batch


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------
def _retry(fn, label, attempts=4, base=2.0):
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if i == attempts:
                raise
            delay = base * (2 ** (i - 1)) + random.uniform(0, 1.5)
            log.warning("%s attempt %d/%d failed (%.1fs): %s", label, i, attempts, delay, exc)
            time.sleep(delay)


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------
def _s3_read_json(bucket: str, key: str) -> dict | None:
    try:
        body = s3().get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body)
    except Exception as exc:
        err_code = getattr(getattr(exc, "response", None), "get", lambda *a: None)("Error", {}).get("Code", "")
        if "NoSuchKey" in str(exc) or "404" in str(exc) or err_code in ("NoSuchKey", "404"):
            return None
        log.debug("s3_read_json %s/%s: %s", bucket, key, exc)
        return None


def _s3_write_json(bucket: str, key: str, data: dict):
    body = json.dumps(data, indent=2).encode()
    _retry(
        lambda: s3().put_object(Bucket=bucket, Key=key, Body=body,
                                ContentType="application/json"),
        label=f"s3_write:{key}",
    )


def _count_index_rows() -> int:
    log.info("Counting rows in s3://%s/%s ...", INPUT_BUCKET, INDEX_KEY)
    obj  = _retry(lambda: s3().get_object(Bucket=INPUT_BUCKET, Key=INDEX_KEY),
                  label="get_index")
    n    = 0
    first = True
    for line in obj["Body"].iter_lines():
        if first:
            first = False
            continue
        if line:
            n += 1
    log.info("Index rows: %d", n)
    return n


# ---------------------------------------------------------------------------
# Slice state: read existing job_status.json files from previous runs
# ---------------------------------------------------------------------------
def _classify_slice(status_json: dict | None) -> str:
    """
    Return one of: 'done' | 'infra_failed' | 'logic_failed' | 'pending'

    Infra failures are those where the container started but exited within
    60 s — indicating the container environment itself failed (OOM-killed
    before processing, missing mount, etc.) rather than a code error.
    Note: ResourceInitializationError jobs never write job_status.json at
    all, so those slices stay 'pending' and are always re-submitted.
    """
    if status_json is None:
        return "pending"
    ec      = status_json.get("exit_code", -1)
    elapsed = status_json.get("elapsed_s", 3600)
    note    = status_json.get("note", "")
    if ec == 0:
        return "done"
    # Short elapsed → container never really ran (network init, ECR pull fail,
    # OOM-before-work, etc.)  → safe to retry.
    if elapsed < 60:
        return "infra_failed"
    # Note field from run_batch_job uses "SIGTERM" on preemption → infra.
    if "SIGTERM" in note:
        return "infra_failed"
    # Explicit infra keywords in the note (legacy job_status fields).
    if any(kw in note for kw in _INFRA_REASONS):
        return "infra_failed"
    return "logic_failed"


def _read_slice_states(n_slices: int) -> dict[int, str]:
    """
    Return {slice_index: state} for all slices by reading job_status.json
    objects from S3 in parallel pages.
    """
    states: dict[int, str] = {i: "pending" for i in range(n_slices)}

    paginator = s3().get_paginator("list_objects_v2")
    prefix    = "results/"
    log.info("Scanning S3 %s/%s for existing slice states ...", OUTPUT_BUCKET, prefix)
    count = 0
    for page in paginator.paginate(Bucket=OUTPUT_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/job_status.json"):
                continue
            # key = results/slice-NNNNN/job_status.json
            try:
                idx = int(key.split("/")[1].split("-")[1])
            except Exception:
                continue
            if idx >= n_slices:
                continue
            data = _s3_read_json(OUTPUT_BUCKET, key)
            states[idx] = _classify_slice(data)
            count += 1

    done        = sum(1 for v in states.values() if v == "done")
    infra_fail  = sum(1 for v in states.values() if v == "infra_failed")
    logic_fail  = sum(1 for v in states.values() if v == "logic_failed")
    pending     = sum(1 for v in states.values() if v == "pending")
    log.info(
        "Slice states: done=%d  infra_failed=%d  logic_failed=%d  pending=%d  "
        "(total=%d, status_files_found=%d)",
        done, infra_fail, logic_fail, pending, n_slices, count,
    )
    return states


# ---------------------------------------------------------------------------
# Job definition management
# ---------------------------------------------------------------------------
def _get_active_job_def() -> dict:
    resp = batch().describe_job_definitions(
        jobDefinitionName=JOB_DEF_NAME, status="ACTIVE"
    )
    defs = sorted(resp["jobDefinitions"], key=lambda d: d["revision"], reverse=True)
    return defs[0] if defs else {}


_REQUIRED_ENV_KEYS = {"PYTHONPATH", "INPUT_BUCKET", "OUTPUT_BUCKET", "INDEX_KEY",
                      "GOOGLE_CREDS_SECRET_ID"}


def _has_public_ip(job_def: dict) -> bool:
    """Return True only if the job def is fully configured (network + all required env vars)."""
    cp = job_def.get("containerProperties") or {}
    nc = cp.get("networkConfiguration") or {}
    if nc.get("assignPublicIp") != "ENABLED":
        return False
    env_names = {e["name"] for e in cp.get("environment", [])}
    if not _REQUIRED_ENV_KEYS.issubset(env_names):
        missing = _REQUIRED_ENV_KEYS - env_names
        log.info("Job def missing env vars: %s → will register new revision", missing)
        return False
    return True


def _register_fixed_job_def(slice_size: int, workers: int) -> str:
    """Register a new revision with all fixes applied. Returns job def ARN."""
    log.info("Registering new job definition revision with networking fix ...")
    resp = _retry(
        lambda: batch().register_job_definition(
            jobDefinitionName=JOB_DEF_NAME,
            type="container",
            platformCapabilities=["FARGATE"],
            containerProperties={
                "image":            ECR_IMAGE,
                "executionRoleArn": EXEC_ROLE_ARN,
                "jobRoleArn":       TASK_ROLE_ARN,
                "resourceRequirements": [
                    {"type": "VCPU",   "value": "4"},
                    {"type": "MEMORY", "value": "16384"},
                ],
                "ephemeralStorage": {"sizeInGiB": 50},
                "environment": [
                    {"name": "PYTHONUNBUFFERED",     "value": "1"},
                    {"name": "PYTHONPATH",            "value": "/app/project:/app"},
                    {"name": "AWS_DEFAULT_REGION",   "value": REGION},
                    {"name": "SLICE_SIZE",            "value": str(slice_size)},
                    {"name": "MAX_WORKERS",           "value": str(workers)},
                    {"name": "WORKERS",               "value": str(workers)},
                    {"name": "CHECKPOINT_INTERVAL_S", "value": "240"},
                    {"name": "GEMINI_MIN_CALL_GAP_S", "value": "2"},
                    {"name": "USE_VISION_API",         "value": "0"},
                    {"name": "DISK_WARN_GB",           "value": "8"},
                    {"name": "DISK_PRUNE_GB",          "value": "4"},
                    {"name": "INPUT_BUCKET",              "value": INPUT_BUCKET},
                    {"name": "OUTPUT_BUCKET",             "value": OUTPUT_BUCKET},
                    {"name": "INDEX_KEY",                 "value": INDEX_KEY},
                    {"name": "GOOGLE_CREDS_SECRET_ID",    "value": "osu-pipeline/credentials"},
                    {"name": "RDS_CREDS_SECRET_ID",       "value": "osu-pipeline/rds"},
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group":         LOG_GROUP,
                        "awslogs-region":        REGION,
                        "awslogs-stream-prefix": "job",
                    },
                },
                "fargatePlatformConfiguration": {"platformVersion": "LATEST"},
                "networkConfiguration": {"assignPublicIp": "ENABLED"},
            },
            retryStrategy={
                "attempts": 2,
                "evaluateOnExit": [
                    {"onReason": "ResourceInitializationError*", "action": "RETRY"},
                    {"onReason": "CannotPullContainerError*",    "action": "RETRY"},
                    {"onExitCode": "0",                          "action": "EXIT"},
                ],
            },
            timeout={"attemptDurationSeconds": 28800},   # 8 hours
        ),
        label="register_job_def",
    )
    arn = resp["jobDefinitionArn"]
    rev = resp["revision"]
    log.info("Registered %s revision %d", JOB_DEF_NAME, rev)
    return f"{JOB_DEF_NAME}:{rev}"


# ---------------------------------------------------------------------------
# Job submission
# ---------------------------------------------------------------------------
def _submit_array_job(
    job_def_ref: str,
    target_slices: list[int],
    slice_size: int,
    workers: int,
    dry_run: bool = False,
) -> str | None:
    """
    Submit an array job that covers the target_slices indices.
    Uses JOB_INDEX_OFFSET so each child's ARRAY_INDEX maps correctly.

    For non-contiguous slices we submit multiple smaller array jobs
    (each covers a contiguous range, or single tasks for gaps).
    Returns the last submitted job ID (or None for dry-run).
    """
    if not target_slices:
        log.info("No slices to submit.")
        return None

    # Sort and batch into contiguous runs to minimise job count.
    target_slices = sorted(set(target_slices))
    log.info("Submitting %d slices via array job(s) ...", len(target_slices))

    last_job_id = None
    runs: list[tuple[int, int]] = []   # (start_index, count)
    run_start = target_slices[0]
    run_len   = 1
    for prev, cur in zip(target_slices, target_slices[1:]):
        if cur == prev + 1:
            run_len += 1
        else:
            runs.append((run_start, run_len))
            run_start = cur
            run_len   = 1
    runs.append((run_start, run_len))

    for offset, size in runs:
        name = f"osu-pipeline-o{offset}-n{size}"
        env_overrides = [
            {"name": "JOB_INDEX_OFFSET", "value": str(offset)},
            {"name": "SLICE_SIZE",        "value": str(slice_size)},
            {"name": "WORKERS",           "value": str(workers)},
        ]
        log.info("  array job: offset=%d  size=%d  name=%s", offset, size, name)
        if dry_run:
            log.info("  [DRY RUN] would submit: %s", name)
            continue

        if size == 1:
            # Array size must be ≥2; use a single job for one slice.
            resp = _retry(
                lambda n=name, e=env_overrides: batch().submit_job(
                    jobName=n,
                    jobQueue=JOB_QUEUE,
                    jobDefinition=job_def_ref,
                    containerOverrides={"environment": e},
                    retryStrategy={"attempts": 2},
                ),
                label=f"submit:{name}",
            )
        else:
            resp = _retry(
                lambda n=name, s=size, e=env_overrides: batch().submit_job(
                    jobName=n,
                    jobQueue=JOB_QUEUE,
                    jobDefinition=job_def_ref,
                    arrayProperties={"size": s},
                    containerOverrides={"environment": e},
                    retryStrategy={"attempts": 2},
                ),
                label=f"submit:{name}",
            )
        job_id = resp["jobId"]
        log.info("  submitted: %s", job_id)
        last_job_id = job_id

    return last_job_id


# ---------------------------------------------------------------------------
# Progress map (written to S3 so monitor.py can read it live)
# ---------------------------------------------------------------------------
_PROGRESS_STARTED_AT: str | None = None   # set once in main()


def _write_progress_map(states: dict[int, str], batch_jobs: list[dict]):
    """Write a summary JSON to S3 for live monitoring (read by monitor.py)."""
    counts = {k: 0 for k in ("done", "infra_failed", "logic_failed", "pending", "running")}
    for v in states.values():
        counts[v] = counts.get(v, 0) + 1
    for job in batch_jobs:
        if job.get("status") == "RUNNING":
            counts["running"] += 1

    data = {
        # Fields used by monitor.py
        "updated_at":     datetime.now(timezone.utc).isoformat(),
        "started_at":     _PROGRESS_STARTED_AT,
        "n_slices":       len(states),
        "states":         {str(k): v for k, v in states.items()},
        "batch_job_ids":  [j["jobId"] for j in batch_jobs if "jobId" in j],
        # Extra summary fields
        "counts":         counts,
        "batch_jobs":     [
            {k: j[k] for k in ("jobId", "jobName", "status") if k in j}
            for j in batch_jobs[-20:]
        ],
    }
    try:
        _s3_write_json(OUTPUT_BUCKET, PROGRESS_MAP_KEY, data)
    except Exception as exc:
        log.warning("Could not write progress map: %s", exc)


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------
def _list_batch_jobs(job_ids: list[str]) -> list[dict]:
    """Describe Batch jobs by ID (handles >100 in chunks)."""
    results = []
    for i in range(0, len(job_ids), 100):
        chunk = job_ids[i:i + 100]
        try:
            resp = _retry(
                lambda c=chunk: batch().describe_jobs(jobs=c),
                label="describe_jobs",
            )
            results.extend(resp.get("jobs", []))
        except Exception as exc:
            log.warning("describe_jobs chunk failed: %s", exc)
    return results


def _monitor_until_done(
    submitted_job_ids: list[str],
    states: dict[int, str],
    n_slices: int,
    dry_run: bool = False,
) -> bool:
    """
    Poll Batch every POLL_INTERVAL_S seconds until all submitted jobs finish.
    Returns True if all succeeded, False if any permanently failed.
    """
    if dry_run or not submitted_job_ids:
        log.info("[DRY RUN] skipping monitor loop")
        return True

    log.info("Monitoring %d submitted job(s) ...", len(submitted_job_ids))
    terminal = {"SUCCEEDED", "FAILED"}
    all_done = False

    while not all_done:
        time.sleep(POLL_INTERVAL_S)

        jobs  = _list_batch_jobs(submitted_job_ids)
        by_status: dict[str, int] = {}
        for j in jobs:
            st = j.get("status", "UNKNOWN")
            by_status[st] = by_status.get(st, 0) + 1

        log.info(
            "Batch status: %s",
            "  ".join(f"{k}={v}" for k, v in sorted(by_status.items())),
        )

        # Update in-memory states for done slices so progress_map stays fresh.
        for j in jobs:
            if j.get("status") == "SUCCEEDED":
                # Try to find which slice this was from the job name.
                name = j.get("jobName", "")
                try:
                    offset = int(name.split("-o")[1].split("-n")[0])
                    arr_idx = j.get("arrayProperties", {}).get("index", 0)
                    states[offset + arr_idx] = "done"
                except Exception:
                    pass

        _write_progress_map(states, jobs)

        all_done = all(j.get("status") in terminal for j in jobs)

    failed = [j for j in jobs if j.get("status") == "FAILED"]
    if failed:
        log.warning("%d job(s) FAILED permanently:", len(failed))
        for j in failed[:5]:
            log.warning("  %s  reason: %s", j["jobId"],
                        j.get("statusReason", "")[:120])
        return False

    log.info("All submitted jobs SUCCEEDED.")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Robust OSU pipeline orchestrator")
    ap.add_argument("--dry-run",     action="store_true", help="Plan only, don't submit")
    ap.add_argument("--status-only", action="store_true", help="Print state and exit")
    ap.add_argument("--slice-size",  type=int, default=DEFAULT_SLICE_SIZE)
    ap.add_argument("--workers",     type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--force-all",   action="store_true",
                    help="Re-submit all slices, ignoring existing done/logic_failed state")
    ap.add_argument("--retry-logic", action="store_true",
                    help="Also retry logic_failed slices (default: skip them)")
    args = ap.parse_args()

    global _PROGRESS_STARTED_AT
    _PROGRESS_STARTED_AT = datetime.now(timezone.utc).isoformat()

    # 1. Count index rows → derive total slices.
    total_records = _count_index_rows()
    n_slices = max(2, math.ceil(total_records / args.slice_size))
    log.info("Total records: %d  slice_size: %d  n_slices: %d",
             total_records, args.slice_size, n_slices)

    if n_slices > 10_000:
        log.error("n_slices %d > Batch array limit 10000. Increase --slice-size.", n_slices)
        sys.exit(1)

    # 2. Read existing slice states from S3.
    states = _read_slice_states(n_slices)

    # 3. Decide which slices to submit.
    if args.force_all:
        to_submit = list(range(n_slices))
    else:
        to_submit = [
            i for i, s in states.items()
            if s in ("pending", "infra_failed")
            or (args.retry_logic and s == "logic_failed")
        ]

    done_count = sum(1 for s in states.values() if s == "done")
    log.info(
        "Plan: %d to submit  |  %d already done  |  %d logic_failed (skipped unless --retry-logic)",
        len(to_submit), done_count,
        sum(1 for s in states.values() if s == "logic_failed"),
    )

    if args.status_only:
        _write_progress_map(states, [])
        log.info("Progress map written to s3://%s/%s", OUTPUT_BUCKET, PROGRESS_MAP_KEY)
        return

    if not to_submit:
        log.info("Nothing to do — all slices are done or logic_failed.")
        _write_progress_map(states, [])
        return

    # 4. Ensure job definition has networking fix.
    job_def = _get_active_job_def()
    if _has_public_ip(job_def):
        job_def_ref = f"{JOB_DEF_NAME}:{job_def['revision']}"
        log.info("Job def %s already has assignPublicIp=ENABLED", job_def_ref)
    else:
        log.info("Job def lacks assignPublicIp=ENABLED — registering fixed revision ...")
        if not args.dry_run:
            job_def_ref = _register_fixed_job_def(args.slice_size, args.workers)
        else:
            job_def_ref = f"{JOB_DEF_NAME}:XX (dry-run)"
            log.info("[DRY RUN] would register new job def revision")

    # 5. Submit.
    submitted_ids: list[str] = []
    job_id = _submit_array_job(
        job_def_ref, to_submit,
        slice_size=args.slice_size,
        workers=args.workers,
        dry_run=args.dry_run,
    )
    if job_id:
        submitted_ids.append(job_id)

    log.info("Submitted %d job(s). Entering monitor loop ...", len(submitted_ids))

    # 6. Monitor to completion.
    success = _monitor_until_done(submitted_ids, states, n_slices, dry_run=args.dry_run)

    # 7. Final progress map.
    _write_progress_map(states, [])
    log.info(
        "Final state: done=%d / %d  (%.1f%%)",
        sum(1 for s in states.values() if s == "done"),
        n_slices,
        100 * sum(1 for s in states.values() if s == "done") / n_slices,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

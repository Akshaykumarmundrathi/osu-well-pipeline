"""
orchestrate.py  —  Generate S3 dataset index, submit Batch jobs, chain stages
==============================================================================

Full cloud flow (nothing local after this runs):
  1. List PDFs in Account 1 S3 bucket → build dataset_index_s3.csv
  2. Upload index to S3
  3. Split into N slices → submit N Batch jobs
  4. Wait for all pipeline jobs to complete
  5. Trigger enrichment job (coord → lat/lon via RDS)
  6. Trigger map-build job (GeoJSON → GitHub push)

Usage:
    python aws/orchestrate.py
    python aws/orchestrate.py --slice-size 500 --max-concurrent 50
    python aws/orchestrate.py --resume       # skip already-done slices
    python aws/orchestrate.py --stage pipeline  # only submit pipeline jobs
    python aws/orchestrate.py --stage enrich    # only submit enrichment
    python aws/orchestrate.py --stage mapbuild  # only submit map build
"""

import argparse
import csv
import io
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ACCOUNT1_ID   = "225989338968"
ACCOUNT2_ID   = "266087050585"
REGION        = "us-east-1"
BUCKET        = f"osu-well-records-{ACCOUNT1_ID}"
PDF_PREFIX    = "pdfs/"
OUT_PREFIX    = "outputs"
INDEX_S3_KEY  = f"{OUT_PREFIX}/merged/dataset_index_s3.csv"
BATCH_QUEUE   = "osu-pipeline-queue"
JOB_DEF_PIPE  = "osu-pipeline-job"
JOB_DEF_ENRICH   = "osu-enrich-job"
JOB_DEF_MAPBUILD = "osu-mapbuild-job"
POLL_INTERVAL = 60   # seconds between status polls
MAX_WAIT_HRS  = 48   # max hours to wait before giving up


def _p(msg): print(msg, flush=True)
def _ts(): return datetime.now(timezone.utc).strftime("%H:%M:%S")


# ── Dataset index ─────────────────────────────────────────────────────────────

def build_s3_index(s3, slice_size: int,
                   collection_filter: int | None = None) -> tuple[list[dict], int]:
    """
    List all PDFs in s3://BUCKET/pdfs/ and build records list.
    Returns (records, total_slices).

    collection_filter: if set, only include records from that collection number.
    Expected S3 key structure:
      pdfs/ExportedFolderContents_N/YEAR/MONTH/stem.pdf
    """
    import re as _re
    prefix = PDF_PREFIX
    if collection_filter is not None:
        # Narrow S3 list to this collection only — avoids listing 570K keys
        prefix = f"{PDF_PREFIX}ExportedFolderContents_{collection_filter}/"
        _p(f"Listing PDFs in s3://{BUCKET}/{prefix} (collection {collection_filter} only)…")
    else:
        _p(f"Listing PDFs in s3://{BUCKET}/{PDF_PREFIX}…")

    records = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith(".pdf"):
                continue
            parts = key.split("/")
            # pdfs / ExportedFolderContents_N / year / month / stem.pdf
            collection = parts[1] if len(parts) > 1 else ""
            year       = parts[2] if len(parts) > 2 else ""
            month      = parts[3] if len(parts) > 3 else ""
            stem       = Path(parts[-1]).stem
            m = _re.search(r"\d+", collection)
            coll_num = int(m.group()) if m else 0
            if collection_filter is not None and coll_num != collection_filter:
                continue
            records.append({
                "pdf_stem":        stem,
                "pdf_path":        f"s3://{BUCKET}/{key}",
                "collection":      collection,
                "collection_num":  str(coll_num),
                "year":            year,
                "month":           month,
                "zip_path":        "",
                "file_size_bytes": str(obj.get("Size", 0)),
                "scan_timestamp":  obj.get("LastModified", "").isoformat()
                                   if hasattr(obj.get("LastModified",""),"isoformat") else "",
            })

    total_slices = max(1, (len(records) + slice_size - 1) // slice_size)
    _p(f"Found {len(records):,} PDFs → {total_slices} slices of ~{slice_size}")
    return records, total_slices


def upload_index(s3, records: list[dict]) -> None:
    """Upload dataset_index_s3.csv to S3."""
    if not records:
        return
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(records[0].keys()))
    w.writeheader()
    w.writerows(records)
    s3.put_object(Bucket=BUCKET, Key=INDEX_S3_KEY,
                  Body=buf.getvalue().encode())
    _p(f"Index uploaded → s3://{BUCKET}/{INDEX_S3_KEY}")


# ── Job submission ────────────────────────────────────────────────────────────

def _submitted_job_ids(batch, job_name_prefix: str) -> set[str]:
    """Return set of job names already submitted (any state except FAILED)."""
    found = set()
    for status in ("SUBMITTED","PENDING","RUNNABLE","STARTING","RUNNING","SUCCEEDED"):
        try:
            paginator = batch.get_paginator("list_jobs")
            for page in paginator.paginate(jobQueue=BATCH_QUEUE, jobStatus=status):
                for job in page.get("jobSummaryList", []):
                    if job["jobName"].startswith(job_name_prefix):
                        found.add(job["jobName"])
        except Exception:
            pass
    return found


def submit_pipeline_jobs(batch, total_slices: int, resume: bool,
                         collection_filter: int | None = None,
                         pipeline_stage: str | None = None,
                         run_tag: str = "") -> list[str]:
    """
    Submit one Batch job per slice. Returns list of job IDs.

    collection_filter: if set, passes --collection N to main.py inside the container.
    pipeline_stage:    if set, passes --stage X so only that stage runs per job.
    run_tag:           appended to job names so multiple runs don't collide.
    """
    tag = f"-{run_tag}" if run_tag else ""
    prefix = f"osu-pipeline-slice{tag}-"
    existing = _submitted_job_ids(batch, prefix) if resume else set()

    job_ids = []
    for i in range(total_slices):
        name = f"{prefix}{i:04d}"
        if name in existing:
            _p(f"  SKIP {name} (already submitted)")
            continue
        env = [
            {"name": "BATCH_SLICE_INDEX",  "value": str(i)},
            {"name": "BATCH_TOTAL_SLICES", "value": str(total_slices)},
            {"name": "S3_INDEX_KEY",       "value": INDEX_S3_KEY},
            {"name": "S3_OUTPUT_PREFIX",
             "value": f"{OUT_PREFIX}/slices/{i:04d}"},
            {"name": "S3_CHECKPOINT_KEY",
             "value": f"checkpoints/{i:04d}/processing_status.csv"},
        ]
        if collection_filter is not None:
            env.append({"name": "COLLECTION_FILTER", "value": str(collection_filter)})
        if pipeline_stage:
            env.append({"name": "PIPELINE_STAGE", "value": pipeline_stage})
        try:
            r = batch.submit_job(
                jobName=name,
                jobQueue=BATCH_QUEUE,
                jobDefinition=JOB_DEF_PIPE,
                containerOverrides={"environment": env},
            )
            job_ids.append(r["jobId"])
            if i % 50 == 0:
                _p(f"  Submitted {i+1}/{total_slices}: {name}  id={r['jobId'][:8]}…")
        except ClientError as e:
            _p(f"  ERROR submitting {name}: {e}")

    _p(f"Submitted {len(job_ids)} new pipeline jobs "
       f"(+{len(existing)} already running/done)")
    return job_ids


def quality_report(s3, total_slices: int, stage: str | None = None) -> dict:
    """
    Download all checkpoint CSVs from S3 and produce a quality report.
    Returns summary dict; prints a human-readable table.
    """
    import csv as _csv
    from collections import defaultdict as _dd

    _p("\n── Quality Report ────────────────────────────────────────────────────────")
    stage_cols = {
        "grid":     ("grid_status",     "grid_error_type",     "grid_confidence"),
        "county":   ("county_status",   "county_error_type",   "county_confidence"),
        "location": ("location_status", "location_error_type", "location_confidence"),
        "latlong":  ("latlong_status",  "latlong_error_type",  "latlong_confidence"),
        "dot":      ("dot_status",      "dot_error_type",      "dot_confidence"),
    }
    stages_to_check = [stage] if stage else list(stage_cols)

    totals   = _dd(lambda: {"done": 0, "failed": 0, "skipped": 0, "total": 0})
    reasons  = _dd(lambda: _dd(int))
    conf_sum = _dd(float)

    for i in range(total_slices):
        key = f"checkpoints/{i:04d}/processing_status.csv"
        try:
            obj = s3.get_object(Bucket=BUCKET, Key=key)
            rows = list(_csv.DictReader(obj["Body"].read().decode().splitlines()))
        except Exception:
            continue
        for row in rows:
            for st in stages_to_check:
                sc = stage_cols.get(st)
                if not sc:
                    continue
                status_val = row.get(sc[0], "")
                totals[st]["total"] += 1
                if status_val == "done":
                    totals[st]["done"] += 1
                    try:
                        conf_sum[st] += float(row.get(sc[2], 0) or 0)
                    except ValueError:
                        pass
                elif status_val == "failed":
                    totals[st]["failed"] += 1
                    reason = row.get(sc[1], "unknown") or "unknown"
                    reasons[st][reason] += 1
                elif status_val in ("skipped", "skip"):
                    totals[st]["skipped"] += 1

    for st in stages_to_check:
        t = totals[st]
        if t["total"] == 0:
            continue
        pct = t["done"] / t["total"] * 100 if t["total"] else 0
        avg_conf = conf_sum[st] / t["done"] if t["done"] else 0
        _p(f"\n  {st.upper():<10}  total={t['total']:,}  "
           f"done={t['done']:,} ({pct:.1f}%)  "
           f"failed={t['failed']:,}  skipped={t['skipped']:,}  "
           f"avg_conf={avg_conf:.1f}")
        if reasons[st]:
            for reason, cnt in sorted(reasons[st].items(), key=lambda x: -x[1])[:8]:
                _p(f"    {'':>10}  {reason:<40} {cnt:,}")

    return dict(totals)


# ── Wait loop ─────────────────────────────────────────────────────────────────

def wait_for_queue(batch, job_name_prefix: str, total: int) -> bool:
    """Poll until all jobs with this prefix are SUCCEEDED or FAILED. Returns True if all succeeded."""
    _p(f"\nWaiting for {total} jobs (prefix={job_name_prefix})…")
    deadline = time.monotonic() + MAX_WAIT_HRS * 3600
    last_print = 0

    while time.monotonic() < deadline:
        counts = {s: 0 for s in ["SUBMITTED","PENDING","RUNNABLE","STARTING",
                                   "RUNNING","SUCCEEDED","FAILED"]}
        for status in counts:
            try:
                pag = batch.get_paginator("list_jobs")
                for page in pag.paginate(jobQueue=BATCH_QUEUE, jobStatus=status):
                    for job in page.get("jobSummaryList", []):
                        if job["jobName"].startswith(job_name_prefix):
                            counts[status] += 1
            except Exception:
                pass

        done    = counts["SUCCEEDED"] + counts["FAILED"]
        running = counts["RUNNING"] + counts["STARTING"]
        pending = counts["SUBMITTED"] + counts["PENDING"] + counts["RUNNABLE"]

        now = time.monotonic()
        if now - last_print >= POLL_INTERVAL:
            _p(f"  [{_ts()}]  succeeded={counts['SUCCEEDED']:,}  "
               f"failed={counts['FAILED']:,}  running={running:,}  "
               f"pending={pending:,}  total_seen={done+running+pending:,}/{total:,}")
            last_print = now

        if done + running + pending >= total and running == 0 and pending == 0:
            if counts["FAILED"] > 0:
                _p(f"  WARNING: {counts['FAILED']} jobs FAILED")
                return False
            _p(f"  All {counts['SUCCEEDED']} jobs SUCCEEDED ✓")
            return True

        time.sleep(30)

    _p(f"  TIMEOUT after {MAX_WAIT_HRS}h — check Batch console")
    return False


# ── Enrichment + map build ────────────────────────────────────────────────────

def submit_single_job(batch, name: str, job_def: str, env: dict) -> str:
    r = batch.submit_job(
        jobName=name,
        jobQueue=BATCH_QUEUE,
        jobDefinition=job_def,
        containerOverrides={"environment": [{"name": k, "value": v}
                                             for k, v in env.items()]},
    )
    _p(f"  Submitted: {name}  id={r['jobId']}")
    return r["jobId"]


def wait_one_job(batch, job_id: str, label: str) -> bool:
    """Wait for a single job to finish. Returns True if SUCCEEDED."""
    while True:
        r = batch.describe_jobs(jobs=[job_id])
        if not r["jobs"]:
            time.sleep(10); continue
        status = r["jobs"][0]["status"]
        _p(f"  [{_ts()}]  {label}: {status}")
        if status == "SUCCEEDED":
            return True
        if status == "FAILED":
            return False
        time.sleep(30)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile",      default=os.environ.get("AWS_PROFILE", "mano"))
    ap.add_argument("--slice-size",   type=int, default=200,
                    help="PDFs per Batch task (default 200 — ~1.5hr at 30s/file)")
    ap.add_argument("--resume",       action="store_true", default=True,
                    help="Skip slices already submitted")
    ap.add_argument("--no-resume",    dest="resume", action="store_false")
    ap.add_argument("--stage",        choices=["all","pipeline","enrich","mapbuild"],
                    default="all")
    ap.add_argument("--pipeline-stage", default=None,
                    help="Run only this pipeline stage inside each task "
                         "(grid|county|location|latlong|dot). "
                         "Omit to run all stages.")
    ap.add_argument("--collection",   type=int, default=None,
                    help="Process only this collection number (e.g. 5). "
                         "Omit to process all collections.")
    ap.add_argument("--run-tag",      default="",
                    help="Tag appended to job names to distinguish parallel runs")
    ap.add_argument("--total-slices", type=int, default=None,
                    help="Override slice count (use existing index)")
    ap.add_argument("--no-wait",      action="store_true",
                    help="Submit jobs and exit without waiting")
    ap.add_argument("--quality-only", action="store_true",
                    help="Skip submission, just print quality report from existing checkpoints")
    args = ap.parse_args()

    sess  = boto3.Session(profile_name=args.profile, region_name=REGION)
    s3    = sess.client("s3")
    batch = sess.client("batch")

    # ── Quality-only mode ─────────────────────────────────────────────────────
    if args.quality_only:
        if args.total_slices is None:
            _p("ERROR: --quality-only requires --total-slices N")
            sys.exit(1)
        quality_report(s3, args.total_slices, args.pipeline_stage)
        return

    # ── Stage: pipeline ──────────────────────────────────────────────────────
    if args.stage in ("all", "pipeline"):
        if args.total_slices:
            total_slices = args.total_slices
            _p(f"Using existing index with {total_slices} slices")
        else:
            records, total_slices = build_s3_index(
                s3, args.slice_size, collection_filter=args.collection)
            upload_index(s3, records)

        submit_pipeline_jobs(batch, total_slices, args.resume,
                             collection_filter=args.collection,
                             pipeline_stage=args.pipeline_stage,
                             run_tag=args.run_tag)

        tag = f"-{args.run_tag}" if args.run_tag else ""
        job_prefix = f"osu-pipeline-slice{tag}-"
        if not args.no_wait:
            ok = wait_for_queue(batch, job_prefix, total_slices)
            _p("")
            quality_report(s3, total_slices, args.pipeline_stage)
            if not ok:
                _p("\nPipeline stage had failures — check CloudWatch logs")
                _p("Run with --stage enrich to proceed anyway")
                if args.stage == "pipeline":
                    sys.exit(1)
        else:
            _p(f"Jobs submitted. Monitor: python aws/monitor.py")
            _p(f"Quality report when done: python aws/orchestrate.py "
               f"--quality-only --total-slices {total_slices}"
               + (f" --pipeline-stage {args.pipeline_stage}" if args.pipeline_stage else ""))
            if args.stage == "pipeline":
                return

    # ── Stage: enrichment ────────────────────────────────────────────────────
    if args.stage in ("all", "enrich"):
        _p("\n── Enrichment job (coord → lat/lon via RDS) ──────────────")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        jid = submit_single_job(batch, f"osu-enrich-{ts}", JOB_DEF_ENRICH, {
            "S3_BUCKET":         BUCKET,
            "S3_OUTPUT_PREFIX":  f"{OUT_PREFIX}/merged",
            "SECRETS_PREFIX":    "osu/",
            "AWS_DEFAULT_REGION": REGION,
        })
        if not args.no_wait:
            ok = wait_one_job(batch, jid, "enrichment")
            if not ok:
                _p("Enrichment failed — check CloudWatch logs")
                if args.stage == "enrich":
                    sys.exit(1)

    # ── Stage: map build ─────────────────────────────────────────────────────
    if args.stage in ("all", "mapbuild"):
        _p("\n── Map build job (GeoJSON → GitHub Pages) ───────────────")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        jid = submit_single_job(batch, f"osu-mapbuild-{ts}", JOB_DEF_MAPBUILD, {
            "S3_BUCKET":         BUCKET,
            "S3_INPUT_KEY":      f"{OUT_PREFIX}/merged/dot_coordinates.csv",
            "SECRETS_PREFIX":    "osu/",
            "AWS_DEFAULT_REGION": REGION,
        })
        if not args.no_wait:
            ok = wait_one_job(batch, jid, "map-build")
            if ok:
                _p("\n✓ Map live: https://github.com/pages  (check your repo URL)")
            else:
                _p("Map build failed — check CloudWatch logs")

    _p("\nDone.")


if __name__ == "__main__":
    main()

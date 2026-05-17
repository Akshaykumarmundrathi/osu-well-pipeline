"""
Submit one AWS Batch array job that processes the entire dataset.

Computes the number of array tasks from the master index size and the
configured slice size, then submits a single array-job with the right
size. Each task picks up its slice using AWS_BATCH_JOB_ARRAY_INDEX.

Usage (from your PC, after `aws configure` and pushing the image):
  python aws/submit_jobs.py \
      --queue   osu-batch-queue \
      --jobdef  osu-pipeline-jobdef:1 \
      --bucket  osu-well-records \
      --index   index/dataset_index.csv \
      --slice   1000
"""

import argparse
import math
import sys

import boto3


def count_index_rows(bucket: str, key: str) -> int:
    """Stream the index CSV from S3 and count rows (header excluded)."""
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    n = 0
    first = True
    for line in obj["Body"].iter_lines():
        if first:
            first = False
            continue
        if line:
            n += 1
    return n


def submit(queue: str, jobdef: str, n_tasks: int, env: dict,
           name: str = "osu-pipeline") -> str:
    """Submit the array job and return its job ID."""
    batch = boto3.client("batch")
    resp = batch.submit_job(
        jobName        = name,
        jobQueue       = queue,
        jobDefinition  = jobdef,
        arrayProperties = {"size": n_tasks},
        containerOverrides = {
            "environment": [{"name": k, "value": str(v)} for k, v in env.items()],
        },
        retryStrategy = {"attempts": 2},
    )
    return resp["jobId"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue",   required=True)
    ap.add_argument("--jobdef",  required=True, help="e.g. osu-pipeline-jobdef:1")
    ap.add_argument("--bucket",  required=True, help="INPUT_BUCKET")
    ap.add_argument("--output-bucket", default=None,
                    help="OUTPUT_BUCKET (defaults to same as input)")
    ap.add_argument("--index",   required=True,
                    help="Key inside --bucket, e.g. index/dataset_index.csv")
    ap.add_argument("--slice",   type=int, default=1000)
    ap.add_argument("--workers", type=int, default=8,
                    help="Internal worker count per container")
    ap.add_argument("--secret",  default="osu-pipeline/credentials",
                    help="Secrets Manager SecretId for GCP creds")
    ap.add_argument("--name",    default="osu-pipeline")
    args = ap.parse_args()

    total = count_index_rows(args.bucket, args.index)
    n_tasks = max(2, math.ceil(total / args.slice))    # Batch min size is 2
    print(f"index has {total:,} records, slice={args.slice} -> {n_tasks} tasks")

    if n_tasks > 10000:
        print(f"ERROR: array size {n_tasks} > AWS Batch limit (10000)",
              file=sys.stderr)
        sys.exit(2)

    env = {
        "INPUT_BUCKET":           args.bucket,
        "OUTPUT_BUCKET":          args.output_bucket or args.bucket,
        "INDEX_KEY":              args.index,
        "SLICE_SIZE":             args.slice,
        "WORKERS":                args.workers,
        "GOOGLE_CREDS_SECRET_ID": args.secret,
    }
    job_id = submit(args.queue, args.jobdef, n_tasks, env, name=args.name)
    print(f"submitted array job: {job_id}")
    print(f"watch progress:  aws batch describe-jobs --jobs {job_id}")


if __name__ == "__main__":
    main()

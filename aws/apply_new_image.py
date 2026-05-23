"""
Run this AFTER CodeBuild completes a new image tag:
1. Register new job def revision (new image, same config)
2. Clear failed S3 slice states so they get re-run
3. Submit fresh array job

Usage:
    python aws/apply_new_image.py [--dry-run]
"""

import argparse
import json
import math
import sys

import boto3

REGION        = "us-east-1"
ACCOUNT_ID    = "225989338968"
ECR_REPO      = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/osu-pipeline"
NEW_IMAGE_TAG = "v13-flat"
NEW_IMAGE     = f"{ECR_REPO}:{NEW_IMAGE_TAG}"
JOB_DEF_NAME   = "osu-pipeline-job"
INPUT_BUCKET   = "osu-well-records-225989338968"
INDEX_KEY      = "index/dataset_index.csv"
JOB_QUEUE      = "osu-pipeline-queue"
OUTPUT_BUCKET  = "osu-pipeline-results"
DEFAULT_SLICE  = 500

batch = boto3.client("batch", region_name=REGION)
s3    = boto3.client("s3",    region_name=REGION)
ecr   = boto3.client("ecr",   region_name=REGION)


def verify_image_exists() -> bool:
    try:
        ecr.describe_images(
            repositoryName="osu-pipeline",
            imageIds=[{"imageTag": NEW_IMAGE_TAG}],
        )
        print(f"  ECR image {NEW_IMAGE_TAG} exists - OK")
        return True
    except ecr.exceptions.ImageNotFoundException:
        print(f"  ECR image {NEW_IMAGE_TAG} NOT FOUND — has CodeBuild finished?")
        return False


def register_new_revision() -> str:
    """Register a new job def revision based on the current highest active revision."""
    jds = batch.describe_job_definitions(
        jobDefinitionName=JOB_DEF_NAME,
        status="ACTIVE",
    )["jobDefinitions"]
    if not jds:
        raise RuntimeError(f"No active job definitions found for {JOB_DEF_NAME}")
    # Always derive from the latest active revision — no hardcoded source rev.
    base = max(jds, key=lambda j: j["revision"])
    print(f"  Basing on {JOB_DEF_NAME}:{base['revision']} (latest active revision)")
    cp   = dict(base["containerProperties"])

    # Swap image + tune workers to reduce concurrent Gemini quota pressure.
    # 30 tasks × 2 workers = 60 concurrent county calls vs 120 with WORKERS=4.
    cp["image"] = NEW_IMAGE
    env = cp.get("environment", [])
    for entry in env:
        if entry["name"] in ("WORKERS", "MAX_WORKERS"):
            entry["value"] = "2"
    cp["environment"] = env
    # Keep jobRoleArn at top level (some API versions need it)
    job_role_arn = cp.get("jobRoleArn", "")

    result = batch.register_job_definition(
        jobDefinitionName=JOB_DEF_NAME,
        type="container",
        containerProperties={**cp, "jobRoleArn": job_role_arn},
        retryStrategy=base["retryStrategy"],
        timeout=base.get("timeout", {"attemptDurationSeconds": 86400}),
        tags=base.get("tags", {}),
        platformCapabilities=["FARGATE"],
    )
    arn = result["jobDefinitionArn"]
    rev = result["revision"]
    print(f"  Registered {JOB_DEF_NAME}:{rev}  image={NEW_IMAGE}  WORKERS=2")
    return arn


def _count_index_rows(slice_size: int = DEFAULT_SLICE) -> int:
    """Count rows in the S3 index to compute the correct array job size."""
    print(f"  Counting rows in s3://{INPUT_BUCKET}/{INDEX_KEY} ...")
    obj = s3.get_object(Bucket=INPUT_BUCKET, Key=INDEX_KEY)
    n = 0
    first = True
    for line in obj["Body"].iter_lines():
        if first:
            first = False
            continue
        if line:
            n += 1
    size = max(2, math.ceil(n / slice_size))
    print(f"  Index rows: {n}  slice_size: {slice_size}  array_size: {size}")
    return size


def clear_failed_slice_states() -> int:
    """Delete infra_failed job_status.json files from S3 so they get re-run."""
    paginator = s3.get_paginator("list_objects_v2")
    deleted = 0
    keys_to_delete = []

    for page in paginator.paginate(Bucket=OUTPUT_BUCKET, Prefix="results/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("job_status.json"):
                continue
            # Read to check if infra_failed
            try:
                body = s3.get_object(Bucket=OUTPUT_BUCKET, Key=key)["Body"].read()
                data = json.loads(body)
                if data.get("state") in ("infra_failed", "failed") or data.get("exit_code", 0) not in (0,):
                    keys_to_delete.append(key)
            except Exception:
                pass

    if keys_to_delete:
        # Delete in batches of 1000
        for i in range(0, len(keys_to_delete), 1000):
            batch_keys = keys_to_delete[i:i+1000]
            s3.delete_objects(
                Bucket=OUTPUT_BUCKET,
                Delete={"Objects": [{"Key": k} for k in batch_keys]},
            )
            deleted += len(batch_keys)
        print(f"  Cleared {deleted} failed slice states from S3")
    else:
        print("  No failed slice states found to clear")
    return deleted


def submit_array_job(job_def_arn: str, array_size: int) -> str:
    resp = batch.submit_job(
        jobName=f"osu-pipeline-{NEW_IMAGE_TAG}",
        jobQueue=JOB_QUEUE,
        jobDefinition=job_def_arn,
        arrayProperties={"size": array_size},
    )
    job_id = resp["jobId"]
    print(f"  Submitted array job ({array_size} tasks): {job_id}")
    return job_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",    action="store_true", help="Print plan but don't execute")
    parser.add_argument("--slice-size", type=int, default=DEFAULT_SLICE,
                        help=f"Records per slice (default {DEFAULT_SLICE})")
    args = parser.parse_args()

    print(f"=== Applying {NEW_IMAGE_TAG} image ===\n")

    print("1. Verifying ECR image exists...")
    if not verify_image_exists():
        print("   Run CodeBuild first: python aws/setup_codebuild.py")
        sys.exit(1)

    if args.dry_run:
        print("\n[DRY RUN] Would register job def and submit array job")
        sys.exit(0)

    print("\n2. Registering new job def revision (new image)...")
    jd_arn = register_new_revision()

    print("\n3. Clearing old failed slice states from S3...")
    clear_failed_slice_states()

    print("\n4. Computing array job size from index...")
    array_size = _count_index_rows(args.slice_size)

    print(f"\n5. Submitting fresh {array_size}-task array job...")
    job_id = submit_array_job(jd_arn, array_size)

    print(f"\n=== Done! Job: {job_id} ===")
    print(f"Monitor with: python aws/monitor.py --job-ids {job_id} --interval 60")

"""
Run this AFTER CodeBuild completes v6-fixed-2:
1. Register job def revision 13 (new image, same config)
2. Clear failed S3 slice states so orchestrate_robust.py re-runs them
3. Submit fresh 1139-task array job

Usage:
    python aws/apply_new_image.py [--dry-run]
"""

import argparse
import json
import sys
import time

import boto3

REGION        = "us-east-1"
ACCOUNT_ID    = "225989338968"
ECR_REPO      = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/osu-pipeline"
NEW_IMAGE_TAG = "v6-fixed-2"
NEW_IMAGE     = f"{ECR_REPO}:{NEW_IMAGE_TAG}"
JOB_DEF_NAME  = "osu-pipeline-job"
SOURCE_REV    = 12
JOB_QUEUE     = "osu-pipeline-queue"
OUTPUT_BUCKET = "osu-pipeline-results"

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


def register_rev13() -> str:
    # Get rev 12 as base
    jds = batch.describe_job_definitions(
        jobDefinitionName=JOB_DEF_NAME,
        status="ACTIVE",
    )["jobDefinitions"]
    base = next(j for j in jds if j["revision"] == SOURCE_REV)
    cp   = dict(base["containerProperties"])

    # Swap image only
    cp["image"] = NEW_IMAGE
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
    print(f"  Registered {JOB_DEF_NAME}:{rev}  image={NEW_IMAGE}")
    return arn


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


def submit_array_job(job_def_arn: str) -> str:
    resp = batch.submit_job(
        jobName="osu-pipeline-v6fixed2",
        jobQueue=JOB_QUEUE,
        jobDefinition=job_def_arn,
        arrayProperties={"size": 1139},
    )
    job_id = resp["jobId"]
    print(f"  Submitted array job: {job_id}")
    return job_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print plan but don't execute")
    args = parser.parse_args()

    print("=== Applying v6-fixed-2 image ===\n")

    print("1. Verifying ECR image exists...")
    if not verify_image_exists():
        print("   Run CodeBuild first: python aws/setup_codebuild.py")
        sys.exit(1)

    if args.dry_run:
        print("\n[DRY RUN] Would register job def and submit array job")
        sys.exit(0)

    print("\n2. Registering job def revision 13 (new image)...")
    jd_arn = register_rev13()

    print("\n3. Clearing old failed slice states from S3...")
    clear_failed_slice_states()

    print("\n4. Submitting fresh 1139-task array job...")
    job_id = submit_array_job(jd_arn)

    print(f"\n=== Done! Job: {job_id} ===")
    print(f"Monitor with: python aws/monitor.py --job-id {job_id} --interval 60")

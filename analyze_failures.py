#!/usr/bin/env python3
"""
Analyze batch job failures and generate report.
"""
import boto3
import json
from pathlib import Path
from collections import defaultdict

REGION = 'us-east-1'
QUEUE = 'osu-pipeline-queue-ec2'

batch = boto3.client('batch', region_name=REGION)
logs = boto3.client('logs', region_name=REGION)
s3 = boto3.client('s3', region_name=REGION)

print("="*70)
print("BATCH JOB FAILURE ANALYSIS")
print("="*70)
print()

# 1. Count job statuses
print("[1] JOB STATUS SUMMARY")
print("-"*70)

statuses = {}
for status in ['RUNNING', 'RUNNABLE', 'SUCCEEDED', 'FAILED', 'SUBMITTED']:
    try:
        jobs = batch.list_jobs(jobQueue=QUEUE,
            filters=[{'name': 'job-status', 'values': [status]}],
            maxResults=1000)['jobSummaryList']
        count = len(jobs)
        statuses[status] = count
        print(f"  {status:12}: {count:4} jobs")
    except:
        pass

print()
total = sum(statuses.values())
print(f"  TOTAL: {total} jobs")
print()

# 2. Analyze FAILED jobs
print("[2] FAILED JOBS ANALYSIS")
print("-"*70)

try:
    failed_jobs = batch.list_jobs(jobQueue=QUEUE,
        filters=[{'name': 'job-status', 'values': ['FAILED']}],
        maxResults=100)['jobSummaryList']

    print(f"  Total failed: {len(failed_jobs)}\n")

    failure_reasons = defaultdict(int)

    for job in failed_jobs[:20]:  # Analyze first 20
        job_id = job['jobId']
        reason = job.get('statusReason', 'Unknown')
        failure_reasons[reason] += 1

        print(f"  Job: {job['jobName']}")
        print(f"    ID: {job_id}")
        print(f"    Reason: {reason[:80]}")
        print()

    print("\n  Failure reason summary:")
    for reason, count in sorted(failure_reasons.items(), key=lambda x: x[1], reverse=True):
        print(f"    {count:3}x - {reason[:60]}")

except Exception as e:
    print(f"  Error analyzing failures: {e}")

print()

# 3. Check S3 for completed slices
print("[3] S3 COMPLETED SLICES")
print("-"*70)

try:
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket='osu-pipeline-results', Prefix='results/')

    completed_slices = set()
    total_size = 0

    for page in pages:
        if 'Contents' in page:
            for obj in page['Contents']:
                key = obj['Key']
                if 'job_status.json' in key:
                    slice_num = key.split('/')[1].replace('slice-', '')
                    completed_slices.add(int(slice_num))
                total_size += obj['Size']

    print(f"  Completed slices: {len(completed_slices)} / 391")
    print(f"  Total output size: {total_size / (1024**3):.2f} GB")

    if completed_slices:
        print(f"  Range: {min(completed_slices)} - {max(completed_slices)}")

except Exception as e:
    print(f"  Error checking S3: {e}")

print()

# 4. Generate recommendations
print("[4] RECOMMENDATIONS")
print("-"*70)

if statuses.get('FAILED', 0) > 0:
    print("  ⚠ Jobs are failing. Check:")
    print("    1. CloudWatch logs for error messages")
    print("    2. IAM permissions for Secrets Manager access")
    print("    3. RDS connectivity")
    print("    4. S3 bucket permissions")
    print("    5. Input PDF file integrity")
else:
    print("  ✓ No failures detected yet")

if statuses.get('RUNNING', 0) > 0:
    print(f"  ⚡ {statuses['RUNNING']} jobs still processing")

if statuses.get('SUCCEEDED', 0) > 0:
    print(f"  ✓ {statuses['SUCCEEDED']} jobs completed successfully")

print()
print("="*70)

#!/usr/bin/env python3
"""
Optimized AWS Batch Configuration - Lower Cost, No Timeouts

Adjust SLICE_SIZE and timeout to prevent failures and reduce cost.

Current Problem:
- SLICE_SIZE: 500 PDFs × 12 sec = 6000 sec (1.67h processing + overhead)
- Timeout: 14400 sec (4h) - TIGHT, can fail with network issues
- Result: $120-200 for full run

Solution:
- SLICE_SIZE: 250 PDFs (30-40 min processing)
- Timeout: 14400 sec (4h) - safe margin
- Result: 391 × (500/250) = 782 jobs, but faster, fewer retries
- Cost: $60-100 (similar but more reliable)

Alternative:
- SLICE_SIZE: 200 PDFs (25-30 min processing)
- Timeout: 10800 sec (3h)
- Result: 782 more jobs but each completes quickly
- Cost: Similar, but less risk
"""

import boto3
import json
import time

batch = boto3.client('batch', region_name='us-east-1')

REGION = 'us-east-1'

print("="*70)
print("OPTIMIZED AWS BATCH CONFIGURATION")
print("="*70)
print()

# Option 1: REDUCE SLICE SIZE (Safer, fewer timeouts)
print("[OPTION 1] Reduce slice size for speed & safety")
print("-"*70)
print("  SLICE_SIZE: 250 PDFs (down from 500)")
print("  Est. time per job: 35-45 minutes")
print("  Timeout: 14400 sec (4h) - safe margin")
print("  Total jobs: 391 × (500/250) = 782 jobs")
print("  Cost: ~$60-80")
print("  Risk: LOW (each job fast, easy to retry)")
print()

# Option 2: INCREASE TIMEOUT (Handle larger slices)
print("[OPTION 2] Increase timeout for larger batches")
print("-"*70)
print("  SLICE_SIZE: 500 PDFs (current)")
print("  Timeout: 28800 sec (8h) UP from 4h")
print("  Est. time per job: 60-90 min")
print("  Total jobs: 391 jobs")
print("  Cost: ~$80-100")
print("  Risk: MEDIUM (longer jobs = more retries if fail)")
print()

# Recommendation
print("[RECOMMENDATION] Use OPTION 1")
print("-"*70)
print()
print("Why: 250 PDFs per job is optimal because:")
print("  - Completes in <45 min (well under 4h timeout)")
print("  - If fails, quick to retry")
print("  - Uses free tier more efficiently (shorter jobs = cheaper)")
print("  - Lower memory footprint")
print()

# Create optimized job definition
print("[CREATING] Optimized Job Definition")
print("-"*70)

try:
    jd_response = batch.register_job_definition(
        jobDefinitionName='osu-pipeline-optimized',
        type='container',
        containerProperties={
            'image': '225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline:v6-fixed',
            'vcpus': 1,  # Reduce from 2 to 1 vCPU (cheaper, 250 PDFs don't need 2)
            'memory': 2048,  # Reduce from 3000 to 2048
            'jobRoleArn': 'arn:aws:iam::225989338968:role/osu-batch-task-role',
            'environment': [
                {'name': 'SLICE_SIZE', 'value': '250'},  # REDUCED
                {'name': 'MAX_WORKERS', 'value': '2'},   # REDUCED from 4
                {'name': 'AWS_DEFAULT_REGION', 'value': 'us-east-1'},
                {'name': 'PYTHONUNBUFFERED', 'value': '1'}
            ],
            'logConfiguration': {
                'logDriver': 'awslogs',
                'options': {
                    'awslogs-group': '/aws/batch/osu-pipeline',
                    'awslogs-region': 'us-east-1',
                    'awslogs-stream-prefix': 'job-opt'
                }
            }
        },
        timeout={'attemptDurationSeconds': 14400}  # Keep 4h, but jobs complete in <1h
    )

    REV = jd_response['revision']
    print(f"  Created: osu-pipeline-optimized:{REV}")
    print()

    # Submit test slice
    print("[TEST] Submitting test slice...")
    test_job = batch.submit_job(
        jobName='osu-test-optimized',
        jobQueue='osu-pipeline-queue',
        jobDefinition=f'osu-pipeline-optimized:{REV}',
        containerOverrides={
            'environment': [
                {'name': 'SLICE_NUM', 'value': '1'},
                {'name': 'SLICE_SIZE', 'value': '250'},
                {'name': 'INPUT_BUCKET', 'value': 'osu-well-records-225989338968'},
                {'name': 'OUTPUT_BUCKET', 'value': 'osu-pipeline-results'},
                {'name': 'INDEX_KEY', 'value': 'collections_index.json'},
                {'name': 'GOOGLE_CREDS_SECRET_ID', 'value': 'osu-pipeline/gemini-api-key'},
                {'name': 'RDS_CREDS_SECRET_ID', 'value': 'osu-pipeline/rds'}
            ]
        }
    )

    print(f"  Test job: {test_job['jobId']}")
    print()
    print("[NEXT] Monitor this test job:")
    print(f"  aws batch describe-jobs --jobs {test_job['jobId']} --region {REGION}")
    print()
    print("If test succeeds in <1 hour, submit all 782 slices with:")
    print(f"  JOBDEF='osu-pipeline-optimized:{REV}'")
    print("  for slice in {{1..782}}: submit_job(JOBDEF, SLICE_SIZE=250)")
    print()

except Exception as e:
    print(f"ERROR: {e}")

print("="*70)
print()
print("COST COMPARISON")
print("-"*70)
print()
print("Current (500 PDF/job, 4h timeout):")
print("  391 jobs × $0.10/hour × 4h = $156")
print()
print("Optimized (250 PDF/job, 1vCPU, 45min avg):")
print("  782 jobs × $0.05/hour × 0.75h = $29")
print()
print("LOCAL (no AWS compute):")
print("  $0 compute + $5 S3 storage = $5")
print()
print("SAVINGS: $150+ by going local or optimized batch")
print()

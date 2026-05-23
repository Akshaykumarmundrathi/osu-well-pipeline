"""
Submit a diagnostic Batch job that prints container filesystem info
without running run_batch_job.py's main logic. Used to debug PYTHONPATH.
"""
import boto3

REGION = "us-east-1"
JOB_QUEUE = "osu-pipeline-queue"
JOB_DEF   = "osu-pipeline-job:12"  # rev 12 with PYTHONPATH env var

batch = boto3.client("batch", region_name=REGION)

# Diagnostic command: override the entrypoint via containerOverrides
# Since the image ENTRYPOINT is ["python", "/app/run_batch_job.py"],
# we can't easily change it. But we CAN check what environment vars
# and files exist by submitting a different command that exits quickly.

# Use Python to print diagnostics (this will be ARGS to the ENTRYPOINT,
# which means it will run: python /app/run_batch_job.py -c "print(...)".
# run_batch_job.py ignores extra args, so we can't do this.

# Instead, register a minimal one-off job def with command override:
print("Registering diagnostic job definition...")
resp = batch.register_job_definition(
    jobDefinitionName="osu-diag",
    type="container",
    containerProperties={
        "image": "225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline:v6-fixed",
        "jobRoleArn": "arn:aws:iam::225989338968:role/osu-batch-task-role",
        "executionRoleArn": "arn:aws:iam::225989338968:role/osu-batch-execution-role",
        "command": [
            "python", "-c",
            (
                "import sys, os, subprocess;"
                "print('PYTHONPATH env:', os.environ.get('PYTHONPATH','<not set>'));"
                "print('sys.path:', sys.path[:8]);"
                "import subprocess;"
                "r = subprocess.run(['ls', '/app'], capture_output=True, text=True);"
                "print('ls /app:', r.stdout[:500]);"
                "r2 = subprocess.run(['ls', '/app/project'], capture_output=True, text=True);"
                "print('ls /app/project:', r2.stdout[:200] if r2.returncode==0 else 'NOT FOUND');"
                "print('Done');"
            )
        ],
        "environment": [
            {"name": "PYTHONPATH", "value": "/app/project:/app"},
            {"name": "INPUT_BUCKET", "value": "osu-well-records-225989338968"},
            {"name": "OUTPUT_BUCKET", "value": "osu-pipeline-results"},
            {"name": "INDEX_KEY", "value": "index/dataset_index.csv"},
        ],
        "resourceRequirements": [
            {"value": "1", "type": "VCPU"},
            {"value": "2048", "type": "MEMORY"},
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": "/aws/batch/osu-pipeline",
                "awslogs-region": "us-east-1",
                "awslogs-stream-prefix": "diag",
            },
        },
        "networkConfiguration": {"assignPublicIp": "ENABLED"},
        "fargatePlatformConfiguration": {"platformVersion": "LATEST"},
        "ephemeralStorage": {"sizeInGiB": 21},
    },
    platformCapabilities=["FARGATE"],
)
jd_arn = resp["jobDefinitionArn"]
print(f"Registered: {jd_arn}")

# Submit it
sub = batch.submit_job(
    jobName="osu-diag-container",
    jobQueue=JOB_QUEUE,
    jobDefinition=jd_arn,
)
print(f"Submitted diagnostic job: {sub['jobId']}")
print("Watch CloudWatch: /aws/batch/osu-pipeline / prefix diag/")
print(f"CLI check: aws batch describe-jobs --jobs {sub['jobId']} --region us-east-1")

"""
Register job definition revision 13: shell-wrapper entrypoint that
hard-codes PYTHONPATH=/app/project:/app before starting run_batch_job.py.

Why: PYTHONPATH in the Batch job definition environment IS present in
os.environ of run_batch_job.py, but the subprocess call to main.py still
fails with ModuleNotFoundError. Using a shell wrapper guarantees the path
is set before Python starts either process.
"""

import json
import boto3

REGION = "us-east-1"
JOB_DEF_NAME = "osu-pipeline-job"
SOURCE_REVISION = 12

batch = boto3.client("batch", region_name=REGION)

# Fetch the current rev 12 definition
resp = batch.describe_job_definitions(
    jobDefinitionName=JOB_DEF_NAME,
    status="ACTIVE",
)
jds = [j for j in resp["jobDefinitions"] if j["revision"] == SOURCE_REVISION]
if not jds:
    raise RuntimeError(f"Job def {JOB_DEF_NAME}:{SOURCE_REVISION} not found")

base = jds[0]
cp = {k: v for k, v in base["containerProperties"].items()
      if k not in ("jobRoleArn",)}  # jobRoleArn moved to top level in some API calls

# ---------------------------------------------------------------
# Fix: use a shell wrapper as entrypoint so PYTHONPATH is
# guaranteed to be set before Python starts, regardless of
# how Batch injects environment variables.
# ---------------------------------------------------------------
cp["entryPoint"] = ["/bin/sh", "-c"]
cp["command"] = [
    "export PYTHONPATH=/app/project:/app && "
    "export PYTHONUNBUFFERED=1 && "
    "exec python /app/run_batch_job.py"
]

# Keep jobRoleArn at the top-level field
job_role_arn = base["containerProperties"].get("jobRoleArn", "")

# Build the register call
register_kwargs = dict(
    jobDefinitionName=JOB_DEF_NAME,
    type="container",
    containerProperties={
        **cp,
        "jobRoleArn": job_role_arn,
    },
    retryStrategy=base["retryStrategy"],
    timeout=base.get("timeout", {"attemptDurationSeconds": 86400}),
    tags=base.get("tags", {}),
    platformCapabilities=["FARGATE"],
)

print("Registering rev 13 with shell-wrapper entrypoint...")
result = batch.register_job_definition(**register_kwargs)
print(f"Registered: {result['jobDefinitionName']}:{result['revision']}")
print(f"ARN: {result['jobDefinitionArn']}")

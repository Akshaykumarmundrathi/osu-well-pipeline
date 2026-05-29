"""
aws/setup_account2.py — One-shot idempotent Account 2 infrastructure setup.
============================================================================

Creates (or verifies) all 9 resources needed to run the OSU well pipeline
in AWS Account 2 ("mano"), then writes aws/.env.account2 with the env vars
needed by orchestrate_robust.py and monitor.py.

Resources (in dependency order)
--------------------------------
 1. ECR cross-account pull policy on Account 1 (allows Account 2 to pull)
 2. S3 bucket  osu-pipeline-results-mano          (Account 2)
 3. CloudWatch log group /aws/batch/osu-pipeline  (Account 2)
 4. IAM role  osu-batch-execution-role            (Account 2)
 5. IAM role  osu-batch-task-role                 (Account 2)
 6. Secrets Manager  osu-pipeline/credentials     (Account 2)
 7. Batch compute environment  osu-pipeline-ce    (Account 2)
 8. Batch job queue  osu-pipeline-queue           (Account 2)
 9. Batch job definition  osu-pipeline-job        (Account 2)

Usage
-----
    python aws/setup_account2.py --profile mano
    python aws/setup_account2.py --profile mano --dry-run
    python aws/setup_account2.py --profile mano --skip-ecr-policy
                                  # if you don't have Account 1 credentials

After running:
    # Windows
    for /f "tokens=1,2 delims==" %a in (aws\\.env.account2) do set %a=%b
    python aws/orchestrate_robust.py --slice-size 1000 --workers 4

    # bash / WSL
    set -a && source aws/.env.account2 && set +a
    python aws/orchestrate_robust.py --slice-size 1000 --workers 4
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Account 1 — ECR + source PDFs (read-only by Account 2)
ACCT1_ID       = "225989338968"
ACCT1_REGION   = "us-east-1"
ACCT1_BUCKET   = "osu-well-records-225989338968"
ECR_REPO       = "osu-pipeline"
ECR_TAG        = "v15-skip-anchor"
ECR_IMAGE      = f"{ACCT1_ID}.dkr.ecr.{ACCT1_REGION}.amazonaws.com/{ECR_REPO}:{ECR_TAG}"

# Account 2 — everything else
ACCT2_REGION   = "us-east-1"
OUTPUT_BUCKET  = "osu-pipeline-results-mano"
LOG_GROUP      = "/aws/batch/osu-pipeline"
LOG_RETENTION  = 7          # days
EXEC_ROLE_NAME = "osu-batch-execution-role"
TASK_ROLE_NAME = "osu-batch-task-role"
SECRET_NAME    = "osu-pipeline/credentials"
CE_NAME        = "osu-pipeline-ce"
QUEUE_NAME     = "osu-pipeline-queue"
JOB_DEF_NAME   = "osu-pipeline-job"

# Batch sizing
VCPU_MAX       = 4096       # max vCPUs for the compute environment
JOB_VCPU       = "4"
JOB_MEM_MB     = "16384"
JOB_STORAGE_GB = 50
GEMINI_MODEL   = "gemini-2.0-flash-lite"

_CFG = Config(retries={"mode": "adaptive", "max_attempts": 6})

# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _load_gcp_creds() -> str:
    """Read the GCP service account JSON from the known local path."""
    candidates = [
        Path(__file__).parent.parent / "smiling-breaker-423712-h3-67b25396bf65.json",
        Path(__file__).parent.parent / "smiling-breaker-423712-h3-aff7ac746ad4.json",
        Path(r"D:\project_modular\smiling-breaker-423712-h3-67b25396bf65.json"),
    ]
    for p in candidates:
        if p.exists():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "GCP service account JSON not found. Expected one of:\n"
        + "\n".join(f"  {p}" for p in candidates)
    )


def _get_gemini_key() -> str:
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        # Try loading from local .env
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith(("GOOGLE_API_KEY=", "GEMINI_API_KEY=")):
                    key = line.split("=", 1)[1].strip().strip('"\'')
                    break
    if not key:
        raise ValueError(
            "Gemini API key not found. Set GOOGLE_API_KEY env var or add to .env file."
        )
    return key


# ---------------------------------------------------------------------------
# AWS client factories
# ---------------------------------------------------------------------------

def _clients(session: boto3.Session) -> dict:
    kw = dict(region_name=ACCT2_REGION, config=_CFG)
    return {
        "iam":    session.client("iam",           **kw),
        "s3":     session.client("s3",            **kw),
        "logs":   session.client("logs",          **kw),
        "sm":     session.client("secretsmanager",**kw),
        "batch":  session.client("batch",         **kw),
        "ec2":    session.client("ec2",           **kw),
        "sts":    session.client("sts",           **kw),
    }


def _acct1_clients(session1: boto3.Session) -> dict:
    return {
        "ecr": session1.client("ecr", region_name=ACCT1_REGION, config=_CFG),
    }


# ---------------------------------------------------------------------------
# Helper: idempotent waiter
# ---------------------------------------------------------------------------

def _wait(label: str, check_fn, retries: int = 30, interval: int = 6):
    for i in range(1, retries + 1):
        result = check_fn()
        if result:
            return result
        print(f"    [{i}/{retries}] waiting for {label} ...", flush=True)
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {label}")


# ---------------------------------------------------------------------------
# 1. ECR cross-account pull policy (Account 1)
# ---------------------------------------------------------------------------

def setup_ecr_policy(c1: dict, acct2_id: str, dry_run: bool):
    print("\n[1/9] ECR cross-account pull policy (Account 1) ...")
    ecr = c1["ecr"]

    new_stmt = {
        "Sid":       "AllowAccount2Pull",
        "Effect":    "Allow",
        "Principal": {"AWS": f"arn:aws:iam::{acct2_id}:root"},
        "Action": [
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchGetImage",
            "ecr:BatchCheckLayerAvailability",
            "ecr:GetAuthorizationToken",
        ],
    }

    try:
        existing = json.loads(
            ecr.get_repository_policy(repositoryName=ECR_REPO)["policyText"]
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "RepositoryPolicyNotFoundException":
            existing = {"Version": "2012-10-17", "Statement": []}
        else:
            raise

    stmts = existing.get("Statement", [])
    if any(s.get("Sid") == "AllowAccount2Pull" for s in stmts):
        print(f"    ECR policy already grants Account 2 pull access — no change.")
        return

    stmts.append(new_stmt)
    policy = json.dumps({"Version": "2012-10-17", "Statement": stmts})
    if dry_run:
        print(f"    [DRY RUN] would set ECR repo policy on {ECR_REPO}")
        return
    ecr.set_repository_policy(repositoryName=ECR_REPO, policyText=policy)
    print(f"    ECR repo policy updated — Account 2 ({acct2_id}) can pull {ECR_TAG}.")


# ---------------------------------------------------------------------------
# 2. S3 output bucket (Account 2)
# ---------------------------------------------------------------------------

def setup_s3_bucket(c: dict, dry_run: bool):
    print(f"\n[2/9] S3 bucket s3://{OUTPUT_BUCKET} (Account 2) ...")
    s3 = c["s3"]

    try:
        s3.head_bucket(Bucket=OUTPUT_BUCKET)
        print(f"    Bucket already exists — no change.")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise

    if dry_run:
        print(f"    [DRY RUN] would create s3://{OUTPUT_BUCKET}")
        return

    kwargs = {"Bucket": OUTPUT_BUCKET}
    if ACCT2_REGION != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": ACCT2_REGION}
    s3.create_bucket(**kwargs)
    # Keep versioning off, block all public access
    s3.put_public_access_block(
        Bucket=OUTPUT_BUCKET,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    )
    print(f"    Created s3://{OUTPUT_BUCKET} (private, versioning off).")


# ---------------------------------------------------------------------------
# 3. CloudWatch log group (Account 2)
# ---------------------------------------------------------------------------

def setup_log_group(c: dict, dry_run: bool):
    print(f"\n[3/9] CloudWatch log group {LOG_GROUP} (Account 2) ...")
    logs = c["logs"]

    try:
        resp = logs.describe_log_groups(logGroupNamePrefix=LOG_GROUP)
        for lg in resp.get("logGroups", []):
            if lg["logGroupName"] == LOG_GROUP:
                print(f"    Log group already exists — no change.")
                return
    except Exception:
        pass

    if dry_run:
        print(f"    [DRY RUN] would create log group {LOG_GROUP}")
        return

    logs.create_log_group(logGroupName=LOG_GROUP)
    logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=LOG_RETENTION)
    print(f"    Created log group {LOG_GROUP} (retention={LOG_RETENTION}d).")


# ---------------------------------------------------------------------------
# 4. IAM execution role (Account 2)
# ---------------------------------------------------------------------------

def setup_exec_role(c: dict, acct2_id: str, dry_run: bool) -> str:
    print(f"\n[4/9] IAM role {EXEC_ROLE_NAME} (Account 2) ...")
    iam = c["iam"]
    arn = f"arn:aws:iam::{acct2_id}:role/{EXEC_ROLE_NAME}"

    try:
        iam.get_role(RoleName=EXEC_ROLE_NAME)
        print(f"    Role already exists — no change.")
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    trust = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    })
    inline_sm = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": f"arn:aws:secretsmanager:{ACCT2_REGION}:{acct2_id}:secret:osu-pipeline/*",
        }],
    })

    if dry_run:
        print(f"    [DRY RUN] would create role {EXEC_ROLE_NAME}")
        return arn

    iam.create_role(
        RoleName=EXEC_ROLE_NAME,
        AssumeRolePolicyDocument=trust,
        Description="ECS task execution role for OSU pipeline (Batch/Fargate)",
    )
    iam.attach_role_policy(
        RoleName=EXEC_ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
    )
    iam.put_role_policy(
        RoleName=EXEC_ROLE_NAME,
        PolicyName="SecretsManagerRead",
        PolicyDocument=inline_sm,
    )
    print(f"    Created {EXEC_ROLE_NAME} (ECSTaskExecution + SM read).")
    return arn


# ---------------------------------------------------------------------------
# 5. IAM task role (Account 2)
# ---------------------------------------------------------------------------

def setup_task_role(c: dict, acct2_id: str, dry_run: bool) -> str:
    print(f"\n[5/9] IAM role {TASK_ROLE_NAME} (Account 2) ...")
    iam = c["iam"]
    arn = f"arn:aws:iam::{acct2_id}:role/{TASK_ROLE_NAME}"

    try:
        iam.get_role(RoleName=TASK_ROLE_NAME)
        print(f"    Role already exists — no change.")
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    trust = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    })
    inline_s3 = json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadSourcePDFs",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{ACCT1_BUCKET}",
                    f"arn:aws:s3:::{ACCT1_BUCKET}/*",
                ],
            },
            {
                "Sid": "WriteResults",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject",
                           "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{OUTPUT_BUCKET}",
                    f"arn:aws:s3:::{OUTPUT_BUCKET}/*",
                ],
            },
        ],
    })

    if dry_run:
        print(f"    [DRY RUN] would create role {TASK_ROLE_NAME}")
        return arn

    iam.create_role(
        RoleName=TASK_ROLE_NAME,
        AssumeRolePolicyDocument=trust,
        Description="ECS task role for OSU pipeline — S3 read/write",
    )
    iam.put_role_policy(
        RoleName=TASK_ROLE_NAME,
        PolicyName="S3ReadWrite",
        PolicyDocument=inline_s3,
    )
    print(f"    Created {TASK_ROLE_NAME} (S3 GetObject on Acct1 + PutObject on Acct2).")
    return arn


# ---------------------------------------------------------------------------
# 6. Secrets Manager secret (Account 2)
# ---------------------------------------------------------------------------

def setup_secret(c: dict, dry_run: bool):
    print(f"\n[6/9] Secrets Manager '{SECRET_NAME}' (Account 2) ...")
    sm = c["sm"]

    gcp_json = _load_gcp_creds()
    gemini_key = _get_gemini_key()
    secret_value = json.dumps({
        "gcp_service_account": gcp_json,
        "gemini_api_key":      gemini_key,
    })

    try:
        sm.describe_secret(SecretId=SECRET_NAME)
        if dry_run:
            print(f"    [DRY RUN] secret exists — would rotate value")
            return
        sm.put_secret_value(SecretId=SECRET_NAME, SecretString=secret_value)
        print(f"    Secret already exists — value updated with latest credentials.")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    if dry_run:
        print(f"    [DRY RUN] would create secret '{SECRET_NAME}'")
        return

    sm.create_secret(
        Name=SECRET_NAME,
        SecretString=secret_value,
        Description="GCP service account + Gemini API key for OSU well pipeline",
    )
    print(f"    Created secret '{SECRET_NAME}' (GCP SA + Gemini key).")


# ---------------------------------------------------------------------------
# 7. Batch compute environment (Account 2)
# ---------------------------------------------------------------------------

def _get_default_vpc_subnets(ec2) -> tuple[str, list[str]]:
    """Return (vpc_id, [subnet_id, ...]) for the default VPC."""
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    vpc_id = vpcs["Vpcs"][0]["VpcId"]
    subnets = ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )
    subnet_ids = [s["SubnetId"] for s in subnets["Subnets"]]
    return vpc_id, subnet_ids


def setup_compute_env(c: dict, acct2_id: str, dry_run: bool):
    print(f"\n[7/9] Batch compute environment '{CE_NAME}' (Account 2) ...")
    batch = c["batch"]
    ec2   = c["ec2"]

    try:
        resp = batch.describe_compute_environments(computeEnvironments=[CE_NAME])
        envs = resp.get("computeEnvironments", [])
        if envs:
            state = envs[0].get("state", "?")
            status = envs[0].get("status", "?")
            print(f"    Already exists: state={state} status={status} — no change.")
            return
    except ClientError:
        pass

    vpc_id, subnet_ids = _get_default_vpc_subnets(ec2)
    print(f"    Default VPC: {vpc_id}  Subnets: {subnet_ids}")

    if dry_run:
        print(f"    [DRY RUN] would create CE '{CE_NAME}' (FARGATE_SPOT, maxvCpus={VCPU_MAX})")
        return

    batch.create_compute_environment(
        computeEnvironmentName=CE_NAME,
        type="MANAGED",
        state="ENABLED",
        computeResources={
            "type":          "FARGATE_SPOT",
            "maxvCpus":      VCPU_MAX,
            "subnets":       subnet_ids,
            "securityGroupIds": [],   # Batch creates a default SG
        },
    )

    # Wait for VALID status
    def _check():
        r = batch.describe_compute_environments(computeEnvironments=[CE_NAME])
        envs = r.get("computeEnvironments", [])
        if envs and envs[0].get("status") == "VALID":
            return True
        return False

    _wait(f"CE {CE_NAME} VALID", _check, retries=20, interval=10)
    print(f"    Created CE '{CE_NAME}' (FARGATE_SPOT, maxvCpus={VCPU_MAX}).")


# ---------------------------------------------------------------------------
# 8. Batch job queue (Account 2)
# ---------------------------------------------------------------------------

def setup_job_queue(c: dict, dry_run: bool):
    print(f"\n[8/9] Batch job queue '{QUEUE_NAME}' (Account 2) ...")
    batch = c["batch"]

    try:
        resp = batch.describe_job_queues(jobQueues=[QUEUE_NAME])
        queues = resp.get("jobQueues", [])
        if queues:
            state = queues[0].get("state", "?")
            status = queues[0].get("status", "?")
            print(f"    Already exists: state={state} status={status} — no change.")
            return
    except ClientError:
        pass

    if dry_run:
        print(f"    [DRY RUN] would create queue '{QUEUE_NAME}'")
        return

    batch.create_job_queue(
        jobQueueName=QUEUE_NAME,
        state="ENABLED",
        priority=1,
        computeEnvironmentOrder=[{"order": 1, "computeEnvironment": CE_NAME}],
    )

    def _check():
        r = batch.describe_job_queues(jobQueues=[QUEUE_NAME])
        qs = r.get("jobQueues", [])
        return qs and qs[0].get("status") == "VALID"

    _wait(f"queue {QUEUE_NAME} VALID", _check, retries=20, interval=6)
    print(f"    Created queue '{QUEUE_NAME}' (priority=1, CE={CE_NAME}).")


# ---------------------------------------------------------------------------
# 9. Batch job definition (Account 2)
# ---------------------------------------------------------------------------

def setup_job_def(c: dict, exec_role_arn: str, task_role_arn: str, dry_run: bool):
    print(f"\n[9/9] Batch job definition '{JOB_DEF_NAME}' (Account 2) ...")
    batch = c["batch"]

    if dry_run:
        print(f"    [DRY RUN] would register job def '{JOB_DEF_NAME}'")
        return

    resp = batch.register_job_definition(
        jobDefinitionName=JOB_DEF_NAME,
        type="container",
        platformCapabilities=["FARGATE"],
        containerProperties={
            "image":            ECR_IMAGE,
            "executionRoleArn": exec_role_arn,
            "jobRoleArn":       task_role_arn,
            "resourceRequirements": [
                {"type": "VCPU",   "value": JOB_VCPU},
                {"type": "MEMORY", "value": JOB_MEM_MB},
            ],
            "ephemeralStorage": {"sizeInGiB": JOB_STORAGE_GB},
            "environment": [
                {"name": "PYTHONUNBUFFERED",       "value": "1"},
                {"name": "PYTHONPATH",             "value": "/app/project:/app"},
                {"name": "AWS_DEFAULT_REGION",     "value": ACCT2_REGION},
                {"name": "SLICE_SIZE",             "value": "1000"},
                {"name": "MAX_WORKERS",            "value": "4"},
                {"name": "WORKERS",                "value": "4"},
                {"name": "CHECKPOINT_INTERVAL_S",  "value": "240"},
                {"name": "GEMINI_MIN_CALL_GAP_S",  "value": "2"},
                {"name": "USE_VISION_API",         "value": "0"},
                {"name": "DISK_WARN_GB",           "value": "8"},
                {"name": "DISK_PRUNE_GB",          "value": "4"},
                {"name": "GEMINI_FLASH_MODEL",     "value": GEMINI_MODEL},
                {"name": "INPUT_BUCKET",           "value": ACCT1_BUCKET},
                {"name": "OUTPUT_BUCKET",          "value": OUTPUT_BUCKET},
                {"name": "INDEX_KEY",              "value": "index/dataset_index.csv"},
                {"name": "GOOGLE_CREDS_SECRET_ID", "value": SECRET_NAME},
                {"name": "RDS_CREDS_SECRET_ID",    "value": "osu-pipeline/rds"},
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group":         LOG_GROUP,
                    "awslogs-region":        ACCT2_REGION,
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
        timeout={"attemptDurationSeconds": 28800},  # 8 hours max
    )
    rev = resp["revision"]
    print(f"    Registered '{JOB_DEF_NAME}' revision {rev}.")
    print(f"    Image: {ECR_IMAGE}")


# ---------------------------------------------------------------------------
# Write .env.account2
# ---------------------------------------------------------------------------

def _write_env_file(acct2_id: str, exec_arn: str, task_arn: str):
    env_path = Path(__file__).parent / ".env.account2"
    content = f"""\
# Generated by setup_account2.py — source this before running orchestrate_robust.py
# Windows: for /f "tokens=1,2 delims==" %a in (aws\\.env.account2) do set %a=%b
# bash/WSL: set -a && source aws/.env.account2 && set +a

# Account identifiers
ACCOUNT1_ID={ACCT1_ID}
ACCOUNT2_ID={acct2_id}
AWS_DEFAULT_REGION={ACCT2_REGION}

# S3
INPUT_BUCKET={ACCT1_BUCKET}
OUTPUT_BUCKET={OUTPUT_BUCKET}
INDEX_KEY=index/dataset_index.csv

# ECR image (lives in Account 1)
ECR_IMAGE={ECR_IMAGE}

# Batch
JOB_QUEUE={QUEUE_NAME}
JOB_DEF_NAME={JOB_DEF_NAME}
LOG_GROUP={LOG_GROUP}

# IAM roles (Account 2)
EXEC_ROLE_ARN={exec_arn}
TASK_ROLE_ARN={task_arn}

# Gemini model
GEMINI_FLASH_MODEL={GEMINI_MODEL}
"""
    env_path.write_text(content, encoding="utf-8")
    print(f"\n  Wrote {env_path}")
    print("  To load on Windows:")
    print(r'    for /f "tokens=1,2 delims==" %a in (aws\.env.account2) do set %a=%b')
    print("  To load on bash/WSL:")
    print("    set -a && source aws/.env.account2 && set +a")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Create all Account 2 resources for the OSU well pipeline"
    )
    ap.add_argument("--profile",        default="mano",
                    help="AWS CLI profile for Account 2 (default: mano)")
    ap.add_argument("--profile-acct1",  default=None,
                    help="AWS CLI profile for Account 1 ECR policy (default: default profile)")
    ap.add_argument("--dry-run",        action="store_true",
                    help="Print what would be created without touching AWS")
    ap.add_argument("--skip-ecr-policy", action="store_true",
                    help="Skip ECR cross-account policy (if Account 1 creds unavailable)")
    args = ap.parse_args()

    print("=" * 65)
    print("  OSU Well Pipeline — Account 2 Setup")
    print("=" * 65)
    print(f"  Account 2 profile : {args.profile}")
    print(f"  Account 1 profile : {args.profile_acct1 or '(default)'}")
    print(f"  Dry run           : {args.dry_run}")
    print(f"  ECR image         : {ECR_IMAGE}")
    print()

    # Account 2 session
    sess2 = boto3.Session(profile_name=args.profile)
    c2    = _clients(sess2)

    # Verify Account 2 identity
    try:
        ident = c2["sts"].get_caller_identity()
        acct2_id = ident["Account"]
        print(f"  Account 2 identity: {ident['Arn']}")
        print(f"  Account 2 ID      : {acct2_id}")
    except Exception as e:
        print(f"ERROR: Cannot authenticate to Account 2 (profile '{args.profile}'): {e}")
        print("  Make sure 'mano' is configured in ~/.aws/credentials or ~/.aws/config")
        sys.exit(1)

    # Derived ARNs
    exec_arn = f"arn:aws:iam::{acct2_id}:role/{EXEC_ROLE_NAME}"
    task_arn = f"arn:aws:iam::{acct2_id}:role/{TASK_ROLE_NAME}"

    # Account 1 session (for ECR policy only)
    if not args.skip_ecr_policy:
        try:
            sess1 = boto3.Session(profile_name=args.profile_acct1)
            c1    = _acct1_clients(sess1)
            ident1 = sess1.client("sts").get_caller_identity()
            print(f"  Account 1 identity: {ident1['Arn']}")
        except Exception as e:
            print(f"\nWARN: Cannot authenticate to Account 1: {e}")
            print("  Skipping ECR cross-account policy setup.")
            print("  Re-run with --skip-ecr-policy if Account 1 creds not available,")
            print("  or configure the ECR policy manually in the AWS console.")
            args.skip_ecr_policy = True
            c1 = None
    else:
        c1 = None

    print()

    # --- Execute all steps ---

    if not args.skip_ecr_policy and c1:
        setup_ecr_policy(c1, acct2_id, args.dry_run)

    setup_s3_bucket(c2,    args.dry_run)
    setup_log_group(c2,    args.dry_run)
    exec_arn = setup_exec_role(c2, acct2_id, args.dry_run) or exec_arn
    task_arn = setup_task_role(c2, acct2_id, args.dry_run) or task_arn

    # Wait a few seconds for IAM roles to propagate before Batch uses them
    if not args.dry_run:
        print("\n  Waiting 10s for IAM roles to propagate ...")
        time.sleep(10)

    setup_secret(c2,   args.dry_run)
    setup_compute_env(c2, acct2_id, args.dry_run)
    setup_job_queue(c2, args.dry_run)
    setup_job_def(c2,  exec_arn, task_arn, args.dry_run)

    # Write env file
    _write_env_file(acct2_id, exec_arn, task_arn)

    print("\n" + "=" * 65)
    if args.dry_run:
        print("  DRY RUN complete — no AWS resources were created.")
    else:
        print("  Setup complete!  All 9 resources created / verified.")
        print()
        print("  Next steps:")
        print("  1. Load env vars:")
        print(r'       Windows: for /f "tokens=1,2 delims==" %a in (aws\.env.account2) do set %a=%b')
        print("       bash/WSL: set -a && source aws/.env.account2 && set +a")
        print("  2. Submit the pipeline:")
        print("       python aws/orchestrate_robust.py --slice-size 1000 --workers 4")
        print("  3. In another terminal, monitor progress:")
        print("       python aws/monitor.py")
    print("=" * 65)


if __name__ == "__main__":
    main()

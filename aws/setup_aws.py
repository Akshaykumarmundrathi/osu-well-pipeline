"""
setup_aws.py  —  Idempotent AWS resource setup for Account 2 (compute)
=======================================================================
Creates / verifies all AWS resources needed to run the pipeline on Batch.
Safe to re-run — all operations check before creating.

Requires AWS credentials for Account 2 (mano — compute).
Configure via:
    aws configure --profile mano
    export AWS_PROFILE=mano

Also patches Account 1's S3 bucket policy for cross-account access.
Requires Account 1 credentials too:
    aws configure --profile akshay
    export AWS_PROFILE_ACCOUNT1=akshay

Usage:
    python aws/setup_aws.py
    python aws/setup_aws.py --dry-run          # print what would be created
    python aws/setup_aws.py --skip-bucket-policy  # if Account 1 access not available
"""

import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

# ── Config ───────────────────────────────────────────────────────────────────
ACCOUNT1_ID    = os.environ["ACCOUNT1_ID"]   # Akshay — data (S3 + RDS)
ACCOUNT2_ID    = os.environ["ACCOUNT2_ID"]   # mano   — compute ($100 credits)
REGION         = "us-east-1"
BUCKET         = os.environ.get("S3_BUCKET") or f"osu-well-records-{ACCOUNT1_ID}"

ECR_REPO       = "osu-well-pipeline"
BATCH_CE_NAME  = "osu-pipeline-ce"
BATCH_QUEUE    = "osu-pipeline-queue"
BATCH_JOB_DEF  = "osu-pipeline-job"
SECRETS_PREFIX = "osu/"
LOG_GROUP      = "/aws/batch/osu-pipeline"
TASK_ROLE_NAME = "osu-task-role"
EXEC_ROLE_NAME = "osu-batch-execution-role"
SNS_TOPIC_NAME = "osu-pipeline-alerts"
DASHBOARD_NAME = "OsuPipeline"

# Fargate Spot config per pipeline container
VCPU   = "2"
MEMORY = "4096"   # MB


def _p(msg): print(msg, flush=True)


# ── Clients ──────────────────────────────────────────────────────────────────

def _clients(profile2: str, profile1: str):
    sess2 = boto3.Session(profile_name=profile2, region_name=REGION)
    sess1 = boto3.Session(profile_name=profile1, region_name=REGION) if profile1 else None
    return {
        "iam":    sess2.client("iam"),
        "ecr":    sess2.client("ecr"),
        "batch":  sess2.client("batch"),
        "sm":     sess2.client("secretsmanager"),
        "logs":   sess2.client("logs"),
        "sns":    sess2.client("sns"),
        "cw":     sess2.client("cloudwatch"),
        "s3_a1":  sess1.client("s3") if sess1 else None,
        "ec2":    sess2.client("ec2"),
    }


# ── IAM ──────────────────────────────────────────────────────────────────────

_TASK_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {   # Cross-account S3 read (source PDFs) + write (outputs)
            "Effect":   "Allow",
            "Action":   ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                         "s3:ListBucket", "s3:GetBucketLocation"],
            "Resource": [f"arn:aws:s3:::{BUCKET}",
                         f"arn:aws:s3:::{BUCKET}/*"],
        },
        {   # Secrets Manager: read all osu/* secrets
            "Effect":   "Allow",
            "Action":   ["secretsmanager:GetSecretValue"],
            "Resource": [f"arn:aws:secretsmanager:{REGION}:{ACCOUNT2_ID}:secret:{SECRETS_PREFIX}*"],
        },
        {   # CloudWatch logs + custom metrics
            "Effect":   "Allow",
            "Action":   ["logs:CreateLogGroup", "logs:CreateLogStream",
                         "logs:PutLogEvents", "cloudwatch:PutMetricData"],
            "Resource": "*",
        },
    ],
}

_EXEC_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["ecr:GetAuthorizationToken",
                       "ecr:BatchCheckLayerAvailability",
                       "ecr:GetDownloadUrlForLayer",
                       "ecr:BatchGetImage",
                       "logs:CreateLogStream",
                       "logs:PutLogEvents"],
            "Resource": "*",
        },
    ],
}


def _ensure_role(iam, name: str, service: str, inline_policy: dict, dry_run: bool) -> str:
    """Create IAM role if it doesn't exist. Return role ARN."""
    try:
        r = iam.get_role(RoleName=name)
        arn = r["Role"]["Arn"]
        _p(f"  IAM role exists: {name}")
    except ClientError:
        if dry_run:
            _p(f"  [DRY] would create IAM role: {name}")
            return f"arn:aws:iam::{ACCOUNT2_ID}:role/{name}"
        trust = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect":    "Allow",
                "Principal": {"Service": service},
                "Action":    "sts:AssumeRole",
            }],
        }
        r = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=f"OSU well pipeline role: {name}",
        )
        arn = r["Role"]["Arn"]
        _p(f"  Created IAM role: {name}  ({arn})")

    # Attach / update inline policy
    if not dry_run:
        iam.put_role_policy(
            RoleName=name,
            PolicyName=f"{name}-policy",
            PolicyDocument=json.dumps(inline_policy),
        )
    return arn


# ── ECR ──────────────────────────────────────────────────────────────────────

def _ensure_ecr(ecr, dry_run: bool) -> str:
    """Create ECR repository if missing. Return registry URI."""
    try:
        r = ecr.describe_repositories(repositoryNames=[ECR_REPO])
        uri = r["repositories"][0]["repositoryUri"]
        _p(f"  ECR repo exists: {uri}")
        return uri
    except ClientError:
        if dry_run:
            _p(f"  [DRY] would create ECR repo: {ECR_REPO}")
            return f"{ACCOUNT2_ID}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}"
        r = ecr.create_repository(
            repositoryName=ECR_REPO,
            imageTagMutability="MUTABLE",
            imageScanningConfiguration={"scanOnPush": False},
        )
        uri = r["repository"]["repositoryUri"]
        _p(f"  Created ECR repo: {uri}")
        return uri


# ── Secrets Manager ───────────────────────────────────────────────────────────

def _ensure_secret(sm, name: str, description: str, placeholder: str, dry_run: bool):
    """Create secret placeholder if it doesn't exist."""
    full_name = f"{SECRETS_PREFIX}{name}"
    try:
        sm.describe_secret(SecretId=full_name)
        _p(f"  Secret exists: {full_name}")
    except ClientError:
        if dry_run:
            _p(f"  [DRY] would create secret: {full_name}")
            return
        sm.create_secret(
            Name=full_name,
            Description=description,
            SecretString=placeholder,
        )
        _p(f"  Created secret placeholder: {full_name}")
        _p(f"    → Update with real value: aws secretsmanager put-secret-value "
           f"--secret-id {full_name} --secret-string '...'")


# ── Batch ─────────────────────────────────────────────────────────────────────

def _get_default_subnets(ec2) -> list[str]:
    """Return default VPC subnet IDs."""
    try:
        vpcs   = ec2.describe_vpcs(Filters=[{"Name":"isDefault","Values":["true"]}])
        vpc_id = vpcs["Vpcs"][0]["VpcId"]
        subs   = ec2.describe_subnets(Filters=[{"Name":"vpcId","Values":[vpc_id]}])
        return [s["SubnetId"] for s in subs["Subnets"]]
    except Exception as e:
        _p(f"  WARNING: could not find default subnets: {e}")
        return []


def _get_default_sg(ec2) -> list[str]:
    """Return default security group ID."""
    try:
        sgs = ec2.describe_security_groups(
            Filters=[{"Name":"group-name","Values":["default"]}]
        )
        return [sg["GroupId"] for sg in sgs["SecurityGroups"][:1]]
    except Exception as e:
        _p(f"  WARNING: could not find default SG: {e}")
        return []


def _ensure_compute_env(batch, exec_role_arn: str, ec2, dry_run: bool):
    try:
        r = batch.describe_compute_environments(computeEnvironments=[BATCH_CE_NAME])
        if r["computeEnvironments"]:
            _p(f"  Compute env exists: {BATCH_CE_NAME}")
            return
    except ClientError:
        pass

    subnets = _get_default_subnets(ec2)
    sgs     = _get_default_sg(ec2)
    if dry_run:
        _p(f"  [DRY] would create compute env: {BATCH_CE_NAME}")
        return
    batch.create_compute_environment(
        computeEnvironmentName=BATCH_CE_NAME,
        type="MANAGED",
        state="ENABLED",
        computeResources={
            "type":             "FARGATE_SPOT",
            "maxvCpus":         200,
            "subnets":          subnets,
            "securityGroupIds": sgs,
        },
        serviceRole="",   # Batch manages its own service role for Fargate
    )
    _p(f"  Created compute env: {BATCH_CE_NAME} (Fargate SPOT, maxvCPUs=200)")


def _ensure_job_queue(batch, dry_run: bool):
    try:
        r = batch.describe_job_queues(jobQueues=[BATCH_QUEUE])
        if r["jobQueues"]:
            _p(f"  Job queue exists: {BATCH_QUEUE}")
            return
    except ClientError:
        pass
    if dry_run:
        _p(f"  [DRY] would create job queue: {BATCH_QUEUE}")
        return
    batch.create_job_queue(
        jobQueueName=BATCH_QUEUE,
        state="ENABLED",
        priority=100,
        computeEnvironmentOrder=[{"order": 1, "computeEnvironment": BATCH_CE_NAME}],
    )
    _p(f"  Created job queue: {BATCH_QUEUE}")


def _register_jd(batch, name: str, script: str,
                  task_role_arn: str, exec_role_arn: str,
                  image: str, extra_env: list | None = None,
                  vcpu: str = VCPU, memory: str = MEMORY,
                  timeout_s: int = 14400, retries: int = 3,
                  stream_prefix: str = "job"):
    """Register a single Fargate Batch job definition (idempotent — always adds a new revision)."""
    env = [
        {"name": "S3_BUCKET",         "value": BUCKET},
        {"name": "SECRETS_PREFIX",    "value": SECRETS_PREFIX},
        {"name": "AWS_DEFAULT_REGION","value": REGION},
    ]
    if extra_env:
        env.extend(extra_env)
    batch.register_job_definition(
        jobDefinitionName=name,
        type="container",
        platformCapabilities=["FARGATE"],
        containerProperties={
            "image":            image,
            "command":          ["python", script],
            "jobRoleArn":       task_role_arn,
            "executionRoleArn": exec_role_arn,
            "resourceRequirements": [
                {"type": "VCPU",   "value": vcpu},
                {"type": "MEMORY", "value": memory},
            ],
            "environment": env,
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group":         LOG_GROUP,
                    "awslogs-region":        REGION,
                    "awslogs-stream-prefix": stream_prefix,
                },
            },
            "networkConfiguration": {"assignPublicIp": "ENABLED"},
            "ephemeralStorage":     {"sizeInGiB": 30},
        },
        retryStrategy={"attempts": retries,
                       "evaluateOnExit": [
                           {"onExitCode": "130", "action": "RETRY"},  # Fargate Spot preempted
                           {"onExitCode": "0",   "action": "EXIT"},
                       ]},
        timeout={"attemptDurationSeconds": timeout_s},
    )
    _p(f"  Registered {name}")


def _ensure_job_definition(batch, task_role_arn: str, exec_role_arn: str,
                            ecr_uri: str, dry_run: bool):
    image = f"{ecr_uri}:latest"
    if dry_run:
        for name in ("osu-pipeline-job", "osu-enrich-job", "osu-mapbuild-job"):
            _p(f"  [DRY] would register job definition: {name}")
        return

    # ── Pipeline job (one per slice, many in parallel) ───────────────────────
    _register_jd(
        batch, name="osu-pipeline-job",
        script="aws/run_batch_job.py",
        task_role_arn=task_role_arn, exec_role_arn=exec_role_arn,
        image=image,
        extra_env=[
            {"name": "USE_VISION_API",   "value": "0"},
            {"name": "PIPELINE_WORKERS", "value": "2"},
        ],
        timeout_s=14400,   # 4h per slice
        retries=3,
        stream_prefix="slice",
    )

    # ── Enrichment job (single instance, runs after all slices done) ─────────
    # Needs more memory for in-memory CSV merge + RDS queries
    _register_jd(
        batch, name="osu-enrich-job",
        script="aws/run_enrich_job.py",
        task_role_arn=task_role_arn, exec_role_arn=exec_role_arn,
        image=image,
        extra_env=[
            {"name": "S3_OUTPUT_PREFIX", "value": "outputs/merged"},
        ],
        vcpu="2", memory="8192",
        timeout_s=7200,    # 2h for enrichment
        retries=2,
        stream_prefix="enrich",
    )

    # ── Map build job (single instance, pushes to GitHub Pages) ─────────────
    _register_jd(
        batch, name="osu-mapbuild-job",
        script="aws/run_mapbuild_job.py",
        task_role_arn=task_role_arn, exec_role_arn=exec_role_arn,
        image=image,
        extra_env=[
            {"name": "S3_INPUT_KEY",  "value": "outputs/merged/dot_coordinates.csv"},
        ],
        vcpu="1", memory="2048",
        timeout_s=1800,    # 30m for map build + git push
        retries=2,
        stream_prefix="mapbuild",
    )


# ── CloudWatch ────────────────────────────────────────────────────────────────

def _ensure_log_group(logs, dry_run: bool):
    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        _p(f"  Created log group: {LOG_GROUP}")
    except ClientError as e:
        if "ResourceAlreadyExistsException" in str(e):
            _p(f"  Log group exists: {LOG_GROUP}")
        else:
            _p(f"  WARNING: log group: {e}")


def _ensure_alarm(cw, sns_arn: str, dry_run: bool):
    if dry_run:
        _p("  [DRY] would create CloudWatch alarm")
        return
    try:
        cw.put_metric_alarm(
            AlarmName="osu-pipeline-job-failures",
            AlarmDescription="Alert when Batch job failure rate is high",
            MetricName="FailedJobCount",
            Namespace="AWS/Batch",
            Dimensions=[{"Name": "JobQueue", "Value": BATCH_QUEUE}],
            Period=300,
            EvaluationPeriods=2,
            Threshold=5,
            ComparisonOperator="GreaterThanThreshold",
            Statistic="Sum",
            AlarmActions=[sns_arn] if sns_arn else [],
        )
        _p("  CloudWatch alarm: osu-pipeline-job-failures")
    except Exception as e:
        _p(f"  WARNING: alarm setup: {e}")


# ── Account 1 bucket policy ───────────────────────────────────────────────────

def _patch_bucket_policy(s3_a1, task_role_arn: str, dry_run: bool):
    """Add cross-account statement to Account 1's bucket policy."""
    if s3_a1 is None:
        _p("  SKIP bucket policy (Account 1 profile not configured)")
        _p(f"  Manually add this statement to s3://{BUCKET} bucket policy:")
        _p(json.dumps({
            "Effect": "Allow",
            "Principal": {"AWS": task_role_arn},
            "Action": ["s3:GetObject","s3:PutObject","s3:DeleteObject",
                       "s3:ListBucket","s3:GetBucketLocation"],
            "Resource": [f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"],
        }, indent=4))
        return

    new_stmt = {
        "Sid":       "OsuBatchCrossAccount",
        "Effect":    "Allow",
        "Principal": {"AWS": task_role_arn},
        "Action":    ["s3:GetObject","s3:PutObject","s3:DeleteObject",
                      "s3:ListBucket","s3:GetBucketLocation"],
        "Resource":  [f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"],
    }
    try:
        existing = json.loads(
            s3_a1.get_bucket_policy(Bucket=BUCKET)["Policy"]
        )
    except ClientError:
        existing = {"Version": "2012-10-17", "Statement": []}

    # Remove any prior OsuBatchCrossAccount statement and re-add fresh
    existing["Statement"] = [
        s for s in existing["Statement"] if s.get("Sid") != "OsuBatchCrossAccount"
    ]
    existing["Statement"].append(new_stmt)

    if dry_run:
        _p(f"  [DRY] would update bucket policy on s3://{BUCKET}")
        return
    s3_a1.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(existing))
    _p(f"  Updated bucket policy: s3://{BUCKET} → allows {task_role_arn}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run",            action="store_true")
    ap.add_argument("--profile",            default=os.environ.get("AWS_PROFILE", "mano"),
                    help="Account 2 (compute) AWS profile")
    ap.add_argument("--profile-account1",   default=os.environ.get("AWS_PROFILE_ACCOUNT1", "akshay"),
                    help="Account 1 (data) AWS profile for bucket policy patch")
    ap.add_argument("--skip-bucket-policy", action="store_true")
    ap.add_argument("--alert-email",        default="",
                    help="Email address for failure alerts (creates SNS subscription)")
    args = ap.parse_args()

    _p(f"\n{'='*60}")
    _p(f"  OSU Pipeline — AWS Setup")
    _p(f"  Account 2 (compute): {ACCOUNT2_ID}  profile={args.profile}")
    _p(f"  Account 1 (data):    {ACCOUNT1_ID}  profile={args.profile_account1}")
    _p(f"  Region: {REGION}   Dry-run: {args.dry_run}")
    _p(f"{'='*60}\n")

    c = _clients(args.profile,
                 None if args.skip_bucket_policy else args.profile_account1)

    _p("── IAM roles ────────────────────────────────────────")
    task_role_arn = _ensure_role(
        c["iam"], TASK_ROLE_NAME,
        "ecs-tasks.amazonaws.com", _TASK_POLICY, args.dry_run,
    )
    exec_role_arn = _ensure_role(
        c["iam"], EXEC_ROLE_NAME,
        "ecs-tasks.amazonaws.com", _EXEC_POLICY, args.dry_run,
    )

    _p("\n── ECR ─────────────────────────────────────────────")
    ecr_uri = _ensure_ecr(c["ecr"], args.dry_run)

    _p("\n── Secrets Manager ─────────────────────────────────")
    _ensure_secret(c["sm"], "gcp-credentials",
                   "GCP service-account JSON for Cloud Vision API",
                   '{"type":"service_account"}', args.dry_run)
    _ensure_secret(c["sm"], "gemini-keys",
                   "GOOGLE_API_KEY — comma-separated for key rotation",
                   "REPLACE_WITH_GEMINI_KEY", args.dry_run)
    _ensure_secret(c["sm"], "rds-config",
                   "PostgreSQL PLSS connection config",
                   json.dumps({"host":"REPLACE","dbname":"Oklahomaplss",
                               "user":"LookUpMaster","password":"REPLACE","port":5432}),
                   args.dry_run)
    _ensure_secret(c["sm"], "github-token",
                   "GitHub PAT for map push (repo:write scope)",
                   "REPLACE_WITH_GITHUB_PAT", args.dry_run)

    _p("\n── CloudWatch ──────────────────────────────────────")
    _ensure_log_group(c["logs"], args.dry_run)

    sns_arn = ""
    if args.alert_email and not args.dry_run:
        try:
            r   = c["sns"].create_topic(Name=SNS_TOPIC_NAME)
            sns_arn = r["TopicArn"]
            c["sns"].subscribe(TopicArn=sns_arn, Protocol="email",
                               Endpoint=args.alert_email)
            _p(f"  SNS topic: {sns_arn}  (confirm subscription email sent to {args.alert_email})")
        except Exception as e:
            _p(f"  WARNING: SNS setup: {e}")

    _ensure_alarm(c["cw"], sns_arn, args.dry_run)

    _p("\n── AWS Batch ────────────────────────────────────────")
    _ensure_compute_env(c["batch"], exec_role_arn, c["ec2"], args.dry_run)
    _ensure_job_queue(c["batch"], args.dry_run)
    _ensure_job_definition(c["batch"], task_role_arn, exec_role_arn,
                           ecr_uri, args.dry_run)

    _p("\n── S3 bucket policy (cross-account) ────────────────")
    if not args.skip_bucket_policy:
        _patch_bucket_policy(c["s3_a1"], task_role_arn, args.dry_run)

    _p(f"\n{'='*60}")
    _p("  Setup complete.\n")
    _p("  Next steps:")
    _p("    1. Update secrets with real values:")
    _p(f"       aws secretsmanager put-secret-value --secret-id {SECRETS_PREFIX}rds-config --secret-string '...'")
    _p(f"       aws secretsmanager put-secret-value --secret-id {SECRETS_PREFIX}gemini-keys --secret-string 'key1,key2'")
    _p(f"       aws secretsmanager put-secret-value --secret-id {SECRETS_PREFIX}gcp-credentials --secret-string file://credentials/smiling-breaker-*.json")
    _p("    2. Build + push Docker image:")
    _p("       python aws/build_image.py")
    _p("    3. Generate S3 dataset index + submit jobs:")
    _p("       python aws/orchestrate.py")
    _p(f"{'='*60}\n")


if __name__ == "__main__":
    main()

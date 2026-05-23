"""
Create AWS CodeBuild project to build and push the fixed Docker image.

Steps:
1. Create IAM role for CodeBuild with ECR + CloudWatch + S3 permissions
2. Upload source code zip to S3
3. Create CodeBuild project with inline buildspec
4. Start the build
5. Print build URL for monitoring
"""

import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import boto3

REGION         = "us-east-1"
ACCOUNT_ID     = "225989338968"
ECR_REPO       = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/osu-pipeline"
NEW_IMAGE_TAG  = "v7-all-fixes"
PROJECT_ROOT   = Path(__file__).parent.parent   # D:\project_modular
SOURCE_BUCKET  = f"osu-pipeline-results"        # reuse existing bucket
SOURCE_KEY     = "codebuild/source.zip"
PROJECT_NAME   = "osu-pipeline-docker-build"
ROLE_NAME      = "osu-codebuild-role"

iam    = boto3.client("iam",          region_name=REGION)
cb     = boto3.client("codebuild",    region_name=REGION)
s3     = boto3.client("s3",           region_name=REGION)

# ---------------------------------------------------------------------------
# 1. Create IAM role for CodeBuild
# ---------------------------------------------------------------------------
def ensure_codebuild_role() -> str:
    try:
        r = iam.get_role(RoleName=ROLE_NAME)
        arn = r["Role"]["Arn"]
        print(f"  IAM role exists: {arn}")
        return arn
    except iam.exceptions.NoSuchEntityException:
        pass

    trust = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "codebuild.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    })
    resp = iam.create_role(
        RoleName=ROLE_NAME,
        AssumeRolePolicyDocument=trust,
        Description="CodeBuild role for OSU pipeline Docker image builds",
    )
    arn = resp["Role"]["Arn"]
    print(f"  Created IAM role: {arn}")

    # Attach policies
    policy_doc = json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            # CloudWatch Logs
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/aws/codebuild/*"
            },
            # ECR auth + push
            {
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": "*"
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                ],
                "Resource": f"arn:aws:ecr:{REGION}:{ACCOUNT_ID}:repository/osu-pipeline"
            },
            # S3 source artifact
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Resource": f"arn:aws:s3:::{SOURCE_BUCKET}/{SOURCE_KEY}"
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{SOURCE_BUCKET}"
            },
        ]
    })
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="osu-codebuild-policy",
        PolicyDocument=policy_doc,
    )
    print("  Attached inline policy")
    time.sleep(10)  # wait for IAM propagation
    return arn


# ---------------------------------------------------------------------------
# 2. Create source zip and upload to S3
# ---------------------------------------------------------------------------
# Files to exclude from the build context (mirrors .dockerignore)
EXCLUDE_DIRS  = {"__pycache__", ".git", ".claude", ".pytest_cache",
                 "project_outputs", "aws/results", "aws/build",
                 "pdfs", "tests", "well_dot_detector", "credentials"}
EXCLUDE_EXTS  = {".pyc", ".pyo", ".zip", ".pdb"}
EXCLUDE_FILES = {".env", ".env.local", "smiling-breaker-423712-h3-aff7ac746ad4.json"}


def _should_include(rel: Path) -> bool:
    parts = rel.parts
    for part in parts:
        if part in EXCLUDE_DIRS:
            return False
    if rel.suffix.lower() in EXCLUDE_EXTS:
        return False
    if rel.name in EXCLUDE_FILES:
        return False
    if any(s in rel.name for s in ["smiling-breaker", ".env"]):
        return False
    return True


def create_and_upload_source() -> None:
    print(f"  Building source zip...")
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # Add Dockerfile.v6-rebuild as "Dockerfile" so we don't need to specify
        dockerfile_path = PROJECT_ROOT / "Dockerfile.v6-rebuild"
        if dockerfile_path.exists():
            zf.write(str(dockerfile_path), "Dockerfile")
            count += 1

        # Add .dockerignore
        di = PROJECT_ROOT / ".dockerignore"
        if di.exists():
            zf.write(str(di), ".dockerignore")

        # Add requirements.txt
        req = PROJECT_ROOT / "requirements.txt"
        if req.exists():
            zf.write(str(req), "requirements.txt")
            count += 1

        # Add unet files
        for fname in ["unet_dot_detector.py", "unet_best.pth"]:
            p = PROJECT_ROOT / fname
            if p.exists():
                zf.write(str(p), fname)
                count += 1
            else:
                print(f"  WARNING: {fname} not found — build may fail!")

        # Add project/ directory (recursive)
        project_dir = PROJECT_ROOT / "project"
        for fpath in sorted(project_dir.rglob("*")):
            if not fpath.is_file():
                continue
            rel = fpath.relative_to(PROJECT_ROOT)
            if not _should_include(rel):
                continue
            zf.write(str(fpath), str(rel).replace("\\", "/"))
            count += 1

        # Add aws/run_batch_job.py
        rb = PROJECT_ROOT / "aws" / "run_batch_job.py"
        if rb.exists():
            zf.write(str(rb), "aws/run_batch_job.py")
            count += 1

    size_mb = buf.tell() / 1024**2
    print(f"  Zip: {count} files, {size_mb:.1f} MB")

    buf.seek(0)
    s3.put_object(Bucket=SOURCE_BUCKET, Key=SOURCE_KEY, Body=buf.read())
    print(f"  Uploaded to s3://{SOURCE_BUCKET}/{SOURCE_KEY}")


# ---------------------------------------------------------------------------
# 3. Create CodeBuild project
# ---------------------------------------------------------------------------
BUILDSPEC = {
    "version": "0.2",
    "phases": {
        "pre_build": {
            "commands": [
                "echo Logging in to Amazon ECR...",
                f"aws ecr get-login-password --region {REGION} | docker login --username AWS --password-stdin {ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com",
            ]
        },
        "build": {
            "commands": [
                "echo Build started on `date`",
                "echo Building Docker image...",
                f"docker build -f Dockerfile -t {ECR_REPO}:{NEW_IMAGE_TAG} .",
            ]
        },
        "post_build": {
            "commands": [
                "echo Build completed on `date`",
                f"docker push {ECR_REPO}:{NEW_IMAGE_TAG}",
                f"echo Pushed {ECR_REPO}:{NEW_IMAGE_TAG}",
            ]
        }
    }
}


def ensure_codebuild_project(role_arn: str) -> None:
    projects = cb.batch_get_projects(names=[PROJECT_NAME]).get("projects", [])
    if projects:
        print(f"  CodeBuild project exists: {PROJECT_NAME} — updating source")
        cb.update_project(
            name=PROJECT_NAME,
            source={
                "type": "S3",
                "location": f"{SOURCE_BUCKET}/{SOURCE_KEY}",
                "buildspec": json.dumps(BUILDSPEC),
            },
        )
        return

    resp = cb.create_project(
        name=PROJECT_NAME,
        description="Build osu-pipeline Docker image and push to ECR",
        source={
            "type": "S3",
            "location": f"{SOURCE_BUCKET}/{SOURCE_KEY}",
            "buildspec": json.dumps(BUILDSPEC),
        },
        artifacts={"type": "NO_ARTIFACTS"},
        environment={
            "type": "LINUX_CONTAINER",
            "image": "aws/codebuild/standard:7.0",
            "computeType": "BUILD_GENERAL1_MEDIUM",
            "privilegedMode": True,   # needed for Docker-in-Docker
        },
        serviceRole=role_arn,
        logsConfig={
            "cloudWatchLogs": {
                "status": "ENABLED",
                "groupName": "/aws/codebuild/osu-pipeline",
            }
        },
    )
    print(f"  Created CodeBuild project: {resp['project']['name']}")


# ---------------------------------------------------------------------------
# 4. Start the build
# ---------------------------------------------------------------------------
def start_build() -> str:
    resp = cb.start_build(projectName=PROJECT_NAME)
    build_id = resp["build"]["id"]
    print(f"  Build started: {build_id}")
    console_url = (
        f"https://{REGION}.console.aws.amazon.com/codesuite/codebuild/"
        f"{ACCOUNT_ID}/projects/{PROJECT_NAME}/build/{build_id.replace(':', '%3A')}/log"
    )
    print(f"  Console: {console_url}")
    return build_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=== Setting up CodeBuild for Docker image rebuild ===\n")

    print("1. Ensuring CodeBuild IAM role...")
    role_arn = ensure_codebuild_role()

    print("\n2. Creating and uploading source zip...")
    create_and_upload_source()

    print("\n3. Ensuring CodeBuild project...")
    ensure_codebuild_project(role_arn)

    print("\n4. Starting build...")
    build_id = start_build()

    print(f"\n=== Build submitted: {build_id} ===")
    print(f"Target image: {ECR_REPO}:{NEW_IMAGE_TAG}")
    print(f"\nMonitor with:")
    print(f"  python aws/monitor_codebuild.py {build_id}")
    print(f"\nThe build will take ~15-20 minutes. Once complete:")
    print(f"  1. Run: python aws/register_rev13.py  (update to use v6-fixed-2)")
    print(f"  2. Cancel current job: aws batch cancel-job --job-id 25e21ea1... --reason 'switching to fixed image'")
    print(f"  3. Re-submit: python aws/orchestrate_robust.py --slice-size 500 --workers 4")

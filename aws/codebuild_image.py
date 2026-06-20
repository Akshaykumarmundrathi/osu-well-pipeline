"""codebuild_image.py -- build + push the pipeline image via AWS CodeBuild.

No local Docker needed. Bundles the build context (incl. gitignored model files)
to S3, ensures a CodeBuild service role + project, starts a build, waits, reports.
Idempotent: re-running reuses the role/project.

Usage: python aws/codebuild_image.py --tag v16
"""
import argparse, io, json, os, time, zipfile
from pathlib import Path
import boto3

REGION = "us-east-1"
ACCOUNT = "225989338968"
ECR_REPO = "osu-pipeline"
ECR_REGISTRY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
ECR_URI = f"{ECR_REGISTRY}/{ECR_REPO}"
BUCKET = "osu-well-records-225989338968"
SRC_KEY = "codebuild/source.zip"
ROLE = "osu-codebuild-role"
PROJECT = "osu-pipeline-build"
REPO_ROOT = Path(__file__).resolve().parent.parent
# only what the Dockerfile COPYs (+ Dockerfile/buildspec)
INCLUDE = ["Dockerfile", "buildspec.yml", "requirements.txt",
           "unet_best.pth", "unet_dot_detector.py", "project", "aws"]
EXCLUDE_DIRS = {"__pycache__", ".git", "project_outputs", "project_outputs_local"}


def bundle_to_s3():
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for item in INCLUDE:
            p = REPO_ROOT / item
            if p.is_file():
                z.write(p, item); n += 1
            elif p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file() and not any(d in f.parts for d in EXCLUDE_DIRS):
                        z.write(f, str(f.relative_to(REPO_ROOT))); n += 1
    buf.seek(0)
    size = len(buf.getvalue())
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=BUCKET, Key=SRC_KEY, Body=buf.getvalue())
    print(f"bundled {n} files ({size/1e6:.1f} MB) -> s3://{BUCKET}/{SRC_KEY}")


def ensure_role():
    iam = boto3.client("iam")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"},
        "Action": "sts:AssumeRole"}]}
    try:
        iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust))
        print(f"created role {ROLE}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"role {ROLE} exists")
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
        {"Effect": "Allow", "Action": [
            "ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload",
            "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
            "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
            "Resource": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{ECR_REPO}"},
        {"Effect": "Allow", "Action": ["s3:GetObject"],
            "Resource": f"arn:aws:s3:::{BUCKET}/{SRC_KEY}"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream",
            "logs:PutLogEvents"], "Resource": "*"}]}
    iam.put_role_policy(RoleName=ROLE, PolicyName="osu-codebuild-inline",
                        PolicyDocument=json.dumps(policy))
    return f"arn:aws:iam::{ACCOUNT}:role/{ROLE}"


def ensure_project(role_arn, tag):
    cb = boto3.client("codebuild", region_name=REGION)
    env = {"type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0",
           "computeType": "BUILD_GENERAL1_MEDIUM", "privilegedMode": True,
           "environmentVariables": [
               {"name": "AWS_REGION", "value": REGION},
               {"name": "ECR_REGISTRY", "value": ECR_REGISTRY},
               {"name": "ECR_URI", "value": ECR_URI},
               {"name": "IMAGE_TAG", "value": tag}]}
    src = {"type": "S3", "location": f"{BUCKET}/{SRC_KEY}",
           "buildspec": "buildspec.yml"}
    art = {"type": "NO_ARTIFACTS"}
    try:
        cb.create_project(name=PROJECT, source=src, artifacts=art,
                          environment=env, serviceRole=role_arn,
                          timeoutInMinutes=30)
        print(f"created project {PROJECT}")
    except cb.exceptions.ResourceAlreadyExistsException:
        cb.update_project(name=PROJECT, source=src, artifacts=art,
                          environment=env, serviceRole=role_arn)
        print(f"updated project {PROJECT}")


def run_build():
    cb = boto3.client("codebuild", region_name=REGION)
    bid = cb.start_build(projectName=PROJECT)["build"]["id"]
    print(f"build started: {bid}\nwatching...", flush=True)
    while True:
        time.sleep(20)
        b = cb.batch_get_builds(ids=[bid])["builds"][0]
        st = b["buildStatus"]; phase = b.get("currentPhase", "")
        print(f"  {st} / {phase}", flush=True)
        if st != "IN_PROGRESS":
            print(f"FINAL: {st}")
            return st == "SUCCEEDED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v16")
    a = ap.parse_args()
    print("ECR repo:", ECR_URI)
    boto3.client("ecr", region_name=REGION)  # repo already created
    bundle_to_s3()
    role = ensure_role()
    time.sleep(8)  # role propagation
    ensure_project(role, a.tag)
    ok = run_build()
    print("IMAGE READY" if ok else "BUILD FAILED — check CodeBuild console logs")


if __name__ == "__main__":
    main()

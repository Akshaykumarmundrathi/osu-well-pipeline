"""
build_image.py  —  Build Docker image and push to ECR
======================================================

Builds the pipeline container image locally, tags it, pushes to ECR,
and registers a new AWS Batch job definition revision that points to
the new image digest.

Usage:
    python aws/build_image.py
    python aws/build_image.py --tag v2          # custom tag (default: latest)
    python aws/build_image.py --no-push         # build only, don't push
    python aws/build_image.py --no-job-def      # skip job definition update
    python aws/build_image.py --profile mano    # AWS profile for Account 2
    python aws/build_image.py --platform linux/amd64  # cross-build on Mac M1

Prerequisites:
    Docker Desktop running
    aws configure --profile mano  (Account 2 — where ECR + Batch live)
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3

REGION       = "us-east-1"
ACCOUNT2_ID  = "266087050585"
ECR_REPO     = "osu-well-pipeline"
ECR_REGISTRY = f"{ACCOUNT2_ID}.dkr.ecr.{REGION}.amazonaws.com"
ECR_URI      = f"{ECR_REGISTRY}/{ECR_REPO}"

# Job definitions to update (pipeline / enrich / mapbuild)
JOB_DEFS = {
    "pipeline": "osu-pipeline-job",
    "enrich":   "osu-enrich-job",
    "mapbuild": "osu-mapbuild-job",
}

# Which CMD each job definition overrides
JOB_CMDS = {
    "pipeline": ["aws/run_batch_job.py"],
    "enrich":   ["aws/run_enrich_job.py"],
    "mapbuild": ["aws/run_mapbuild_job.py"],
}

REPO_ROOT = Path(__file__).parent.parent


def _run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run(cmd, check=True, **kwargs)


def _p(msg): print(msg, flush=True)


def ecr_login(profile: str):
    """Authenticate Docker to ECR."""
    _p("\n── ECR Login ─────────────────────────────────────────────")
    sess = boto3.Session(profile_name=profile, region_name=REGION)
    ecr  = sess.client("ecr")
    token = ecr.get_authorization_token()
    import base64
    auth = token["authorizationData"][0]
    creds = base64.b64decode(auth["authorizationToken"]).decode()
    user, pwd = creds.split(":", 1)
    _run(["docker", "login",
          "--username", user,
          "--password-stdin",
          ECR_REGISTRY],
         input=pwd.encode())
    _p("ECR login OK")


def build_image(tag: str, platform: str) -> str:
    """
    Build the Docker image and return the full image URI.
    """
    _p(f"\n── Build ─────────────────────────────────────────────────")
    full_tag = f"{ECR_URI}:{tag}"
    cmd = [
        "docker", "build",
        "--platform", platform,
        "-t", full_tag,
        str(REPO_ROOT),
    ]
    _run(cmd)
    _p(f"Build complete: {full_tag}")
    return full_tag


def push_image(full_tag: str):
    _p(f"\n── Push ──────────────────────────────────────────────────")
    _run(["docker", "push", full_tag])
    _p("Push complete")


def get_image_digest(full_tag: str) -> str:
    """Return the image digest (sha256:...) after push."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", full_tag],
        capture_output=True, text=True, check=False,
    )
    digest_line = result.stdout.strip()
    if "@" in digest_line:
        return digest_line.split("@", 1)[1]
    # Fallback: use tag
    return full_tag


def update_job_definitions(profile: str, image_uri: str, digest: str,
                            which: list[str]):
    """
    Register new revisions of all job definitions pointing to the new image.
    Uses the digest URI (image@sha256:...) for immutable references.
    """
    _p(f"\n── Job Definitions ───────────────────────────────────────")
    sess  = boto3.Session(profile_name=profile, region_name=REGION)
    batch = sess.client("batch")

    # Use digest if available, otherwise tag
    if digest and digest.startswith("sha256:"):
        pinned = f"{ECR_URI}@{digest}"
    else:
        pinned = image_uri

    for key, job_def_name in JOB_DEFS.items():
        if key not in which:
            continue
        _p(f"  Updating {job_def_name} → {pinned[:60]}…")
        try:
            # Fetch current definition to copy all settings
            current = batch.describe_job_definitions(
                jobDefinitionName=job_def_name,
                status="ACTIVE",
            )
            defs = current.get("jobDefinitions", [])
            if not defs:
                _p(f"    WARNING: no active definition for {job_def_name} — skipping")
                continue

            # Use the most recent revision
            existing = sorted(defs, key=lambda d: d["revision"])[-1]
            container = existing["containerProperties"].copy()
            container["image"] = pinned

            # Override CMD for enrich/mapbuild jobs
            cmd_override = JOB_CMDS.get(key)
            if cmd_override:
                container["command"] = cmd_override

            resp = batch.register_job_definition(
                jobDefinitionName=job_def_name,
                type=existing["type"],
                containerProperties=container,
                platformCapabilities=existing.get("platformCapabilities", ["FARGATE"]),
                tags=existing.get("tags", {}),
                retryStrategy=existing.get("retryStrategy", {"attempts": 3}),
                timeout=existing.get("timeout", {}),
            )
            rev = resp["revision"]
            _p(f"    ✓ {job_def_name}:{rev}")
        except Exception as e:
            _p(f"    ERROR updating {job_def_name}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile",   default=os.environ.get("AWS_PROFILE", "mano"))
    ap.add_argument("--tag",       default="latest")
    ap.add_argument("--platform",  default="linux/amd64",
                    help="Docker build platform (use linux/amd64 even on ARM Macs)")
    ap.add_argument("--no-push",   action="store_true",
                    help="Build only — don't push to ECR")
    ap.add_argument("--no-job-def", action="store_true",
                    help="Skip job definition update after push")
    ap.add_argument("--jobs",      nargs="+",
                    choices=list(JOB_DEFS.keys()),
                    default=list(JOB_DEFS.keys()),
                    help="Which job definitions to update (default: all)")
    args = ap.parse_args()

    _p(f"OSU Pipeline — Image Builder")
    _p(f"  Registry:  {ECR_REGISTRY}")
    _p(f"  Repo:      {ECR_REPO}")
    _p(f"  Tag:       {args.tag}")
    _p(f"  Platform:  {args.platform}")
    _p(f"  Profile:   {args.profile}")

    # Add a datestamp tag alongside --tag
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    full_tag  = f"{ECR_URI}:{args.tag}"

    # Build
    full_tag = build_image(args.tag, args.platform)

    if args.no_push:
        _p("\nSkipping push (--no-push). Done.")
        return

    # Login + push
    ecr_login(args.profile)
    push_image(full_tag)

    # Also tag + push with datestamp for rollback capability
    dated_tag = f"{ECR_URI}:{date_tag}"
    _run(["docker", "tag", full_tag, dated_tag])
    push_image(dated_tag)
    _p(f"Datestamp tag pushed: {date_tag}")

    # Get digest
    digest = get_image_digest(full_tag)
    _p(f"Image digest: {digest or '(unavailable)'}")

    if args.no_job_def:
        _p("\nSkipping job definition update (--no-job-def). Done.")
        return

    # Update job definitions
    update_job_definitions(args.profile, full_tag, digest, args.jobs)

    _p(f"\n✓ Done. New image: {full_tag}")
    _p(f"  Submit jobs with:  python aws/orchestrate.py")


if __name__ == "__main__":
    main()

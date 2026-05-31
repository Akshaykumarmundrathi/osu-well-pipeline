"""
run_mapbuild_job.py  —  AWS Batch map-build container entrypoint
================================================================

Downloads dot_coordinates.csv from S3, runs build_map_data.py to
produce well_locations.json, then commits and pushes to GitHub Pages
using a Personal Access Token from Secrets Manager.

Environment variables (set by orchestrate.py job submission):
  S3_BUCKET           source+output bucket  (osu-well-records-225989338968)
  S3_INPUT_KEY        key of dot_coordinates.csv  (outputs/merged/dot_coordinates.csv)
  SECRETS_PREFIX      Secrets Manager prefix  (osu/)
  AWS_DEFAULT_REGION  us-east-1

Flow:
  1. Pull secrets → configure GITHUB_TOKEN env var
  2. Clone the GitHub repo using the PAT (sparse clone: docs/ only)
  3. Download dot_coordinates.csv from S3
  4. Run build_map_data.py → writes docs/data/well_locations.json into the clone
  5. git commit + push
  6. Exit 0
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ── Configuration ────────────────────────────────────────────────────────────
S3_BUCKET   = os.environ.get("S3_BUCKET",          "osu-well-records-225989338968")
S3_IN_KEY   = os.environ.get("S3_INPUT_KEY",       "outputs/merged/dot_coordinates.csv")
SECRETS_PFX = os.environ.get("SECRETS_PREFIX",     "osu/")
REGION      = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

WORK_DIR    = Path("/tmp/mapbuild")
REPO_DIR    = WORK_DIR / "repo"
OUTPUT_DIR  = WORK_DIR / "output"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("run_mapbuild_job")


# ── Secrets ──────────────────────────────────────────────────────────────────

def pull_secrets() -> str:
    """Pull secrets; return GitHub PAT string (may be empty)."""
    log.info("Pulling secrets (prefix=%s)…", SECRETS_PFX)
    sm = boto3.client("secretsmanager", region_name=REGION)

    def _get(name: str) -> str:
        try:
            return sm.get_secret_value(SecretId=name).get("SecretString", "") or ""
        except ClientError as e:
            log.warning("Secret %s not found: %s", name, e)
            return ""

    github_token = _get(f"{SECRETS_PFX}github-token").strip()
    if not github_token:
        log.error("GitHub PAT not found in Secrets Manager at %sgithub-token",
                  SECRETS_PFX)
    else:
        log.info("GitHub PAT loaded")

    return github_token


# ── Git helpers ───────────────────────────────────────────────────────────────

def _run(cmd: list, cwd=None, env=None, check=True) -> subprocess.CompletedProcess:
    """Run a subprocess; log command; raise on non-zero if check=True."""
    display = " ".join(str(c) for c in cmd)
    # Mask any token in display
    display = display.replace(os.environ.get("_GITHUB_TOKEN", ""), "***")
    log.info("  $ %s", display)
    return subprocess.run(cmd, cwd=cwd, env=env,
                          check=check, capture_output=False)


def clone_repo(github_token: str) -> Path:
    """
    Sparse-clone the pipeline repo (docs/ only — we only need to push GeoJSON).
    Uses HTTPS with embedded PAT for authentication.

    The repo URL is read from GITHUB_REPO_URL env var (set it in the job
    definition or Secrets Manager). Falls back to a placeholder if not set.
    """
    repo_url_base = os.environ.get(
        "GITHUB_REPO_URL",
        "github.com/YOUR_ORG/osu-well-pipeline.git",
    )
    # Embed token into URL: https://<token>@github.com/...
    authed_url = f"https://{github_token}@{repo_url_base}"

    REPO_DIR.parent.mkdir(parents=True, exist_ok=True)

    log.info("Sparse-cloning repo (docs/ only)…")
    env = {**os.environ, "_GITHUB_TOKEN": github_token}

    _run(["git", "clone",
          "--depth", "1",
          "--filter=blob:none",
          "--sparse",
          authed_url,
          str(REPO_DIR)], env=env)

    _run(["git", "sparse-checkout", "set", "docs"], cwd=REPO_DIR)
    _run(["git", "config", "user.email", "batch@osu-pipeline.internal"], cwd=REPO_DIR)
    _run(["git", "config", "user.name", "OSU Batch Pipeline"], cwd=REPO_DIR)

    log.info("Repo cloned → %s", REPO_DIR)
    return REPO_DIR


# ── Download CSV ──────────────────────────────────────────────────────────────

def download_csv(s3) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    local = OUTPUT_DIR / "dot_coordinates.csv"
    try:
        s3.download_file(S3_BUCKET, S3_IN_KEY, str(local))
        size = local.stat().st_size
        log.info("Downloaded s3://%s/%s → %s (%s bytes)", S3_BUCKET, S3_IN_KEY, local, f"{size:,}")
    except ClientError as e:
        log.error("Failed to download dot_coordinates.csv: %s", e)
        raise
    return local


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("OSU Well Pipeline — Map Build Job")
    log.info("  CSV:  s3://%s/%s", S3_BUCKET, S3_IN_KEY)
    log.info("=" * 60)

    # 1. Secrets
    github_token = pull_secrets()
    if not github_token:
        log.error("Cannot push to GitHub without a PAT. Exiting.")
        sys.exit(1)

    # 2. S3 client
    s3 = boto3.client("s3", region_name=REGION)

    # 3. Download dot_coordinates.csv
    csv_path = download_csv(s3)

    # 4. Clone repo (docs/ only)
    try:
        repo = clone_repo(github_token)
    except subprocess.CalledProcessError as e:
        log.error("Git clone failed: %s", e)
        sys.exit(1)

    # 5. Run build_map_data.py
    #    REPO_ROOT = cloned repo dir
    #    OUTPUT_ROOT = where dot_coordinates.csv lives
    script = Path(__file__).parent.parent / "project" / "build_map_data.py"
    env = {
        **os.environ,
        "REPO_ROOT":    str(repo),
        "OUTPUT_ROOT":  str(OUTPUT_DIR),
        "PYTHONPATH":   str(Path(__file__).parent.parent / "project"),
    }
    cmd = [
        sys.executable, str(script),
        "--output",   str(OUTPUT_DIR),
        "--repo",     str(repo),
        # Do NOT pass --push here — we handle git ourselves below
        # so we can use the embedded-token clone
    ]
    log.info("Running build_map_data.py…")
    t0     = time.monotonic()
    result = subprocess.run(cmd, check=False, env=env)
    elapsed = time.monotonic() - t0
    log.info("build_map_data.py finished in %.0fs, exit_code=%d",
             elapsed, result.returncode)

    if result.returncode != 0:
        log.error("Map build failed")
        sys.exit(result.returncode)

    # 6. Verify GeoJSON was written
    geojson = repo / "docs" / "data" / "well_locations.json"
    if not geojson.exists():
        log.error("well_locations.json was not written by build_map_data.py")
        sys.exit(1)

    size = geojson.stat().st_size
    log.info("GeoJSON written: %s (%s bytes)", geojson, f"{size:,}")

    # 7. git add + commit + push
    try:
        _run(["git", "add", "docs/data/well_locations.json"], cwd=repo)
        _run(["git", "add", "docs/index.html"], cwd=repo, check=False)  # patched by build_map
        ts    = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        count = 0
        try:
            import json as _json
            data = _json.loads(geojson.read_text())
            count = len(data.get("features", []))
        except Exception:
            pass

        commit_msg = f"chore: update well map [{count:,} wells] — {ts} [batch]"
        _run(["git", "commit", "-m", commit_msg], cwd=repo)
        _run(["git", "push"], cwd=repo)
        log.info("Pushed to GitHub Pages ✓  (%d wells)", count)
    except subprocess.CalledProcessError as e:
        log.error("git push failed: %s", e)
        sys.exit(1)

    log.info("Map build job complete.")


if __name__ == "__main__":
    main()

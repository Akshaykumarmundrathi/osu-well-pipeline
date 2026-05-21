#!/usr/bin/env bash
# =============================================================================
# build_push.sh — Build and push osu-pipeline Docker images to ECR
#
# Usage:
#   bash aws/build_push.sh            # build app only (normal code change)
#   bash aws/build_push.sh --base     # rebuild base too (requirements.txt changed)
#   bash aws/build_push.sh --all      # rebuild both base + app from scratch
#
# What it does:
#   1. Logs into ECR (token lasts 12 h)
#   2. Prunes dangling Docker layers to free disk before building
#   3. Rebuilds base image if --base/--all or if requirements.txt hash changed
#   4. Rebuilds app image (always fast — only COPY layers change)
#   5. Pushes both images to ECR
#   6. Verifies the push by describing the new ECR image
# =============================================================================

set -euo pipefail

REGION="us-east-1"
ACCOUNT="225989338968"
ECR="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
BASE_REPO="osu-pipeline-base"
APP_REPO="osu-pipeline"
HASH_FILE=".docker_base_hash"     # tracks requirements.txt hash for cache invalidation

BUILD_BASE=false
NO_CACHE_BASE=false

for arg in "$@"; do
  case "$arg" in
    --base) BUILD_BASE=true ;;
    --all)  BUILD_BASE=true; NO_CACHE_BASE=true ;;
  esac
done

echo "============================================================"
echo " OSU Pipeline — Docker Build & Push"
echo " Region  : $REGION"
echo " Account : $ACCOUNT"
echo " Base    : rebuild=$BUILD_BASE  no-cache=$NO_CACHE_BASE"
echo "============================================================"

# ------------------------------------------------------------------
# 1. ECR login
# ------------------------------------------------------------------
echo "[1/6] Logging into ECR ..."
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ECR"

# ------------------------------------------------------------------
# 2. Prune dangling images to free disk before building
# ------------------------------------------------------------------
echo "[2/6] Pruning dangling Docker layers ..."
docker image prune -f
docker builder prune -f --filter "until=24h" 2>/dev/null || true

# ------------------------------------------------------------------
# 3. Check if base needs rebuild (requirements.txt hash changed)
# ------------------------------------------------------------------
REQ_HASH=$(md5sum requirements.txt | awk '{print $1}')
STORED_HASH=$(cat "$HASH_FILE" 2>/dev/null || echo "")

if [ "$REQ_HASH" != "$STORED_HASH" ]; then
  echo "[3/6] requirements.txt changed ($STORED_HASH -> $REQ_HASH) — rebuilding base"
  BUILD_BASE=true
fi

# ------------------------------------------------------------------
# 4. Build base image (heavy deps — only when needed)
# ------------------------------------------------------------------
if [ "$BUILD_BASE" = "true" ]; then
  echo "[3/6] Building base image (apt + pip + torch) ..."
  NO_CACHE_FLAG=""
  if [ "$NO_CACHE_BASE" = "true" ]; then
    NO_CACHE_FLAG="--no-cache"
  fi

  docker build $NO_CACHE_FLAG \
    -f Dockerfile.base \
    -t "${BASE_REPO}:latest" \
    -t "${ECR}/${BASE_REPO}:latest" \
    .

  echo "[3b/6] Pushing base image to ECR ..."
  docker push "${ECR}/${BASE_REPO}:latest"

  # Save hash so we don't rebuild unnecessarily next time
  echo "$REQ_HASH" > "$HASH_FILE"
  echo "       Base image pushed ✓"
else
  echo "[3/6] Base image up-to-date (requirements.txt unchanged) — pulling from ECR ..."
  # Pull latest base so Docker has it locally for the FROM directive
  docker pull "${ECR}/${BASE_REPO}:latest" 2>/dev/null \
    || echo "       (Could not pull — using local cache)"
fi

# ------------------------------------------------------------------
# 5. Build app image (code only — always fast)
# ------------------------------------------------------------------
echo "[4/6] Building app image (code-only layer) ..."
docker build \
  --cache-from "${ECR}/${BASE_REPO}:latest" \
  -f Dockerfile \
  -t "${APP_REPO}:latest" \
  -t "${ECR}/${APP_REPO}:latest" \
  .
echo "       App image built ✓"

# ------------------------------------------------------------------
# 6. Push app image
# ------------------------------------------------------------------
echo "[5/6] Pushing app image to ECR ..."
docker push "${ECR}/${APP_REPO}:latest"
echo "       App image pushed ✓"

# ------------------------------------------------------------------
# 7. Verify
# ------------------------------------------------------------------
echo "[6/6] Verifying ECR image ..."
aws ecr describe-images \
  --repository-name "$APP_REPO" \
  --region "$REGION" \
  --query "sort_by(imageDetails,&imagePushedAt)[-1].{pushed:imagePushedAt,digest:imageDigest,sizeMB:to_string(imageSizeInBytes)}" \
  --output json

echo ""
echo "============================================================"
echo " Done! Image is live in ECR."
echo " Submit a new Batch job to use the updated image."
echo "============================================================"

#!/bin/bash
# Phase 2 Implementation - Docker rebuild + Job Definition v6 registration
# This script handles the core implementation fixes

set -e

echo "========================================="
echo "PHASE 2: DOCKER REBUILD & JOB DEFINITION"
echo "========================================="
echo ""

ACCOUNT_ID="225989338968"
REGION="us-east-1"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

# Step 1: Verify Docker build completed
echo "[1/5] Checking Docker build..."
if docker image ls | grep -q "osu-pipeline.*v6-fixed"; then
  echo "✓ v6-fixed image found locally"
  IMAGE_SIZE=$(docker images osu-pipeline:v6-fixed --format "{{.Size}}")
  echo "    Image size: $IMAGE_SIZE"
else
  echo "✗ v6-fixed image not found. Is Docker build still running?"
  exit 1
fi

# Step 2: Tag image for ECR
echo "[2/5] Tagging image for ECR..."
docker tag osu-pipeline:v6-fixed "${ECR_URI}/osu-pipeline:v6-fixed"
docker tag osu-pipeline:v6-fixed "${ECR_URI}/osu-pipeline:latest"
echo "✓ Tagged for ECR"

# Step 3: Push to ECR
echo "[3/5] Pushing to ECR (this may take 2-5 minutes)..."
docker push "${ECR_URI}/osu-pipeline:v6-fixed"
docker push "${ECR_URI}/osu-pipeline:latest"
echo "✓ Pushed to ECR"

# Step 4: Verify image in ECR
echo "[4/5] Verifying image in ECR..."
aws ecr describe-images \
  --repository-name osu-pipeline \
  --region ${REGION} \
  --query 'imageDetails[?contains(imageTags, `v6-fixed`)].imageSizeInBytes' \
  --output text | while read size; do
    if [ ! -z "$size" ]; then
      SIZE_MB=$((size / 1024 / 1024))
      echo "✓ Image in ECR: ${SIZE_MB}MB"
    fi
  done

# Step 5: Register Job Definition v6
echo "[5/5] Registering Job Definition v6..."
aws batch register-job-definition \
  --job-definition-name osu-pipeline-job \
  --revision 6 \
  --type container \
  --cli-input-json file://jobdef-v6.json \
  --region ${REGION} 2>&1 | grep -E "jobDefinition|Error"

echo ""
echo "========================================="
echo "PHASE 2 COMPLETE"
echo "========================================="
echo ""
echo "Next steps:"
echo "1. Run local test: export SLICE_SIZE=100 && python aws/run_batch_job.py --test --slice-index 0"
echo "2. Submit AWS test job with v6 definition"
echo "3. Monitor CloudWatch logs"
echo "4. Resubmit all 391 slices"
echo ""
echo "To resubmit all slices after test succeeds:"
echo "  for i in {0..390}; do"
echo "    aws batch submit-job --job-name \"osu-v6-\${i}\" \\"
echo "      --job-queue arn:aws:batch:${REGION}:${ACCOUNT_ID}:job-queue/osu-pipeline-queue \\"
echo "      --job-definition osu-pipeline-job:6 \\"
echo "      --container-overrides environment=[{name=JOB_INDEX,value=\${i}}] \\"
echo "      --region ${REGION}"
echo "  done"

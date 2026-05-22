#!/bin/bash
set -e

REGION="us-east-1"
ACCOUNT="225989338968"

echo "=========================================="
echo "AWS BATCH CLEANUP & REBUILD"
echo "=========================================="
echo ""

# STEP 1: CANCEL ALL STUCK JOBS
echo "[STEP 1] Cancelling all stuck jobs..."
echo "=========================================="

RUNNABLE=$(aws batch list-jobs --job-queue osu-pipeline-queue --filters "name=job-status,values=RUNNABLE" --region $REGION --max-results 1000 --query 'jobSummaryList[*].jobId' --output text 2>/dev/null || echo "")

if [ ! -z "$RUNNABLE" ]; then
    for JOB_ID in $RUNNABLE; do
        echo "  Terminating RUNNABLE job: $JOB_ID"
        aws batch terminate-job --job-id "$JOB_ID" --reason "Stuck - rebuilding" --region $REGION 2>/dev/null || true
    done
else
    echo "  No RUNNABLE jobs to cancel"
fi

echo "  Waiting for jobs to terminate..."
sleep 5

# STEP 2: DELETE OLD JOB DEFINITIONS
echo ""
echo "[STEP 2] Cleaning up old job definitions..."
echo "=========================================="

# Keep only latest 2 revisions, deregister others
DEFS=$(aws batch describe-job-definitions --job-definition-name osu-pipeline-job --region $REGION --query 'jobDefinitions[*].[revision,jobDefinitionArn]' --output text 2>/dev/null || echo "")

if [ ! -z "$DEFS" ]; then
    REVISIONS=($(echo "$DEFS" | awk '{print $1}' | sort -rn))
    KEEP=2
    for (( i=$KEEP; i<${#REVISIONS[@]}; i++ )); do
        REV=${REVISIONS[$i]}
        echo "  Deregistering revision $REV"
        aws batch deregister-job-definition --job-definition "osu-pipeline-job:$REV" --region $REGION 2>/dev/null || true
    done
else
    echo "  No job definitions to clean"
fi

# STEP 3: DELETE BROKEN BASE IMAGE (if exists)
echo ""
echo "[STEP 3] Checking ECR for broken images..."
echo "=========================================="

IMAGES=$(aws ecr describe-images --repository-name osu-pipeline-base --region $REGION --query 'imageDetails[*].imageTags' --output text 2>/dev/null || echo "")

if [ ! -z "$IMAGES" ]; then
    echo "  WARNING: osu-pipeline-base has images but isn't used"
    echo "  Images: $IMAGES"
    echo "  (Will keep for now - decide later if needed)"
else
    echo "  osu-pipeline-base is empty (good)"
fi

# STEP 4: VERIFY KEY INFRASTRUCTURE
echo ""
echo "[STEP 4] Verifying infrastructure..."
echo "=========================================="

# Check Compute Environment
CE_STATE=$(aws batch describe-compute-environments --compute-environments osu-pipeline-ce --region $REGION --query 'computeEnvironments[0].state' --output text 2>/dev/null || echo "MISSING")
echo "  Compute Environment (osu-pipeline-ce): $CE_STATE"

# Check Job Queue
JQ_STATE=$(aws batch describe-job-queues --job-queues osu-pipeline-queue --region $REGION --query 'jobQueues[0].state' --output text 2>/dev/null || echo "MISSING")
echo "  Job Queue (osu-pipeline-queue): $JQ_STATE"

# Check S3 Buckets
echo "  S3 Buckets:"
echo "    - INPUT: osu-well-records-225989338968"
aws s3 ls s3://osu-well-records-225989338968 --max-items 1 --region $REGION 2>/dev/null >/dev/null && echo "      ✓ EXISTS" || echo "      ✗ MISSING"

echo "    - OUTPUT: osu-pipeline-results"
aws s3 ls s3://osu-pipeline-results --max-items 1 --region $REGION 2>/dev/null >/dev/null && echo "      ✓ EXISTS" || echo "      ✗ MISSING"

# Check Secrets
echo "  Secrets Manager:"
SECRETS=$(aws secretsmanager list-secrets --region $REGION --query "SecretList[?contains(Name, 'osu')].Name" --output text 2>/dev/null || echo "")
if [ ! -z "$SECRETS" ]; then
    echo "    ✓ Secrets found: $SECRETS"
else
    echo "    ✗ No OSU secrets found"
fi

# STEP 5: CREATE NEW CLEAN JOB DEFINITION v11
echo ""
echo "[STEP 5] Creating new Job Definition v11..."
echo "=========================================="

cat > /tmp/jobdef-v11-clean.json << 'JOBDEF'
{
  "jobDefinitionName": "osu-pipeline-job",
  "type": "container",
  "containerProperties": {
    "image": "225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline:v6-fixed",
    "resourceRequirements": [
      {"type": "VCPU", "value": "2"},
      {"type": "MEMORY", "value": "4096"}
    ],
    "jobRoleArn": "arn:aws:iam::225989338968:role/osu-batch-task-role",
    "executionRoleArn": "arn:aws:iam::225989338968:role/osu-batch-execution-role",
    "environment": [
      {"name": "SLICE_SIZE", "value": "500"},
      {"name": "MAX_WORKERS", "value": "4"},
      {"name": "AWS_DEFAULT_REGION", "value": "us-east-1"},
      {"name": "PYTHONUNBUFFERED", "value": "1"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/aws/batch/osu-pipeline",
        "awslogs-region": "us-east-1",
        "awslogs-stream-prefix": "job"
      }
    }
  },
  "platformCapabilities": ["FARGATE"],
  "timeout": {
    "attemptDurationSeconds": 28800
  }
}
JOBDEF

echo "  Registering osu-pipeline-job:v11..."
NEW_REV=$(aws batch register-job-definition --cli-input-json file:///tmp/jobdef-v11-clean.json --region $REGION --query 'revision' --output text 2>/dev/null || echo "ERROR")

if [ "$NEW_REV" != "ERROR" ]; then
    echo "  ✓ New revision: $NEW_REV"
else
    echo "  ✗ Failed to register job definition"
    exit 1
fi

# STEP 6: SUBMIT TEST JOB
echo ""
echo "[STEP 6] Submitting test job (slice 1, 500 PDFs)..."
echo "=========================================="

TEST_JOB=$(aws batch submit-job \
  --job-name "osu-rebuild-test-001" \
  --job-queue "osu-pipeline-queue" \
  --job-definition "osu-pipeline-job:$NEW_REV" \
  --container-overrides '{
    "environment": [
      {"name": "SLICE_NUM", "value": "1"},
      {"name": "SLICE_SIZE", "value": "500"},
      {"name": "INPUT_BUCKET", "value": "osu-well-records-225989338968"},
      {"name": "OUTPUT_BUCKET", "value": "osu-pipeline-results"},
      {"name": "INDEX_KEY", "value": "collections_index.json"},
      {"name": "GOOGLE_CREDS_SECRET_ID", "value": "osu-pipeline/gemini-api-key"},
      {"name": "RDS_CREDS_SECRET_ID", "value": "osu-pipeline/rds"}
    ]
  }' \
  --region $REGION \
  --query 'jobId' \
  --output text 2>/dev/null || echo "ERROR")

if [ "$TEST_JOB" != "ERROR" ]; then
    echo "  ✓ Test job submitted: $TEST_JOB"
else
    echo "  ✗ Failed to submit test job"
    exit 1
fi

# STEP 7: MONITOR TEST JOB
echo ""
echo "[STEP 7] Monitoring test job (60 seconds)..."
echo "=========================================="

for i in {1..12}; do
    STATUS=$(aws batch describe-jobs --jobs "$TEST_JOB" --region $REGION --query 'jobs[0].status' --output text 2>/dev/null || echo "UNKNOWN")
    echo "  [$i/12] Status: $STATUS"

    if [ "$STATUS" = "RUNNING" ]; then
        echo "  ✓ JOB STARTED!"
        echo "    Expected completion: ~4 hours"
        echo "    Monitor with: aws batch describe-jobs --jobs $TEST_JOB --region $REGION"
        break
    fi

    if [ "$STATUS" = "FAILED" ]; then
        echo "  ✗ JOB FAILED!"
        exit 1
    fi

    sleep 5
done

echo ""
echo "=========================================="
echo "REBUILD COMPLETE"
echo "=========================================="
echo ""
echo "Summary:"
echo "  - Cancelled stuck jobs"
echo "  - Cleaned up old definitions"
echo "  - Created new Job Definition v$NEW_REV"
echo "  - Submitted test job: $TEST_JOB"
echo "  - Current status: $STATUS"
echo ""
echo "Next: Monitor job and once SUCCEEDED, run full 391-slice resubmission"
echo ""

#!/bin/bash
set -e

REGION="us-east-1"
ACCOUNT="225989338968"

echo "=========================================="
echo "AWS EC2-BASED BATCH - BUILD & RUN"
echo "=========================================="
echo ""

# STEP 1: CANCEL ALL STUCK JOBS
echo "[1] Terminating stuck jobs..."
aws batch list-jobs --job-queue osu-pipeline-queue --filters "name=job-status,values=RUNNABLE" --region $REGION --max-results 1000 --query 'jobSummaryList[*].jobId' --output text 2>/dev/null | tr ' ' '\n' | while read JOB; do
    [ ! -z "$JOB" ] && aws batch terminate-job --job-id "$JOB" --reason "Rebuilding to EC2" --region $REGION 2>/dev/null || true
done
sleep 3

# STEP 2: DELETE OLD COMPUTE ENVIRONMENT (if FARGATE based)
echo "[2] Checking compute environment..."
CE_TYPE=$(aws batch describe-compute-environments --compute-environments osu-pipeline-ce --region $REGION --query 'computeEnvironments[0].type' --output text 2>/dev/null || echo "NONE")

if [ "$CE_TYPE" = "FARGATE" ]; then
    echo "  Current: FARGATE - will replace with EC2"
    # Update to different name to avoid conflicts
    echo "  (Will create new EC2 environment)"
else
    echo "  Current: $CE_TYPE"
fi

# STEP 3: CREATE EC2 COMPUTE ENVIRONMENT
echo "[3] Creating EC2 compute environment..."

cat > /tmp/ce-ec2.json << 'CEDEF'
{
  "computeEnvironmentName": "osu-pipeline-ec2",
  "type": "MANAGED",
  "state": "ENABLED",
  "computeResources": {
    "type": "EC2",
    "minvCpus": 0,
    "maxvCpus": 256,
    "desiredvCpus": 60,
    "instanceTypes": ["optimal"],
    "subnets": [
      "subnet-0b9994bd4b4600300",
      "subnet-019a923bb3896a564",
      "subnet-04597552f8bb67e1e"
    ],
    "securityGroupIds": ["sg-03085b97dbd108e7e"],
    "instanceRole": "arn:aws:iam::225989338968:instance-profile/ecsInstanceRole"
  },
  "serviceRole": "arn:aws:iam::225989338968:role/AWSBatchServiceRole"
}
CEDEF

aws batch create-compute-environment --cli-input-json file:///tmp/ce-ec2.json --region $REGION 2>/dev/null && \
    echo "  ✓ Created osu-pipeline-ec2 (EC2-based)" || \
    echo "  Note: Environment may already exist"

sleep 2

# STEP 4: CREATE/UPDATE JOB QUEUE FOR EC2
echo "[4] Creating job queue for EC2..."

# Try to create new queue
aws batch create-job-queue \
  --job-queue-name osu-pipeline-queue-ec2 \
  --state ENABLED \
  --priority 1 \
  --compute-environment-order '{"order": 1, "computeEnvironment": "osu-pipeline-ec2"}' \
  --region $REGION 2>/dev/null && \
    echo "  ✓ Created new queue: osu-pipeline-queue-ec2" || \
    echo "  Note: Queue may already exist"

# STEP 5: CREATE JOB DEFINITION FOR EC2
echo "[5] Creating job definition for EC2..."

cat > /tmp/jobdef-ec2.json << 'JOBDEF'
{
  "jobDefinitionName": "osu-pipeline-ec2",
  "type": "container",
  "containerProperties": {
    "image": "225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline:v6-fixed",
    "vcpus": 2,
    "memory": 3000,
    "jobRoleArn": "arn:aws:iam::225989338968:role/osu-batch-task-role",
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
        "awslogs-stream-prefix": "job-ec2"
      }
    }
  },
  "timeout": {
    "attemptDurationSeconds": 28800
  }
}
JOBDEF

REV=$(aws batch register-job-definition --cli-input-json file:///tmp/jobdef-ec2.json --region $REGION --query 'revision' --output text 2>/dev/null || echo "ERROR")

if [ "$REV" != "ERROR" ]; then
    echo "  ✓ Created osu-pipeline-ec2:$REV"
else
    echo "  ✗ Failed"
    exit 1
fi

# STEP 6: SUBMIT ALL 391 SLICES
echo ""
echo "[6] SUBMITTING ALL 391 SLICES TO EC2 QUEUE..."
echo "=========================================="

QUEUE="osu-pipeline-queue-ec2"
JOBDEF="osu-pipeline-ec2:$REV"

SUBMITTED=0
FAILED=0

for SLICE in {1..391}; do
    JOB=$(aws batch submit-job \
      --job-name "osu-slice-$SLICE" \
      --job-queue "$QUEUE" \
      --job-definition "$JOBDEF" \
      --container-overrides "{
        \"environment\": [
          {\"name\": \"SLICE_NUM\", \"value\": \"$SLICE\"},
          {\"name\": \"SLICE_SIZE\", \"value\": \"500\"},
          {\"name\": \"INPUT_BUCKET\", \"value\": \"osu-well-records-225989338968\"},
          {\"name\": \"OUTPUT_BUCKET\", \"value\": \"osu-pipeline-results\"},
          {\"name\": \"INDEX_KEY\", \"value\": \"collections_index.json\"},
          {\"name\": \"GOOGLE_CREDS_SECRET_ID\", \"value\": \"osu-pipeline/gemini-api-key\"},
          {\"name\": \"RDS_CREDS_SECRET_ID\", \"value\": \"osu-pipeline/rds\"}
        ]
      }" \
      --region $REGION \
      --query 'jobId' \
      --output text 2>/dev/null || echo "FAILED")

    if [ "$JOB" != "FAILED" ] && [ ! -z "$JOB" ]; then
        SUBMITTED=$((SUBMITTED + 1))
    else
        FAILED=$((FAILED + 1))
    fi

    if [ $((SUBMITTED + FAILED)) -eq 50 ] || [ $((SUBMITTED + FAILED)) -eq 100 ] || [ $((SUBMITTED + FAILED)) -eq 200 ] || [ $((SUBMITTED + FAILED)) -eq 391 ]; then
        PERCENT=$((($SUBMITTED * 100) / 391))
        echo "  Progress: $SUBMITTED submitted, $FAILED failed [$PERCENT%]"
    fi

    # Rate limiting
    if [ $((SLICE % 10)) -eq 0 ]; then
        sleep 0.5
    fi
done

echo ""
echo "=========================================="
echo "SUBMISSION COMPLETE"
echo "=========================================="
echo "  Total submitted: $SUBMITTED / 391"
echo "  Failed: $FAILED"
echo ""
echo "Expected processing time:"
echo "  - With 60 concurrent EC2 instances"
echo "  - ~4 hours per slice average"
echo "  - Total: ~26-30 hours to completion"
echo ""
echo "Monitor with:"
echo "  aws batch list-jobs --job-queue osu-pipeline-queue-ec2 --filters name=job-status,values=RUNNING,SUCCEEDED,FAILED --region $REGION"
echo ""


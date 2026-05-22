#!/bin/bash
set -e

echo "=========================================="
echo "OSU PIPELINE - FULL 391 SLICE RESUBMISSION"
echo "=========================================="
echo ""

JOB_DEF="osu-pipeline-job:10"
QUEUE="osu-pipeline-queue"
REGION="us-east-1"
TOTAL_SLICES=391

echo "Configuration:"
echo "  Job Definition: $JOB_DEF (v6-fixed, FARGATE, SLICE_SIZE=500, 8h timeout)"
echo "  Queue: $QUEUE"
echo "  Total slices to submit: $TOTAL_SLICES"
echo ""

echo "Preparing job submission batch..."
SUBMITTED=0
FAILED=0

for SLICE_NUM in $(seq 1 $TOTAL_SLICES); do
  JOB_ID=$(aws batch submit-job \
    --job-name "osu-slice-$SLICE_NUM" \
    --job-queue "$QUEUE" \
    --job-definition "$JOB_DEF" \
    --container-overrides "{
      \"environment\": [
        {\"name\": \"SLICE_NUM\", \"value\": \"$SLICE_NUM\"},
        {\"name\": \"SLICE_SIZE\", \"value\": \"500\"},
        {\"name\": \"INPUT_BUCKET\", \"value\": \"osu-well-records-225989338968\"},
        {\"name\": \"OUTPUT_BUCKET\", \"value\": \"osu-pipeline-results\"},
        {\"name\": \"INDEX_KEY\", \"value\": \"collections_index.json\"},
        {\"name\": \"GOOGLE_CREDS_SECRET_ID\", \"value\": \"osu-pipeline/gemini-api-key\"},
        {\"name\": \"RDS_CREDS_SECRET_ID\", \"value\": \"osu-pipeline/rds\"}
      ]
    }" \
    --region "$REGION" \
    --query 'jobId' \
    --output text 2>/dev/null)
  
  if [ ! -z "$JOB_ID" ]; then
    SUBMITTED=$((SUBMITTED + 1))
    if (( SUBMITTED % 50 == 0 )); then
      echo "  [$SUBMITTED/$TOTAL_SLICES] Submitted slice $SLICE_NUM (Job: ${JOB_ID:0:8}...)"
    fi
  else
    FAILED=$((FAILED + 1))
    echo "  ❌ FAILED to submit slice $SLICE_NUM"
  fi
  
  # Rate limit: AWS allows ~100 submit operations per second, be conservative
  if (( SLICE_NUM % 10 == 0 )); then
    sleep 0.5
  fi
done

echo ""
echo "=========================================="
echo "SUBMISSION COMPLETE"
echo "=========================================="
echo "Total submitted: $SUBMITTED/$TOTAL_SLICES"
echo "Failed: $FAILED"
echo ""
echo "Expected processing time:"
echo "  - Slice processing: ~4 hours per slice"
echo "  - With 30 concurrent jobs: ~26-30 hours total"
echo "  - Expected completion: $(date -u -d '+30 hours' '+%Y-%m-%d %H:%M UTC')"
echo ""
echo "Monitor progress:"
echo "  aws batch describe-job-definitions --job-definition-name osu-pipeline-job --region us-east-1 --query 'jobDefinitions[0]'  # Check definition"
echo "  aws batch list-jobs --job-queue osu-pipeline-queue --filters name=job-status,values=RUNNING,SUCCEEDED,FAILED --region us-east-1  # Check queue"
echo "  aws s3 ls s3://osu-pipeline-results/results/ --recursive | grep -c job_status.json  # Count completed slices"
echo ""

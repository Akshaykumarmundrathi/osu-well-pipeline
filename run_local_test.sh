#!/bin/bash
# Local test of v6 job config with small slice (100 PDFs)

export SLICE_SIZE=100
export INPUT_BUCKET="osu-well-records-225989338968"
export OUTPUT_BUCKET="osu-well-records-225989338968"
export INDEX_KEY="dataset_index.csv"
export JOB_INDEX=0
export AWS_REGION="us-east-1"
export GOOGLE_CREDS_SECRET_ID="osu-pipeline/credentials"
export RDS_CREDS_SECRET_ID="osu-pipeline/rds"

echo "=== Local Test: v6 Config with SLICE_SIZE=100 ==="
echo "Environment:"
echo "  SLICE_SIZE=$SLICE_SIZE"
echo "  JOB_INDEX=$JOB_INDEX"
echo "  Buckets: $INPUT_BUCKET → $OUTPUT_BUCKET"
echo ""

cd project
python ../aws/run_batch_job.py 2>&1 | head -50

echo ""
echo "✓ Local test completed (check output above)"

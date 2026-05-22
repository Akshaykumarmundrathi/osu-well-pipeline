# IMPLEMENTATION FIXES FOR CATASTROPHIC FAILURES

## FIX 1: Reduce Slice Size to Prevent Job Timeouts

### Problem
- Current: SLICE_SIZE=1500 PDFs/job
- Timeout: 4 hours
- Expected duration: 1500 × 30sec = 45,000 sec = 12.5 hours
- Result: ~50% jobs timeout and fail

### Solution: SLICE_SIZE=500 PDFs/job
```
New duration: 500 × 30sec = 15,000 sec = 4.2 hours (fits timeout)
Trade-off: 3× more jobs (1,173 total vs 391)
```

### Implementation Steps

#### Step 1A: Update bulk_submit.py

Change line in `bulk_submit.py`:
```python
SLICE_SIZE = 500  # Was 1500 — reduced to prevent timeout
```

#### Step 1B: Resubmit Failed Jobs

```bash
# For each failed slice, resubmit with smaller chunks
for slice_num in {0..390}; do
  aws batch submit-job \
    --job-name "osu-rev5-fixed-${slice_num}" \
    --job-queue arn:aws:batch:us-east-1:225989338968:job-queue/osu-pipeline-queue \
    --job-definition osu-pipeline-job:5 \
    --array-properties size=1 \
    --container-overrides "environment=[{name=JOB_INDEX,value=${slice_num}}]" \
    --region us-east-1
done
```

#### Step 1C: Monitor Success Rate

Track completion in CloudWatch:
```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Batch \
  --metric-name RunningJobCount \
  --dimensions Name=JobQueue,Value=osu-pipeline-queue \
  --start-time $(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 3600 \
  --statistics Average
```

---

## FIX 2: Secure Credentials in AWS Secrets Manager

### Problem  
- `GOOGLE_API_KEY` exposed in plaintext `.env`
- `RDS_PASSWORD` exposed in plaintext `.env`

### Solution: Move to AWS Secrets Manager

#### Step 2A: Create Secrets in AWS

```bash
# Create Gemini API Key secret
aws secretsmanager create-secret \
  --name osu-pipeline/gemini-api-key \
  --description "Google Gemini API Key for OSU pipeline" \
  --secret-string '{"api_key":"AIzaSyDc2kUTgbpg****REDACTED****"}' \
  --region us-east-1

# Create RDS credentials secret  
aws secretsmanager create-secret \
  --name osu-pipeline/rds \
  --description "PostgreSQL credentials for PLSS lookup" \
  --secret-string '{
    "host": "oklahomagridlatlongdb.cz62c0sysryk.us-east-1.rds.amazonaws.com",
    "port": 5432,
    "username": "LookUpMaster",
    "password": "ROTATE_PASSWORD_REQUIRED",
    "dbname": "Oklahomaplss"
  }' \
  --region us-east-1

# Store GCP service account in Secrets Manager (if new key created)
aws secretsmanager create-secret \
  --name osu-pipeline/gcp-service-account \
  --description "GCP service account for Vision API" \
  --secret-string file://path/to/new-key.json \
  --region us-east-1
```

#### Step 2B: Update IAM Role to Allow Secrets Access

```bash
# Add to batch job role policy
aws iam put-role-policy \
  --role-name OSUPipelineBatchTaskRole \
  --policy-name SecretsManagerAccess \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": [
        "arn:aws:secretsmanager:us-east-1:225989338968:secret:osu-pipeline/*"
      ]
    }]
  }'
```

#### Step 2C: Update Job Entry Point to Fetch Secrets

In `run_batch_job.py`, add at startup:

```python
import json
import boto3

secrets_client = boto3.client('secretsmanager', region_name='us-east-1')

def load_secrets():
    """Fetch credentials from AWS Secrets Manager at job start"""
    
    # Load Gemini API key
    try:
        secret = secrets_client.get_secret_value(SecretId='osu-pipeline/gemini-api-key')
        api_key = json.loads(secret['SecretString'])['api_key']
        os.environ['GOOGLE_API_KEY'] = api_key
    except Exception as e:
        print(f"Warning: Could not load Gemini key from Secrets Manager: {e}")
        # Fall back to environment variable if set
    
    # Load RDS credentials
    try:
        secret = secrets_client.get_secret_value(SecretId='osu-pipeline/rds')
        rds_creds = json.loads(secret['SecretString'])
        os.environ['RDS_HOST'] = rds_creds.get('host')
        os.environ['RDS_PORT'] = str(rds_creds.get('port', 5432))
        os.environ['RDS_USER'] = rds_creds.get('username')
        os.environ['RDS_PASSWORD'] = rds_creds.get('password')
        os.environ['RDS_DBNAME'] = rds_creds.get('dbname')
    except Exception as e:
        print(f"Warning: Could not load RDS credentials from Secrets Manager: {e}")

# Call at job start
load_secrets()
```

---

## FIX 3: Rebuild and Push Docker Image

### Problem
- ECR image failing to pull from Fargate
- Possible image corruption or network issue

### Solution: Rebuild and Re-push

#### Step 3A: Rebuild Docker Image

```bash
cd D:/project_modular

# Build for ECR
docker build -t osu-pipeline:latest \
  --build-arg BUILDKIT_INLINE_CACHE=1 \
  .

# Tag for ECR
docker tag osu-pipeline:latest \
  225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline:latest
```

#### Step 3B: Login to ECR and Push

```bash
# Get ECR login token
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin \
  225989338968.dkr.ecr.us-east-1.amazonaws.com

# Push image
docker push 225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline:latest

# Verify push succeeded
aws ecr describe-images \
  --repository-name osu-pipeline \
  --region us-east-1 \
  --query 'imageDetails[0].[imagePushedAt,imageSizeBytes]'
```

---

## FIX 4: Update Job Definition with Fixed Configuration

### Changes to apply to `osu-pipeline-job` definition:

```json
{
  "jobDefinitionName": "osu-pipeline-job",
  "revision": 6,
  "containerProperties": {
    "image": "225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline:latest",
    "vcpus": 2,
    "memory": 3008,
    "jobRoleArn": "arn:aws:iam::225989338968:role/OSUPipelineBatchTaskRole",
    "environment": [
      {
        "name": "SLICE_SIZE",
        "value": "500"
      },
      {
        "name": "MAX_WORKERS",
        "value": "4"
      },
      {
        "name": "AWS_DEFAULT_REGION",
        "value": "us-east-1"
      }
    ],
    "ulimits": [
      {
        "hardLimit": 4096,
        "name": "nofile",
        "softLimit": 4096
      }
    ]
  },
  "timeout": {
    "attemptDurationSeconds": 28800
  }
}
```

**Key changes:**
- Added `SLICE_SIZE=500` environment variable
- Increased timeout to 8 hours (28800 sec) as safety margin
- Added file descriptor limits for large file operations
- Ensured Secrets Manager IAM role is attached

---

## FIX 5: Testing & Validation

### Test 1: Local Testing with Small Slice

```bash
# Test with 100 PDFs locally
export SLICE_SIZE=100
export INPUT_PDF_PATH=D:/project_modular/pdfs/ExportedFolderContents_13/1995/01\ -\ January/

python run_batch_job.py --test --slice-index 0

# Verify:
# ✓ Job completes in <30 minutes
# ✓ Output CSVs created with correct schema
# ✓ No memory errors
# ✓ Success rate matches expected (~50%)
```

### Test 2: AWS Batch Test Job

```bash
# Submit single test job with slice 0
aws batch submit-job \
  --job-name "osu-test-fixed-000" \
  --job-queue arn:aws:batch:us-east-1:225989338968:job-queue/osu-pipeline-queue \
  --job-definition osu-pipeline-job:6 \
  --array-properties size=1 \
  --container-overrides "environment=[{name=JOB_INDEX,value=0}]" \
  --region us-east-1

# Monitor in CloudWatch
# Verify:
# ✓ Job reaches RUNNING state (no ECR pull failure)
# ✓ Job completes in <5 hours (timeout is 8h)
# ✓ Output appears in S3 results/slice-00000/
# ✓ job_status.json created
# ✓ dot_coordinates.csv valid
```

### Test 3: Success Rate Validation

```bash
# After test job completes:
aws s3 cp s3://osu-well-records-225989338968/results/slice-00000/dot_coordinates.csv - | wc -l

# Verify:
# ✓ Line count > 0 (at least some successful extractions)
# ✓ Format matches expected CSV schema
# ✓ Coordinates are in valid Oklahoma bounds
```

---

## FIX 6: Full Resubmission Pipeline

### Command to resubmit all 391 slices with FIXED configuration

```bash
# Create submit script
cat > /tmp/submit_fixed_jobs.sh <<'SCRIPT'
#!/bin/bash
echo "Submitting all 391 slices with FIXED configuration..."
echo "Slice Size: 500 PDFs/job"
echo "Timeout: 8 hours"
echo ""

COUNTER=0
for slice_idx in {0..390}; do
  # Format slice index with leading zeros
  SLICE_ID=$(printf "%05d" $slice_idx)
  
  echo -n "Submitting slice $SLICE_ID... "
  
  JOB_ID=$(aws batch submit-job \
    --job-name "osu-rev6-${SLICE_ID}" \
    --job-queue arn:aws:batch:us-east-1:225989338968:job-queue/osu-pipeline-queue \
    --job-definition osu-pipeline-job:6 \
    --array-properties size=1 \
    --container-overrides "environment=[{name=JOB_INDEX,value=${slice_idx}}]" \
    --region us-east-1 \
    --query 'jobId' \
    --output text 2>&1)
  
  if [ -z "$JOB_ID" ] || [[ "$JOB_ID" == *"error"* ]]; then
    echo "FAILED: $JOB_ID"
  else
    echo "OK ($JOB_ID)"
    ((COUNTER++))
  fi
  
  # Rate limiting: 1 submission per second
  sleep 1
done

echo ""
echo "Submitted $COUNTER/391 jobs"
SCRIPT

chmod +x /tmp/submit_fixed_jobs.sh
/tmp/submit_fixed_jobs.sh
```

---

## IMPLEMENTATION SCHEDULE

| Step | Task | Time | Owner |
|------|------|------|-------|
| 1 | Clean C: drive, prune Docker | 15 min | System |
| 2 | Create AWS Secrets | 10 min | AWS CLI |
| 3 | Update IAM roles | 5 min | AWS IAM |
| 4 | Rebuild Docker image | 20 min | Docker |
| 5 | Push to ECR | 5 min | Docker/ECR |
| 6 | Create Job Definition v6 | 5 min | AWS CLI |
| 7 | Local testing (100 PDFs) | 30 min | Manual test |
| 8 | Single job AWS test | 5 hours | AWS Batch |
| 9 | Monitor test job | 15 min | CloudWatch |
| 10 | Resubmit all 391 slices | 10 min | Bash script |
| 11 | Monitor completion | 24 hours | Auto-refresh |
| **TOTAL** | **Full Recovery** | **~30.5 hours** | |

---

## SUCCESS CRITERIA

- ✅ All 391 slices submitted without errors
- ✅ <5% job failure rate (timeout + other errors)
- ✅ >95% jobs reach RUNNING status (no ECR pull failures)
- ✅ S3 outputs created for >380 slices
- ✅ Final well count: 40,000-45,000 (from all 13 collections)
- ✅ No new credential leaks
- ✅ Cost <$2,000 total for full pipeline

---

**Status:** Ready for implementation  
**Risk Level:** MEDIUM (requires AWS API changes, data pipeline impact)  
**Rollback Plan:** Keep current job definition v5 for fallback

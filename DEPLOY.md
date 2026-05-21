# OSU Well Pipeline — Cloud Deployment Guide

End-to-end steps to run the full 46 k-PDF pipeline on AWS using EC2 + Docker + RDS + S3 + Batch.
Budget note:  This guide uses `c5.4xlarge` Batch
instances (16 vCPU / 32 GB) to maximise throughput.  Typical cost for one full run: **$50–120**
depending on Vision API calls (cached after first run).

---

## Prerequisites

| Tool | Version |
|------|---------|
| AWS CLI | v2 |
| Docker | 24+ |
| Python | 3.11 |
| `aws configure` | done (your account, us-east-1) |

---

## Step 0 — One-time AWS infrastructure setup

> Skip if infrastructure already exists; verify with the `aws` commands shown.

### 0.1 ECR repository (stores your Docker image)

```bash
aws ecr create-repository --repository-name osu-pipeline --region us-east-1
# Note the repositoryUri in the output, e.g.:
#   123456789.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline
```

### 0.2 Secrets Manager — GCP credentials + Gemini key

```bash
aws secretsmanager create-secret --name osu-pipeline/credentials  --secret-string '{
    "gcp_service_account": <paste entire contents of your service-account.json>,
    "gemini_api_key": "your-gemini-api-key"
  }'
```

### 0.3 IAM Task Role

Create `task-role-policy.json` (see `aws/config/task_role_policy.json`), then:

```bash
# Create the role
aws iam create-role --role-name osu-batch-task-role --assume-role-policy-document file://aws/config/trust_batch.json

# Attach managed policies
aws iam attach-role-policy --role-name osu-batch-task-role --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess

aws iam put-role-policy  --role-name osu-batch-task-role --policy-name osu-task-inline --policy-document file://aws/config/task_role_policy.json
```

### 0.4 Batch Compute Environment, Queue, and Job Definition

Edit `aws/config/*.json` — replace all `REPLACE-WITH-*` placeholders — then:

```bash
aws batch create-compute-environment --cli-input-json file://aws/config/compute_env.json

aws batch create-job-queue --cli-input-json file://aws/config/job_queue.json

aws batch register-job-definition --cli-input-json file://aws/config/job_definition.json
```

---

## Step 1 — Store RDS credentials in Secrets Manager (or Parameter Store)

The pipeline reads RDS creds **only from environment variables** — never from source code.
AWS Batch injects them automatically if you set them in the job definition's `environment` section.

```bash
# Option A: in job_definition.json "environment" array (plaintext, OK for internal use)
# Option B: Secrets Manager (recommended for shared teams)
aws secretsmanager create-secret --name osu-pipeline/rds --secret-string '{
    "RDS_HOST":     "oklahomagridlatlongdb.xxx.us-east-1.rds.amazonaws.com",
    "RDS_DBNAME":   "Oklahomaplss",
    "RDS_USER":     "LookUpMaster",
    "RDS_PASSWORD": "your-password"
  }'
```

Then in `run_batch_job.py`, extend `_load_secrets()` to also pull RDS creds:

```python
rds = payload.get("rds_creds", {})
for k in ("RDS_HOST", "RDS_DBNAME", "RDS_USER", "RDS_PASSWORD"):
    if k in rds:
        os.environ[k] = rds[k]
```

---

## Step 2 — Build and push the Docker image

```bash
# Authenticate Docker to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin  123456789.dkr.ecr.us-east-1.amazonaws.com

# Build (from project root — Dockerfile is here)
docker build -t osu-pipeline .

# Tag and push
REPO=123456789.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline
docker tag osu-pipeline:latest $REPO:latest
docker push $REPO:latest

echo "Image pushed: $REPO:latest"
```

---

## Step 3 — Build the dataset index (if not already done)

> This scans S3 and produces `dataset_index.csv` used by every Batch task.

```bash
python aws/scan_s3.py --bucket osu-well-records-225989338968 --mode flat --output D:/project_outputs/dataset_index.csv

# Upload the index to S3 so Batch workers can read it
aws s3 cp D:/project_outputs/dataset_index.csv  s3://osu-well-records-225989338968/index/dataset_index.csv
```

---

## Step 4 — Local smoke-test (5 records)

Before burning EC2 hours, verify the pipeline works end-to-end locally.

```bash
# Extract 5 rows from the index
python -c "
import csv
rows = list(csv.DictReader(open('D:/project_outputs/dataset_index.csv')))
import csv as c
with open('/tmp/smoke_index.csv', 'w', newline='') as f:
    w = c.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows[:5])
print('wrote 5 rows')
"

# Run the pipeline on those 5 records
cd D:/project_modular/project
python main.py  --index /tmp/smoke_index.csv --output D:/project_outputs/smoke_test --workers 2
```

Expected output structure:
```
D:/project_outputs/smoke_test/
  processing_status.csv   ← per-record stage results
  success.csv             ← records that completed all stages
  failed.csv              ← records with at least one failure
  grids/                  ← extracted grid PNGs
  dots/                   ← U-Net overlay PNGs
  locations/
  counties/
  logs/
  manual_review/
  run_insights.md
  parameter_suggestions.json
```

---

## Step 5 — Run pytest test suite

```bash
cd D:/project_modular
pip install pytest
pytest tests/test_pipeline.py -v
```

All 40+ tests should pass.  The security tests will fail if any hardcoded
credentials are detected — fix them before deploying.

---

## Step 6 — Submit the full Batch array job

```bash
python aws/submit_jobs.py \
  --queue   osu-batch-queue \
  --jobdef  osu-pipeline-jobdef:1 \
  --bucket  osu-well-records-225989338968 \
  --index   index/dataset_index.csv \
  --slice   500 \
  --workers 8 \
  --secret  osu-pipeline/credentials \
  --name    osu-pipeline-run1
```

This submits ~93 array tasks (46 k records ÷ 500 per task).
Watch progress:

```bash
# List running jobs
aws batch list-jobs --job-queue osu-batch-queue --job-status RUNNING

# Describe a specific job
aws batch describe-jobs --jobs <job-id>

# Tail logs (CloudWatch)
aws logs tail /aws/batch/job --follow
```

---

## Step 7 — Merge results after all tasks complete

```bash
python aws/merge_results.py \
  --bucket osu-well-records-225989338968 \
  --prefix results/ \
  --output D:/project_outputs/merged/
```

---

## Step 8 — Monitoring and cost control

### CloudWatch dashboard
- ECS CPU/Memory utilisation per task
- Vision API call counts (check GCP console)
- Failed job counts

### Automatic shutdown
Batch automatically terminates containers when tasks finish — no idle charges.

### RDS
RDS is always-on (you pre-paid for it or it has a fixed monthly cost).
The pipeline opens connections only during coord-enrichment, then closes them.
No idle connection charges.

### S3
S3 costs are negligible (< $5 for full dataset).

### Vision API
First run: ~$30–60 (46 k PDFs × 2 pages × $1.50/1000 calls).
Subsequent runs: $0 — disk cache prevents re-calling the API for the same image.

---

## Environment variable reference

| Variable | Required | Description |
|----------|----------|-------------|
| `RDS_HOST` | Yes | RDS PostgreSQL endpoint |
| `RDS_PORT` | No | Default: 5432 |
| `RDS_DBNAME` | Yes | Database name |
| `RDS_USER` | Yes | DB username |
| `RDS_PASSWORD` | Yes | DB password |
| `GOOGLE_APPLICATION_CREDENTIALS` | Yes (local) | Path to GCP JSON |
| `GOOGLE_API_KEY` | Yes | Gemini API key |
| `GOOGLE_CREDS_SECRET_ID` | Yes (Batch) | Secrets Manager ID |
| `UNET_CHECKPOINT` | No | Path to `.pth` file |
| `OUTPUT_ROOT` | No | Output directory (default: `/tmp/output` in Docker) |
| `INPUT_BUCKET` | Yes (Batch) | S3 input bucket |
| `OUTPUT_BUCKET` | Yes (Batch) | S3 output bucket |
| `INDEX_KEY` | Yes (Batch) | S3 key for dataset_index.csv |
| `SLICE_SIZE` | No | Records per task (default: 1000) |
| `WORKERS` | No | Threads per container (default: 8) |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `EnvironmentError: RDS_HOST not set` | Creds not injected | Check job def `environment` |
| `FileNotFoundError: unet_best.pth` | Checkpoint missing from image | Re-run `docker build` |
| `ServiceUnavailable` (Vision API) | GCP quota exceeded | Wait; pipeline auto-retries |
| All records `grid: failed/not_detected` | Wrong PDF tier config | Check `tier_for()` boundaries |
| `coord_enrichment skipped: connection refused` | RDS security group | Add Batch VPC CIDR to RDS SG |
| Empty `dot_coordinates.csv` | No dot stage records | Check dot stage errors in `failed.csv` |

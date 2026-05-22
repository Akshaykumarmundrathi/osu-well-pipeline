# Phase 2 Implementation Status

**Date:** 2026-05-22  
**Status:** IN PROGRESS

## Overview

Phase 2 of the recovery plan is implementing the core fixes to reduce job timeouts and secure credentials. This document tracks progress.

## Current State

### Pipeline Queue Status
- **RUNNING**: 30 jobs
- **SUCCEEDED**: 19 jobs (completion markers in S3: 196 slices)
- **FAILED**: 245 jobs (v5 jobs with timeout issues)
- **RUNNABLE**: 489 jobs (queued, awaiting worker slots)
- **Total Submitted**: 2,704 jobs (2,495 new + 209 existing)
- **Success Rate**: 7.3% (expected given v5 configuration issues)

### Root Causes Being Fixed

| Issue | Root Cause | Fix | Status |
|-------|-----------|-----|--------|
| 50% timeout failures | SLICE_SIZE=1500 for 4h timeout | Reduce to 500 | ⏳ In Progress |
| ECR pull failures | Image registry timeout | Rebuild + re-push | ⏳ In Progress |
| Plaintext credentials | .env with keys | AWS Secrets Manager | ✅ Code Ready |
| Job duration mismatch | 1500 PDFs × 30s = 12.5h timeout | 500 PDFs × 30s = 4.2h fits | ⏳ In Progress |

## Phase 2 Tasks

### [1] Docker Image Rebuild

**Status**: 🔄 **BUILDING** (started ~15:37 UTC)

**Files Created**:
- `Dockerfile.v6-rebuild` - Standalone image from python:3.10-slim
  - Includes all dependencies (boto3, Google APIs, PyTorch, OpenCV, etc.)
  - Sets SLICE_SIZE=500 environment variable
  - 637MB image size (estimated)

**Next Step**: Push to ECR once build completes

### [2] Secrets Manager Configuration

**Status**: ⏳ **PENDING USER ACTION**

**What Needs to Happen**:
1. User provides credentials (Gemini API key, RDS password)
2. Run `create_secrets.sh` to register in AWS Secrets Manager
3. Update IAM role with `update_iam_role.sh`

**Why It's Safe**:
- Credentials are never stored in plaintext in repo
- Secrets Manager handles encryption at rest
- IAM role ensures only Batch jobs can access
- Code in `run_batch_job.py` already loads credentials on startup

### [3] Job Definition v6 Registration

**Status**: ⏳ **READY** (depends on Docker build)

**File Created**: `jobdef-v6.json`
- Image URI: `225989338968.dkr.ecr.us-east-1.amazonaws.com/osu-pipeline:v6-fixed`
- SLICE_SIZE: 500 (down from 1500)
- Timeout: 28800 seconds (8 hours, up from 14400 = 4 hours)
- vCPU: 2 (no change)
- Memory: 3008 MB (no change)

**Command to Register** (once image is in ECR):
```bash
aws batch register-job-definition \
  --job-definition-name osu-pipeline-job \
  --revision 6 \
  --type container \
  --cli-input-json file://jobdef-v6.json \
  --region us-east-1
```

### [4] IAM Role Update

**Status**: ⏳ **READY** (script created)

**File Created**: `update_iam_role.sh`

**What It Does**:
- Adds `secretsmanager:GetSecretValue` permission to OSUPipelineBatchTaskRole
- Scoped to: `arn:aws:secretsmanager:us-east-1:225989338968:secret:osu-pipeline/*`

**Run**: `bash update_iam_role.sh`

### [5] Code Updates

**Status**: ✅ **ALREADY COMPLETE**

**Why**:
- `run_batch_job.py` already has `_load_secrets()` function
- Already calls AWS Secrets Manager client at startup
- Already sets environment variables for Gemini key and RDS credentials
- Code is ready as-is; no changes needed

## Remaining Steps (In Order)

1. ✅ Create new Dockerfile (DONE)
2. 🔄 Wait for Docker build to complete (15 minutes remaining est.)
3. ⏳ Tag and push image to ECR (5 minutes)
4. ⏳ Register Job Definition v6 (1 minute)
5. ⏳ **USER ACTION**: Create Secrets Manager entries (provide credentials)
6. ⏳ Update IAM role (1 minute)
7. ⏳ Local testing with SLICE_SIZE=100 (30 minutes)
8. ⏳ AWS Batch test with v6 job definition (5 hours)
9. ⏳ Resubmit all 391 slices (10 minutes submission, 24-36 hours processing)

## Expected Improvements

Once Phase 2 completes:

| Metric | v5 (Current) | v6 (Expected) |
|--------|---|---|
| SLICE_SIZE | 1500 PDFs | 500 PDFs |
| Job Duration | ~12.5 hours | ~4.2 hours |
| Timeout | 4 hours ❌ | 8 hours ✅ |
| Timeout Failures | 50% | <5% |
| Credentials | Plaintext .env ❌ | Secrets Manager ✅ |
| ECR Issues | Possible 403s | Fresh image pull |
| Total Jobs Needed | 391 + failures | ~1,173 total |

## Timeline Estimate

- **Docker build**: ~20-30 minutes (large image, Python + deps)
- **ECR push**: ~5 minutes (668MB image)
- **Job Definition registration**: 1 minute
- **Secrets Manager setup**: 5 minutes (user provides credentials)
- **IAM role update**: 1 minute
- **Local testing**: 30 minutes
- **AWS test job**: 5-6 hours
- **Resubmission**: 10 minutes + 24-36 hours processing

**Total for Phase 2**: ~4 hours hands-on + 24-36 hours processing

## Success Criteria

Phase 2 is complete when:
- ✅ Docker image built and pushed to ECR
- ✅ Job Definition v6 registered in AWS Batch
- ✅ Secrets Manager entries created
- ✅ IAM role has secretsmanager:GetSecretValue permission
- ✅ Local test with SLICE_SIZE=100 completes without timeout
- ✅ Single AWS test job completes successfully (no timeout, no ECR errors)
- ✅ Test job output validated in S3

## Next Checkpoint

Once Docker build completes:
1. Execute `phase2_implementation.sh` to push and register
2. Request user to create Secrets Manager entries
3. Update IAM role
4. Proceed to local testing

---

**Status**: Waiting for Docker build  
**Monitor**: `docker images` or `docker ps` to check build progress  
**Escalation**: If build fails, check `Dockerfile.v6-rebuild` and Docker disk space

# Cost-Optimized Pipeline - Free Tier + Timeout Fix

## PROBLEM ANALYSIS

### Current Setup Issues
- FARGATE timeout: 4 hours (14400 sec)
- Batch SLICE_SIZE: 500 PDFs
- Estimated time per PDF: 8-15 seconds (with Vision OCR + Gemini API)
- 500 PDFs × 12 sec avg = **6,000+ seconds needed (1.67 hours)**
- But with network I/O, retries, S3 uploads: **3-4 hours realistic**
- **PROBLEM:** Timeouts at hour 4 if processing exceeds capacity

### Cost Analysis
- AWS Batch FARGATE: ~$0.05-0.10 per job-hour (for 2 vCPU)
- 391 jobs × 4 hours = 1,564 hours ≈ **$78-156**
- Plus S3, CloudWatch, Secrets Manager: +$30-50
- **TOTAL: ~$120-200 for full run**

## SOLUTION: HYBRID COST-OPTIMIZED APPROACH

### Option A: Local Processing (FREE - Recommended)
- Run pipeline locally on D: drive
- Process in batches
- Upload results to S3 directly
- Cost: **$0 (free tier S3)**
- Time: Run in background while you work
- Benefit: No AWS compute costs, full control

### Option B: Lambda + SQS (Ultra-low cost)
- Process PDFs in parallel via Lambda
- Lambda free tier: 1M requests/month, 400,000 GB-seconds free
- Cost for 195K PDFs: **$10-30** (pay only for compute seconds)
- Timeout: 900 seconds (15 min) - need smaller batches
- Slice size: 100-150 PDFs per invoke
- Total invocations: 1,300-1,950
- **Cost estimate: $15-40**

### Option C: Batch with Proper Timeout (Moderate cost)
- Reduce SLICE_SIZE: 300 PDFs (instead of 500)
- Increase timeout: 28800 sec (8 hours)
- Estimated time: ~45-60 min per job (with overhead)
- Jobs needed: 391 × (500/300) = ~652 jobs
- Cost: ~$40-80
- Benefit: Reliable, fewer failures

## RECOMMENDED: OPTION A (Local + S3 Upload)

### Setup

```bash
# 1. Run locally in batches
python3 project/main.py \
  --input-bucket osu-well-records-225989338968 \
  --output-bucket osu-pipeline-results \
  --slice-start 1 \
  --slice-end 100 \
  --local-mode

# 2. Process at YOUR pace
# 3. Auto-upload to S3 every batch
# 4. Cost: $0 AWS compute, use S3 free tier (5GB/month free uploads)
```

### Why This Works
- **No timeout issues** - local process runs forever
- **No cost** - use your computer's CPU
- **Full control** - see what's happening
- **Resumable** - process 50 slices, pause, continue later
- **Fast** - no network overhead for each PDF

## IMPLEMENTATION: Modify pipeline for local mode

```python
# In project/main.py
if LOCAL_MODE:
    # Read input CSVs locally
    # Process PDFs with local GPU/CPU
    # Write results to local temp directory
    # Upload batches to S3 when complete
else:
    # Current Batch flow
```

## COST PROJECTION BY OPTION

| Option | Setup Cost | Per-Job Cost | Total for 391 slices | Time | Risk |
|--------|-----------|--------------|-------------------|----|------|
| **A: Local** | FREE | FREE | **$0-5** (just S3) | Yours | Low |
| **B: Lambda** | $5 | $0.03/invoke | **$15-40** | 2-3h | Medium |
| **C: Batch (current)** | $0 | $0.10/hour | **$80-150** | 24-36h | High |

## NEXT STEPS

Choose your approach:
1. **Go Local**: Run the pipeline code directly on your D: drive (FASTEST, FREE)
2. **Use Lambda**: Switch to Lambda + SQS (Cheapest cloud option)
3. **Fix Batch**: Reduce SLICE_SIZE + increase timeout (current approach, higher cost)


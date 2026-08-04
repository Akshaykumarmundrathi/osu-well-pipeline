# AWS Cloud-Run Design — Finish the Corpus on Fargate

Goal: process the remaining **~520,000 unmapped records** in **hours** on AWS
Fargate, instead of weeks on the 7.4 GB laptop. Same account as S3 + RDS →
no cross-account friction.

## 1. What's already deployed (verified Jun-16)
| Component | State |
|---|---|
| Fargate compute env `osu-pipeline-ce` | ENABLED/VALID, FARGATE, maxvCpus 4096 |
| Job queue `osu-pipeline-queue` | ENABLED |
| Job definitions (`osu-pipeline-job`, …) | ACTIVE — 4 vCPU / 16 GB, image `…/osu-pipeline:v15-skip-anchor` |
| Secrets (`osu-pipeline/credentials`, `/rds`, `/gemini-api-key`) | present |
| Networking | 3 subnets |
| **Account quota: Fargate On-Demand vCPU** | **1,000** ← real concurrency ceiling |

## 2. The one gap → being fixed
- **ECR repo was missing** → created `…/osu-pipeline`.
- **Docker image must be (re)built + pushed.** Local Docker is down + laptop is
  busy, so build via **CodeBuild from an S3 source bundle** (includes the
  gitignored `unet_best.pth` + `unet_dot_detector.py`). Tag → `v16` (update job
  def to match, or push as `v15-skip-anchor`).
- Update `osu-pipeline/gemini-api-key` secret to the **7 working keys**.

## 3. How the run executes
```
dataset_index (571,446) ── orchestrate.py ──> N slices (e.g. 1,000 × ~520 recs)
                                  │
                          AWS Batch submit (array job)
                                  │
            ┌─────────────────────┼─────────────────────┐
       Fargate task           Fargate task   …  (up to 250 concurrent
       4 vCPU/16 GB           4 vCPU/16 GB        @ 4 vCPU = 1,000 vCPU cap)
            │                     │
   run_batch_job.py: pull slice from S3, process records (latlong→grid→
   location→county→dot), OCR via Vision, county via Gemini (7-key rotation),
   write per-record results + status shard back to S3 every 300 s; SIGTERM
   handler checkpoints to S3 on Spot reclaim.
                                  │
                       collect_results.py ── merge shards ──> processing_status
                                  │
              run_coord_enrichment (RDS) ──> dot_coordinates
                                  │
                    build_map_data ──> well_locations.json ──> GitHub Pages
```

## 4. Concurrency & time
- 1,000 vCPU ÷ 4 vCPU/task = **250 concurrent tasks**.
- ~520k records ÷ 250 = ~2,080 records/task. At ~4–6 s/record (Vision-bound) ≈
  **2.5–3.5 hours** wall-clock for the full remainder.
- Ramp: burst launch rate (100→1,000 per Victor) fills to 250 tasks in <1 min.
- **Option — Spot:** Fargate Spot vCPU quota is 140 (35 tasks) and ~70% cheaper,
  but interruptible; the SIGTERM checkpointer handles reclaims. Use On-Demand for
  the speed run, Spot for a cheaper slower run.

## 5. Cost (the real gate)
| Item | Estimate |
|---|---|
| Fargate compute (250 tasks × ~3 h × 4 vCPU × ~$0.04/vCPU-h) | **~$120** |
| **Google Vision OCR** (~520k records × ~1–3 images) | **~$1,200–1,400** (the dominant cost) |
| Gemini (county) | $0 (free tier, 7-key rotation) |
| S3 / data transfer / RDS | a few $ |
| **Total to finish the entire corpus** | **~$1,350–1,550** |

## 6. Safety / controls (build into the launch)
1. **Kill switch:** `aws batch cancel-job` + disable the job queue → all tasks
   stop; partial results already in S3 are safe.
2. **Cost cap dry-run:** launch **one slice first** (≈520 records, ~$1.50) →
   verify outputs + accuracy → then submit the rest. Prevents a $1,400 mistake.
3. **Budget alarm:** AWS Budgets alert at $200 / $800 / $1,400.
4. **Idempotent + resumable:** every task skips already-done records (status in
   S3); a crashed/reclaimed task just re-runs its slice — no double Vision charge
   (the per-image cache + done-skip guard apply).
5. **Monitoring:** `monitor.py` tails the queue (RUNNING/SUCCEEDED/FAILED counts)
   + CloudWatch logs; fold to the map incrementally as slices complete.

## 7. Launch sequence (once image is in ECR + budget approved)
```
1. python aws/build_image.py (or CodeBuild)         # image -> ECR
2. update gemini secret -> 7 keys
3. python aws/orchestrate.py --slices 1 --smoke     # 1-slice dry run (~$1.50)
4. verify outputs/accuracy in S3
5. python aws/orchestrate.py --all                  # full submit (250 concurrent)
6. python aws/monitor.py                            # watch
7. collect_results -> enrich -> build_map -> push   # fold to live map
```

**Status:** infra ready except the image build + secret update. The run is a
*budget decision*, not a capacity one — 1,000 vCPUs are available now.

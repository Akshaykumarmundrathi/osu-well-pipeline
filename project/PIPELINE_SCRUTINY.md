# Pipeline Scrutiny & Cloud-Hardening (Jun-16)

Full-pass review of the pipeline for cloud-native readiness, insight leverage,
and breakdown robustness. Changes are additive and tested; the working pipeline
(50k+ wells mapped) was not refactored.

## Robustness — already solid (verified, no change needed)
- **Stage level:** every stage runs inside try/except in `main._process_record`;
  on exception the record is marked not-detected with the error, logged with
  `exc_info`, and the PDF manager is flagged dirty so the next stage gets a fresh
  handle. Side-effects (review-queue writes, image relativization) are each
  guarded.
- **Record level:** PDF-open failures are caught and skipped, not fatal.
- **Batch level (`run_batch_job.py`):** Secrets Manager pull, S3 checkpoint
  download + resume, periodic S3 sync, SIGTERM handler (flush → exit 130 → Batch
  auto-retry on Spot reclaim), disk watchdog. Cloud-resilient.

## Insight leverage (integrated)
1. **Printed lat/long (exact coords):** the latlong stage regex already matches
   the modern `Latitude: 36.958904 Longitude: -98.256947` format, so the FULL
   pipeline (and the cloud run) extracts exact coordinates automatically — the
   precision win is native, not a post-pass.
2. **Section-number fallback:** `_SEC_BEFORE_TR` recovers the section when OCR
   drops the "SEC" label (number adjacent to township).
3. **County→direction backfill (RDS):** `plss_resolver` now loads
   `county_directions.csv` (RDS-derived ground truth) — for the 59/77 (E/W) and
   60/77 (N/S) deterministic counties it returns the exact direction, resolving
   records that lost their N/S or E/W suffix; non-deterministic counties keep the
   meridian heuristic. Verified: a suffix-less record resolves to the same coords.

## Cloud-native — critical fix
- **Dockerfile `USE_VISION_API` was 0 (Tesseract).** Tesseract is proven
  inaccurate on the older scans (C1–C5 location 0–8%); a cloud run on that
  default would produce garbage. **Fixed to 1 (Vision)** + a `run_batch_job`
  guard that forces Vision unless an operator explicitly opts into a Tesseract
  experiment, and logs the active backend to CloudWatch.
- Paths are env-var-backed (`OUTPUT_ROOT`, `SOURCE_ROOT`); secrets via Secrets
  Manager; `county_directions.csv` ships in the image (under `project/`).

## Action item for the operator
- **Update the `osu-pipeline/gemini-api-key` secret to the 7 working keys** (the
  2 deleted `AIzaSy…` keys were pruned locally; the cloud pulls this secret).

## Net
The pipeline is robust and cloud-ready; the one blocking cloud bug (Tesseract
default) is fixed, and the proven free precision/coverage wins are now native to
the processing path, so the cloud run benefits automatically.

# Session Log — 2026-07-14 · Cloud-native hardening & cost-control review

*Project discussion, implementation work, technical findings, and open engineering decisions. Companion to the commit history; see `CASE_STUDY.md` §10.*

## Entry state

- Map live with 51,559 wells.
- C11 grid campaign complete; automatically stopped at the configured spend cap.
- No processing jobs running.
- Seven working Gemini API keys available.

## Review scope

Review the pipeline for cloud-native readiness, incorporating experimental findings and validating error handling, logging, recovery, configuration, and cost controls.

Primary questions:
- Are processing stages adequately isolated against failures?
- Do production defaults reflect the most accurate extraction methods identified during testing?
- Are existing precision and coverage improvements integrated into the cloud execution path?
- Can processing pause when free-tier quotas are exhausted and resume after quota resets?
- Which remaining costs can be controlled or avoided?

## Implementation and verification

### H1 — Robustness audit

Verified existing safeguards:

- Per-stage `try/except` handling.
- Manager re-creation for failure isolation.
- Exception logging with `exc_info`.
- Record-level PDF-open guards.
- AWS Batch SIGTERM handling: checkpoint → S3 upload → automatic retry.
- Resume from S3 checkpoints.
- Disk-usage watchdog.

No code changes were required.

### H2 — Coordinate extraction and parser coverage

Confirmed that the latitude/longitude extraction stage already supports modern decimal coordinates:

```text
Latitude: 36.958904 Longitude: -98.256947
```

The full pipeline, including cloud execution, therefore uses exact printed coordinates when available.

Also verified that the section-number fallback, `_SEC_BEFORE_TR`, is already integrated.

### H3 — County-based direction backfill

Integrated the RDS-derived `county_directions.csv` into `plss_resolver` priors.

- Deterministic E/W direction available for 59 of 77 counties.
- Deterministic N/S direction available for 60 of 77 counties.
- Records missing direction suffixes can use county-derived priors.
- Counties without deterministic directions retain the meridian heuristic.

Smoke test: an STR reference without direction suffixes resolved to the same coordinates as its fully specified equivalent.

Commit: `de960f7`

### H4 — Cloud-native configuration correction

Identified and fixed the session’s **critical** configuration issue: the Dockerfile defaulted to `USE_VISION_API=0`, selecting Tesseract despite earlier experiments showing inadequate accuracy on old scans.

Changes:
- Changed the default to Vision.
- Added a `run_batch_job` startup guard that selects Vision unless an operator explicitly opts into a Tesseract experiment.
- Added startup logging visible in CloudWatch.
- Verified environment-backed paths.
- Verified Secrets Manager integration.
- Verified that `county_directions.csv` is included in the container image.

Commit: `bb311c0`

### Documentation and project-state persistence

- Created `PIPELINE_SCRUTINY.md` to document the audit and findings.  
  Commit: `1e35f68`
- Updated the project checkpoint, `session-checkpoint-jun16-late.md`, and its index.

## Technical findings

### Old-form coverage is limited by missing section numbers

The section number remains the main coverage constraint on old grid forms. Most unresolved cases reflect information that was not captured, rather than a parser defect.

The session estimated that approximately 13% is recoverable through free processing improvements.

### Runtime defaults must match experimental findings

The pipeline’s error handling and recovery mechanisms were already robust. The main operational risk was a configuration default that contradicted prior accuracy testing.

Review takeaway: audit deployed defaults and startup behavior against pilot results—not only the available code paths.

## Proposed improvements — not yet implemented

### Gemini quota-aware cooldown and resume

Proposed behavior:
- Pause county extraction when all seven configured keys encounter daily request-quota exhaustion (`429-RPD`).
- Resume after the daily quota reset.
- Persist progress across the pause.
- Pace extraction around available free-tier capacity.

This remains a proposed feature, not an implemented capability.

### Explicit Vision-call spending limit

Proposed behavior:
- Track new Vision calls.
- Stop processing after a configured call limit.
- Use that limit as an explicit guard against additional OCR spending.

This remains separate from the existing campaign spend-cap behavior.

## Cost and capacity assessment

The following reflects the session’s recorded estimates and assumptions, not newly verified pricing.

| Component | Recorded assessment | Project implication |
|---|---|---|
| Vision OCR | First 1,000 images per month free, followed by usage-based charges. | Daily cooldown does not eliminate OCR charges. |
| Gemini county extraction | Uses a daily free-tier request allowance, with reset around Pacific midnight. | Quota-aware pause/resume could pace processing within available capacity. |
| AWS Fargate | Approximately $120 estimated for the full run, with $100 in available credits. | Credits would offset most, but not all, of the estimated compute cost. |
| Modern-text processing | Free, non-Vision extraction opportunities already exhausted on C11–C13. | No further bulk recovery was identified through this path. |
| Old-scan backlog | Approximately 470,000 records; Tesseract did not meet accuracy requirements in prior testing. | Vision remains the selected OCR path. |
| Remaining Vision budget | Approximately $1,200–$1,400. | Bulk completion requires a budget decision under the current approach. |

The review did not identify a no-cost path for completing the old-scan backlog at the required accuracy.

## Outcome and remaining project actions

The review confirmed existing recovery safeguards and cloud-readiness checks, integrated county-based direction backfill, and corrected the OCR default that conflicted with prior testing.

Remaining actions:
- Select the AWS container image-build path.
- Approve or revise the Vision processing budget.
- Update `osu-pipeline/gemini-api-key` with the seven working keys.
- Decide whether to implement Gemini quota-aware cooldown and resume.
- Decide whether to implement an explicit Vision-call limit.

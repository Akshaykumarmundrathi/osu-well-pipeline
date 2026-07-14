# Session Log — 2026-07-14 · Cloud-native hardening & cost-control review

*Points of discussion, actions, thoughts, ideas, and work from the working
session. Companion to the commit history; see `CASE_STUDY.md` §10.*

Entry state: map **51,559 wells** live; C11 grid campaign complete (auto-stopped
at the user's spend cap); nothing running; 7 working Gemini keys.

---

## Points of discussion
- **"Scrutinize every code file → cloud-native, leverage new insights, solidify
  breakdowns / error logs / catch blocks / robustness."** The framing task for
  the session.
- **Can we run "free only"** — cool-off when free credits exhaust, resume when the
  free tier resets, all within AWS free resources?
- **Immigration admin**: report employment termination (Jun 30) + new employment
  (Jul 2) and ask the professor/DSO to update the SEVP portal — what's needed from
  the student's end.
- **Context refresh**: persist current project state to memory.

## Actions taken
- **H1 — Robustness audit.** Verified the pipeline is already hardened: per-stage
  try/except with manager re-creation isolation and `exc_info` logging; record-
  level PDF-open guards; Batch-level SIGTERM checkpoint→S3→auto-retry, S3 resume,
  disk watchdog. No change needed.
- **H2 — Insight leverage.** Confirmed the latlong stage regex already captures
  the modern decimal `Latitude: 36.958904 Longitude: -98.256947` format, so the
  full pipeline (and the cloud run) gets exact printed coordinates natively.
  Section-number fallback (`_SEC_BEFORE_TR`) already integrated.
- **H3 — County→direction backfill.** Wired the RDS-derived `county_directions.csv`
  (E/W deterministic 59/77, N/S 60/77) into `plss_resolver` priors so records that
  lost their N/S or E/W suffix still resolve; non-deterministic counties keep the
  meridian heuristic. Smoke-tested: a suffix-less STR resolved to the *same*
  coordinates as the fully-specified one. **Commit `de960f7`.**
- **H4 — Cloud-native pass. Critical bug caught:** the Dockerfile defaulted to
  `USE_VISION_API=0` (Tesseract — proven inaccurate on old scans). Fixed to Vision
  + added a `run_batch_job` startup guard/log that forces Vision unless an operator
  explicitly opts into a Tesseract experiment, surfaced to CloudWatch. Verified
  paths are env-backed, secrets via Secrets Manager, `county_directions.csv` ships
  in the image. **Commit `bb311c0`.**
- Wrote **`PIPELINE_SCRUTINY.md`** summarizing the review. **Commit `1e35f68`.**
- Updated the memory checkpoint (`session-checkpoint-jun16-late.md`) + index.
- Drafted the **SEVP employment-change email** (to DSO, professor cc'd) with the
  three student-side questions (new/amended I-983? self-update the SEVP Portal vs.
  DSO SEVIS update? required documents).

## Thoughts / analysis
- The **section number is the coverage ceiling** on old grid forms — mostly
  genuinely uncaptured, not a parser gap. Only ~13% is free-recoverable.
- **Robustness was already solid**; the real risk was a *configuration* default
  (Tesseract) contradicting an experimental finding — audit runtime defaults
  against what the pilots proved, not just the code paths.

## Ideas surfaced (not yet built)
- **Autonomous Gemini free-tier day-cooldown**: pause when all 7 keys hit 429-RPD,
  auto-resume after the Pacific-midnight reset — keeps county extraction strictly
  free and paces a Gemini-bounded run.
- **Hard Vision-spend cap**: stop after N new Vision calls as an explicit cost guard.

## Cost reality established
- **Vision is the gate** — free only for the first 1,000 images/month, then pay-per-
  use, and it is a *Google* cost, not an AWS resource. No cool-off recovers it.
- **Gemini** county extraction *is* free-tier (daily RPD, resets ~PT-midnight).
- **AWS Fargate compute** (~$120 for the full run) is covered by the $100 credits.
- **The free lever (modern-text path, no Vision) is already exhausted** on C11–C13;
  the ~470K old-scan backlog genuinely needs paid Vision (Tesseract dead).
- Conclusion: there is **no free way** to finish the old-scan bulk — it is a
  ~$1,200–1,400 Vision budget decision, not a capacity or engineering gap.

## Outcome
Pipeline confirmed robust and cloud-ready; the one blocking cloud bug (Tesseract
default) fixed; proven free precision/coverage wins are native to the processing
path. Open decisions handed to the operator: AWS image-build path + Vision budget;
update the `osu-pipeline/gemini-api-key` secret to the 7 working keys.

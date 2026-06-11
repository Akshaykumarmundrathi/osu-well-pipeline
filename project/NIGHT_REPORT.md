# Wake-Up Report — 2026-06-11 (5-hour autonomous block, 07:41–13:25)

## Headline numbers

| Metric | Before | After |
|---|---|---|
| **Wells on live map** | 1,615 | **4,778** (+196%) |
| Resume startup | 12.6s | **2.1s** (run-scoped CSV load) |
| Coordinate correctness | ~2,000 silently mirrored E/W | 505 deterministically fixed, 1,518 flagged on map |
| GitHub repo | no README | professional README + About + topics, stale files purged |

## The three big finds (your instincts were right)

### 1. The E/W mirror (county-consistency audit)
Built `audit_consistency.py`: every resolved well's extracted county
cross-examined against the county its PLSS cell actually sits in.
**52% disagreed — a systematic pattern**: suffixless ranges were defaulted
to W during resolution, dropping eastern wells into western Oklahoma
(Creek→Kingfisher, Nowata→Woods, Tulsa→Blaine). `re_resolve_mismatches.py`
repaired 505 deterministically (county pins the direction via RDS, both-
direction query, exactly-one-match wins). The 1,518 deeper-incompatibility
wells carry `county_mismatch` flags visible in map popups.

### 2. The label allowlist eating wells
`build_map_data.py` had a hardcoded `_RESOLVED_SOURCES` allowlist — every
row whose resolution label post-dated the list was silently dropped
(county_pinned, corrected, direct_latlong, p4, centroids): **~2,400 valid
wells never reached the map across all previous builds.** Coordinates +
Oklahoma bounds now decide; labels are popup metadata. 2,341 → 4,778.

### 3. Era-wide suspect generalization (your directive)
`sweep_suspect_dones.py` compares every done record's stored bbox against
your measured envelopes — no reprocessing needed to find silent false
positives. Result: C1–C5 are clean (≤5%), **C10 is 80% suspect** — that
era's "done" grids are mostly tables. All 56 main-scope suspects + 816 old
grid-failures + 148 exposure failures reset and re-running now with the
full new stack.

## Still running when you read this (all monitored)

- **redo(main)**: 872 records (old failures + suspects) through the full
  current stack — ~7-9h total; resumes itself, three monitor layers:
  per-run monitors + META-monitor (process liveness × log freshness across
  all runs, alerts on dead runs even if a per-run monitor dies)
- **redo(exposure)**: 148 records, same treatment
- After both: `extract_insights.py` (era-clustered failed→done deltas,
  failure clustering, auto-recommendations) → enrichment → map push

## ⚠️ AWS discovery — action needed from you (2 minutes)

The mano account's REAL Fargate quotas are **6 vCPUs** (not 30 as the
support letter assumed — and the letter went to the wrong account; the
open case there is also for "Burst Launch Rate", not vCPUs). File these
two requests (I'm not permitted to submit account-level quota writes):

```
aws service-quotas request-service-quota-increase --service-code fargate --quota-code L-36FBB829 --desired-value 256 --profile mano   # Spot vCPUs
aws service-quotas request-service-quota-increase --service-code fargate --quota-code L-3032A538 --desired-value 64  --profile mano   # On-Demand vCPUs
```

## New tooling (all committed + pushed)

`audit_consistency.py` · `re_resolve_mismatches.py` · `sweep_suspect_dones.py`
· `extract_insights.py` · run-scoped `ProcessingStatus(stems_filter=)` ·
`grid_suspect` envelope guard · README.md

## GitHub
README live, About + homepage + 10 topics set, REDESIGN.md /
analyze_test100.py / run_test100.py removed, clutter purged.
Corrections Action active (first cron sweep 09:00 UTC daily).
Remember: add RDS_* repo secrets for the Action's coordinate re-resolution.


## Architecture insight discovered under load (for next session)

The status CSV's save path is O(file-size) per save and serialized by one
advisory lock: every month-batch save merge-rewrites the FULL 130MB /
514K-row file. With 2-3 processes saving, outside writers starve (30s lock
timeouts) and IO is dominated by rewrites. Today's scoped-LOAD fix solved
startup; the matching save-side enhancement is per-run sharded status files
(status.<runid>.csv) consolidated on completion — eliminates lock
contention entirely and makes saves O(run-size). Recommended before the
570K cloud run.


## ROOT CAUSE OF ALL SILENT DEATHS — FOUND (memory)

This machine has **7.4GB RAM**. Every "silent freeze/death" today and
historically (resume5's 4-hour freeze, the month-boundary deaths) is
memory exhaustion: 2 concurrent runs = 5+ python processes (CSV parses,
torch, OpenCV, PDF renders) exhaust commit charge and Windows kills them
without any log entry. Observed 1.1GB free during the last double-death;
single-run sessions (2 workers) historically survived 4+ hours.

**Operating rule now enforced: ONE pipeline run at a time locally**
(run_chain.bat sequences redo -> regen automatically; META v7 watches
free memory and chain completion). Cloud is unaffected — each Fargate
task gets its own 2GB.

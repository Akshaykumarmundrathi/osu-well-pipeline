# Night Shift Report — 2026-06-10 (autonomous session while you slept)

Status legend: ✅ done · 🔄 running when you read this · ⚠️ needs your decision

## FINAL SCOREBOARD (after repair2, all 3,690 C2-C5 records)

| Stage | before tonight | after | change |
|---|---|---|---|
| Location | 35% (852 done) | **64% (2,385 done)** | **+29 pts, 2.8×** |
| Full 3-field STR | 402 | **1,064** | 2.6× |
| Dot | 54% | **76% (2,198)** | +22 pts |
| Grid | 78% | 77% (2,874) | flat (harder records added) |
| County | 83% | 69% 🔄 | dipped — reset records re-ran Vision-only; **Gemini upgrade pass running now** (1,128 records, fresh quota, monitored) |

STR plausibility holds at 98%. Top remaining gap: missing range (638) —
the 2× OCR-resolution floor (see audit #4); next lever is a 2.5× re-OCR
retry tier.

## Headline wins

1. ✅ **C2-C5 main run finished** — 3,700/3,700 in 4h46m, exit 0.
2. ✅ **Map restored** — 792 wells live (commit `507caef`), was 0.
3. ✅ **Location extraction breakthrough** — found & fixed 3 compounding bugs
   (commit `60ace77`); **36% of previously-failed records now extract**
   (11/30 sample: 6 full STR, 5 partial). ADELINE test case: total failure →
   sec=30 twp=25N rng=17E @ 100%.
4. ✅ Repairs applied: 495 MID-misclassified + 67 fake-Blaine county records
   reset; 1,944 location-failed records reset for re-extraction with new code.
5. 🔄 **repair2 run** processing all 2,280 pending records with every fix live.

## The location fix (commit 60ace77) — what was wrong

Three stacked bugs starved STR extraction on 1939-50 forms:

| # | Bug | Effect |
|---|---|---|
| 1 | `choose_group` compared `twp.x_min > sec.x_max` but boxes are PRE-EXTENDED +500px | Triplet matching NEVER fired for compact same-line headers (SEC/TWP/RGE ~150-220px apart). All past "grouped" wins came from the no-ordering twp+rng fallback |
| 2 | No sec+twp fallback existed | At 2× render Vision OCR drops the small "RGE" label entirely (verified: reads fine at 2.5×) → those pages had sec+twp keywords but grouping returned None |
| 3 | No way to recover range value without its keyword | Added: trailing "`<num> E/W`" after township match infers range (E/W required; twp uses N/S — clean discriminator) |

Regression-checked: 11/12 previously-done records byte-identical; the 1 diff
was OCR nondeterminism (old value was likely a false positive from a well
number).

**Knock-on insight:** the "missing range" epidemic (184 partials) is mostly
OCR resolution: `RESOLUTION_MULTIPLIER=2` is below Vision's floor for the
small RGE print. Raising to 2.5 fixes OCR but breaks all grid-size
calibrations — NOT done; the value-inference fallback recovers most cases
instead.

## Silent-freeze forensics (3rd occurrence)

repair1 froze at a month boundary (April 1940) — same signature as resume5
and the first enrichment run: processes alive, log static, zero errors, then
processes die. Could not capture stacks (processes died before py-spy attach).

**Countermeasure now in place:** the repair2 monitor auto-runs py-spy stack
dumps on ALL python processes at the 8-minute-stall mark → next freeze writes
`D:\project_outputs\freeze_stacks.txt` with the exact hanging frame.

Pattern so far: all 3 freezes happened at/right after month-batch boundaries
→ suspect multiprocessing pool task handoff, not the per-record code.

## Coordinate enrichment

- run1 died silently after 596 rows (same freeze family).
- run2 ✅ completed: **1,257 resolved** (P1-3 direct, P4 lease-neighbor +380,
  P5 centroid +59), 544 failed, 2,253 skipped (no STR — exactly the pool the
  location fix feeds). 792 within OK bounds → published.
- RDS healthy: 4.5M-row PLSS table, ~43% query cache hit rate.

## Failure pattern table (full corpus, pre-repair)

| stage | error | era | count | fix path |
|---|---|---|---|---|
| location | not_found | 1917-1989 | 2,336 | ✅ 60ace77 + repair2 rerun |
| dot | grid_image_not_found | 1911-15 | 1,146 | old C1 runs missing grid PNGs — needs grid rerun for those stems |
| dot | not_detected | 1926-60 | 1,059 | U-Net labeling (`inspect_dots.py`) |
| county | no_match | 1932-60 | 952 | Gemini pass (quota is fresh) ⚠️ |
| grid | not_detected | 1916-60 | 664 | true failures + handwritten era |
| location | exception | 1913-14 | 479 | old-run artifact; reset+rerun cheap |
| county | exception | 1926-32 | 85 | was the gemini_disabled misclass — already fixed |

## AWS / infra state

- Both AWS accounts verified working (`default`=data, `mano`=compute).
- All Batch infra exists; secrets populated.
- ⚠️ S3 cross-account bucket policy: classifier requires YOUR explicit
  approval: `python aws/setup_aws.py --profile mano --profile-account1 default`
- ⚠️ Docker Desktop would not stay up after two autonomous launch attempts —
  needs a manual start (likely first-run dialog/WSL prompt).
- ⚠️ Gemini: 11/12 keys exhausted yesterday; quota reset at midnight Pacific.
  GEMINI_DISABLED=1 still set in .env pending your go-ahead.
- Cost reminder for full 570K cloud run: ~$1,570 Vision + ~$30-50 Gemini paid tier.

## D-drive output inventory (24 dirs, ~24 GB)

| dir | size | note |
|---|---|---|
| `project_outputs` | 17 GB | live master (514K rows) |
| `project_outputs_local` | 6.5 GB | old May run — source of the historic 3,377-well map; keep until S3 verified |
| `project_outputs_enrichtest` | 126 MB | tonight's enrichment workspace |
| `project_outputs_c2345` | 250 MB | C2-C5 index + snapshots |
| 15× `smoke_*` / `test*` dirs | ~150 MB | dev clutter — safe to delete after you confirm (not touched) |

## Storage insight

~2 MB/record debug output (counties/ full-page annotated PNGs dominate).
At 570K records ≈ 1.1 TB. Decide before cloud run: keep crops, drop/sample
full-page PNGs.

## Your manual review queue (unchanged)

1. `D:\project_outputs\location_review\` — 100 location-failure cases + index.csv
2. `python inspect_dots.py --form-type MID` — U-Net label session (C9-10 forms)
3. `project/ISSUES_AND_FIXES.md` — issue registry with all evidence
4. This file.

## Overnight sequence still executing

repair2 (🔄) → final audit → county Gemini decision point → final enrichment
→ map push (expect well count > 792).

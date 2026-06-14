# Pipeline Improvement Findings (Jun-13, from the 6,341 broad run)

Read-only analysis of the now-broad results (all 13 collections have real
stage outcomes for the first time). Goal: find format/era patterns to target.
**Nothing here was auto-applied to the live map** — all recommendations are
additive and listed for review (improvement-only, no negative impact).

## 1. Per-collection stage success (attempted records)

| coll | era | grid | location | county | dot | headline problem |
|--|--|--|--|--|--|--|
| C1 | 1911-15 | 92% | 41% | 89% | 59% | location weak |
| C2 | 1926-40 | 88% | 48% | 84% | 38% | **dot (hollow circles, P11)** |
| C3 | 1941-50 | 98% | 44% | 86% | 63% | location weak |
| C4 | 1950s | 99% | 73% | 84% | 78% | ok |
| C5 | 1958+ | 98% | 74% | 81% | 86% | ok |
| C6 | 1960s | 99% | 83% | 80% | 93% | good |
| C7 | 1970s | 99% | 77% | 90% | 91% | good |
| C8 | 1980s | 99% | 62% | 91% | 85% | ok |
| C9 | 1980s | 93% | 66% | 93% | 77% | ok |
| C10 | 1988 | 96% | 91% | 86% | **48%** | dot drop |
| C11 | 2012 | 83% | 96% | 75% | **48%** | half-digital |
| C12 | 2014+ | **48%** | 70% | **36%** | **28%** | **digital-native, no grid** |
| C13 | 2022+ | **7%** | **12%** | **34%** | **3%** | **digital-native, no grid** |

## 2. Root cause — C11-C13 are a different FORMAT, not a failure

C12-C13 are **digital-native OCC completion reports** (typed PDFs), not the
hand-drawn grid forms the pipeline was built for. There is often **no grid and
no ink dot** to find, so grid/dot legitimately score ~0. The location data is
in TYPED TEXT instead (well name, API number, sometimes a "Location: county S
T R quadrant" block). Treating these as grid forms is the mismatch. They need a
**text-field path**, not grid/dot.

## 3. Validated lever — API-number → county (deterministic)

Oklahoma API numbers encode the county: `county = sorted_counties[(code-1)//2]`
(verified: 35-003→Alfalfa, 35-011→Blaine). Measured agreement vs Vision-verified
county, decoding the **leading stem digits**:

| collections | agreement | use? |
|--|--|--|
| C1-C6, C10, C11-C13 | **84-94%** | YES — stems start with the API county code |
| C7-C9 | 35-61% | NO — these stems use a different ID (OTC#), would corrupt |

So it's a **collection-gated** lever, not global. Best target: C11-C13 county
(currently 34-75%) where the grid path can't help anyway.

## 4. Recommended ADDITIVE fixes (review before enabling)

All are blank-fill + flagged (never override a confident extraction, never gate):

1. **Modern-format text path for C11-C13** — when grid/dot find nothing, parse
   county (API code, gated to C1-C6/C10-C13), STR (well-name encoding like
   `23-20N-1W`, or a "Location:" block when present), quadrant. Tag
   `resolution_source=modern_text` + `needs_review`. Net new coverage on the
   ~1,000 currently-failing modern wells; zero effect on grid-form collections.
2. **C2/C10 dot** — both are dot-detector misses on present grids (C2 = hollow
   circles P11; C10 = 1988 mark style). Same fix path: U-Net round-2 labels
   (user's review console already targets these).
3. **C1/C3 location (41-44%)** — early header-STR variants still unmatched;
   inspect via review_notes once the user annotates, then add patterns.

## 5. Status of the ecosystem (unchanged, healthy)
Map 7,163 wells; index/status/coords/site/S3 all coherent & deduped; monotonic
+ safe_merge guards protect existing data. None of the above has been applied —
awaiting review so improvements stay strictly non-destructive.

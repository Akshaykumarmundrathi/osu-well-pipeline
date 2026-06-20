# PLSS RDS — Inference Patterns (read-only exploration, Jun-16)

Read-only exploration of `plss_grid` (4,565,504 quarter-quarter cells; 77
counties; 65,031 sections). No writes/overwrites.

## Key inference levers
1. **Direction is county-determined for ~77% of counties** (ground truth):
   - **E/W single-valued: 59/77** counties · **N/S single-valued: 60/77**.
   - So a record that extracted township/range NUMBERS but lost the N/S or E/W
     suffix (a real failure mode) can have the suffix filled deterministically
     from its county. → `county_directions.csv` (county, ns, ns_deterministic,
     ew, ew_deterministic, centroid_lat/lon).
2. **County centroid = a county-only fallback coordinate.** Records with only a
   county (no usable STR) can be placed at the county centroid (coarse, flag
   `county_centroid`), better than dropping them. Centroids in the same CSV.
3. **Section is NOT inferable** — every township carries sections 1–36 uniformly;
   the section number must be read from the form (confirms the legacy ceiling).
4. **T/R is broad per county** (~hundreds of T/R combos each), so county does not
   narrow township/range enough to infer them — only the *direction* suffix.

## OK-wide reference
- Township range 1–29; Range 1–27 typical (a few artifact values up to 99).
- Cells: N/S ≈ 84% North / 16% South · E/W ≈ 57% East / 43% West.

## Data-hygiene notes (RDS)
- One malformed county entry (`'9'`/blank, ~4,480 cells) — exclude in joins.
- A handful of out-of-range `range` values (>27) appear to be artifacts.

## How to use (free, additive)
Add a direction-backfill step before RDS resolution: when twp/rng numbers exist
but a suffix is missing, look up `county_directions.csv` and apply the
deterministic suffix. Lifts partial-STR records to resolvable without new OCR.

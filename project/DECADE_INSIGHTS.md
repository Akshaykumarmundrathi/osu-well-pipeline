# Pattern Analysis & Inference Insights (Jun-16)

Analysis of the processed records to find patterns that can complete the
unfinished ones. Read-only; map left at 49,760 wells.

## What does NOT help (ruled out)
- **County clustering by batch (month/operator):** median top-county share only
  **25%**; **0 of 221** batches are dominated by one county. 1002A filings are
  geographically mixed within a month → **cannot infer county from neighbors.**

## What DOES help (already used / minor)
- **Township N/S:** 89% North (Oklahoma base line) → default "N" is ~89% safe.
- **Range E/W:** 46% E / 54% W; **29 of 66 counties are ≥95% one direction** →
  E/W is county-determined for ~half the counties (county-pinned E/W already
  implemented). The other half straddle the Indian Meridian.

## The real bottleneck — and the free fix
- Of location-done records, only **44% have a complete Section+Township+Range**.
  **Section is the missing field** (range/township parse ~80-85%, section ~58%
  overall, ~20% on C11 grid forms).
- The "spot well" grid gives position *within* a section, not the section number
  → section is **not** inferable from grid/county/direction patterns. It must be
  read from the form.
- **Root cause = parser, not data:** `_SEC_RE` requires the literal word
  "SEC/SECTION" before the number. Township parses off its N/S letter and range
  off its E/W letter, so when OCR drops/splits the "SEC" label, township+range
  survive but the **section is lost**. The number IS on the form and IS in the
  (cached) OCR.
- **Fix (free):** add a section fallback that reads the 1-2 digit number
  positioned immediately before the township in the STR string
  (`14 18N 13E`, `14-18N-13E`, `NW/4 14-18N-13E` → section 14). Then **re-run the
  location stage on missing-section records — the Vision OCR is cached (95k
  entries), so recovery costs $0** (no new API charges).

## Expected impact
Recovering section on the ~56% of location-done records that have township+range
but no section would lift many partial records to full STR → resolvable to
coordinates via the existing RDS path, at no Vision cost. Best applied as a
free re-extraction pass over already-OCR'd records before spending on new ones.

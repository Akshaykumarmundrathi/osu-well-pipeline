# Assumption Audit — every restriction questioned (2026-06-10)

Premise (user directive): structured anchors, zones, thresholds and filters
were added to *help* extraction, but each one is also a way to be confidently
wrong. This audit lists every confinement, what breaks when it lies, and the
verdict from measured evidence. Status: 🔴 proven harmful (fixed) ·
🟡 suspect (test queued) · 🟢 earns its keep.

## 1. Geometry-derived form classification — 🔴 FIXED
`classify_form_type()` trusted the detected grid bbox. The casing-table
false positive made 495 early-tier records "MID", poisoning every downstream
hint. **Lesson: a classifier must not consume the output of the thing it
gates.** Fix: tier guard + width cap (2a01b36). Long-term: classify from
page TEXT (form id / title / keyword geometry) — see survey below.

## 2. STR zone filters — 🔴 FIXED (fallback added)
`str_zone` pre-filters OCR tokens to a page region. Measured: un-hinted
records succeeded at 67% vs 39-44% for hinted ones. When the hint is wrong
the stage CANNOT recover — the data is filtered out before any strategy runs.
Fix: full-page retry when zone-confined strategies find <2 fields
(location_extractor.py). Same pattern should be considered for county's
format hints (county_format_hint reorders strategies — less dangerous since
all three anchors are still tried).

## 3. choose_group ordering vs pre-extended boxes — 🔴 FIXED
Triplet matching compared against x_max that already included +500px,
silently disabling the primary strategy for compact headers (60ace77).
**Lesson: units/conventions must be explicit at function boundaries** — the
boxes' "already extended" property was documented only in a docstring two
files away.

## 4. RESOLUTION_MULTIPLIER = 2 — 🟡 systemic ceiling
Proven: Vision drops the small "RGE" label at 2× but reads it at 2.5×.
Every OCR consumer inherits this floor. Raising it globally breaks all
pixel-calibrated grid filters (W/H/AR/density windows, W_MAX=565 cap,
anchor crop params). Options, cheapest first:
  a) value-inference fallbacks (done for range — 60ace77)
  b) re-OCR at 2.5× ONLY for pages that failed location (1 extra Vision
     call per failure; ~$0.30/1000 failures)
  c) decouple: render OCR at 2.5×, grid CV at 2× (needs scale mapping)
Recommendation: (b) as a retry tier before Gemini.

## 5. Exact-token keyword matching — 🟡 test queued
`find_keywords_lists` requires exact match after punctuation strip.
Protects against "secondary"/"arrangement" false hits, but misses GLUED
OCR tokens: "Sec.18" normalises to "sec18" ≠ "sec". Hypothesis: a regex
`^(sec|twp|rge)\W*\d{1,3}[nsew]?$` token class would recover label+value
fusions with near-zero false-positive risk (digit suffix required).
Test against survey OCR dumps before changing.

## 6. ≥2-of-3 field acceptance — 🟡 information thrown away
Records with ONLY a section (or only twp) are recorded as total failures —
the partial value isn't even stored. Cost: coord Pass-5 centroid needs all
three; but a sec-only record + county could still narrow to ~36 candidate
sections. Cheap win: store 1-field partials in the CSV (no acceptance
change) so enrichment/review can use them.

## 7. ILLEGIBLE_WORD_THRESHOLD = 15 — 🟢 reasonable
<15 tokens on a page ⇒ skip. A page with the STR but fewer than 15 total
tokens is implausible (the boilerplate alone exceeds that). Keep.

## 8. Grid W/H/AR/density windows + tier caps — 🟢 with caveat
The new W_MAX=565 cap is data-derived (1,000 dot-verified grids). Caveat:
all of these are calibrated to 2× render — any resolution change must
re-derive them (note added to config).

## 9. County RETRY_CONFIDENCE_THRESHOLD = 95 / FUZZY 72 — 🟢 after fix
The 'N'→Blaine incident was a scorer artifact (partial-match on 1-2 chars),
not a threshold problem. Length guard fixed it. 90-score anchors are
accepted only after Gemini declines — correct order.

## 10. Pages ≤ 2 assumption / PAGE_TO_PROCESS=0 — 🟡 verify on C10+
Survey records pages per PDF; modern collections may exceed 2 pages with
the grid not on page 1. The smoke-test C13 20% grid rate may partly be
page-coverage, not detection.

## 11. Anchor phrases as grid locator — 🟢 but never terminal
anchor_above/below methods dominate successes (62% of detections).
Full-page CV fallback already exists when the anchor fails. Healthy
pattern: hint first, fall back to open search — now replicated for
location zones (#2).

---

# Format-router design (toward per-format pipelines)

Today: ONE pipeline, with per-form-type *hints* (zones, strategy order,
county format) derived from grid geometry. The audit shows the hints are
valuable but the CLASSIFIER is the weak link.

Proposed: classify from page text FIRST (form id, title, boilerplate,
keyword constellation) — `survey_form_fingerprints.py` is measuring whether
printed form ids ("Form 1002", "Form 1003", OCC numbers) are present and
era-stable enough to be the primary router. If they are:

```
page OCR (already done for every stage)
   └─ FormRouter.classify(text, kw_positions)
        ├─ known form id  → recipe table: {str_zone, str_strategy,
        │                     county_format, grid_zone prior, dot model tier}
        ├─ no id but kw constellation matches a known cluster → same
        └─ unknown → open hints (STR_ANY) + full-page strategies
```

Recipes are DATA (a dict/CSV), not code branches — adding a new form type
= adding a row, not a new pipeline. Switching logic stays in one place and
every recipe inherits the universal rule from this audit: **hints reorder
and bound the search; they never terminate it.**

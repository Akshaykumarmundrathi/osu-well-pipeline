# Map & Data Insights (Jun-16) — 49,760 mapped wells

Deep analysis of all mapped + processed + failed data.

## 1. Map composition — what's actually on the map
- **49,760 wells, all 77 Oklahoma counties represented.**
- **86% are modern (C11–C13 = 42,799)**, mapped FREE via the text path. The
  older grid collections (C1–C10) contribute only ~6,961.
- **Precision is mostly coarse:** 80% (40,090) are `section_centroid`
  (~½-mile accuracy — the well's section, not its exact spot). Only ~15%
  (`quadrant_direct` + `p4_quadrant_direct` = 7,170) are cell-precise from the
  grid/dot pipeline. **Coverage is broad; precision is the trade-off.**
- **Decade skew:** 2010s = 31,265 (63%), 2000s = 6,951, 2020s = 4,448, 1910s =
  3,261. **Mid-century (1960s–1990s) is very sparse** (<1,500 total).
- **Top counties:** Carter, Kingfisher, Creek, Stephens, Canadian, Grady, Woods,
  Alfalfa — the Anadarko-Basin / active-play counties (fits the DOE grant focus).

## 2. The big coverage gap (where the wells are)
- **497,440 records attempted; only 49,760 mapped.** The gap is NOT hard
  failures — those are small (grid 16–70/coll, location 150–380/coll). The gap
  is **incomplete extraction**: records processed but lacking a full
  Section+Township+Range (the **section number** is the missing field).
- **Mid-era grid collections (C6–C10) are the gap:** ~50,000 attempted each but
  only 43–367 mapped. These are scanned grid forms where the section often isn't
  captured. This — not failures — is the ceiling on the map.

## 3. Failures are not the bottleneck — incompleteness is
Per-stage hard failures (whole corpus): grid ~165, location ~3.5k, county ~5.7k,
dot ~3.7k. Small versus 497k attempted. The real loss is the ~447k attempted
records that produced partial data and never reached a mappable coordinate.

## 4. Free-recovery ceiling
The Vision OCR disk cache holds ~95k image results. Only records whose pages are
already cached can be re-processed at **zero API cost**; the rest would re-charge
Vision. So free recovery is bounded to the cached subset (the section-parser
re-run recovers ~13% of its missing-section candidates).

## 5. Actionable conclusions
1. **Biggest free lever already pulled:** the modern text path (C11–C13) — done.
2. **Section number is the single ceiling** on older grid forms. It is mostly
   genuinely uncaptured (not just a parser gap), so lifting C6–C10 needs better
   OCR/extraction of the section field — best done in the cloud run, not on the
   laptop.
3. **Precision upgrade path:** the 40k section-centroid wells could be sharpened
   to cell-level if the grid-dot (U-Net) stage runs on them — a quality, not
   coverage, improvement.
4. **The map is already representative** (all 77 counties, basin-focused) and
   strongest exactly where the DOE project cares (modern + Anadarko counties).

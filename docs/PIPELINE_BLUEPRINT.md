# Oklahoma Well Records Pipeline — Complete Blueprint

> Single source of truth for how a PDF flows through the system,
> what every output looks like, how stages coordinate, and how the
> four-pass execution strategy works end-to-end on AWS Batch.

---

## Table of Contents
1. [Architecture Overview](#1-architecture-overview)
2. [PDF Source & Rendering](#2-pdf-source--rendering)
3. [Stage-by-Stage Flow](#3-stage-by-stage-flow)
4. [Output Directory & File Schemas](#4-output-directory--file-schemas)
5. [Tier System & Stage Gates](#5-tier-system--stage-gates)
6. [Thresholds & Review Flags](#6-thresholds--review-flags)
7. [Logging](#7-logging)
8. [U-Net in AWS Batch](#8-u-net-in-aws-batch)
9. [Four-Pass Execution Strategy](#9-four-pass-execution-strategy)
10. [Coordination Map (function → output)](#10-coordination-map)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INPUT SOURCES                               │
│  Local:  ExportedFolderContents (N).zip  →  utils/zip_reader.py    │
│  Batch:  s3://osu-well-records-*/...pdf  →  utils/s3_reader.py     │
└────────────────────────────┬────────────────────────────────────────┘
                             │ raw PDF bytes
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│   scan_dataset.py  →  dataset_index.csv  (one row per PDF)         │
│   main.py groups records by (collection, year, month)              │
└────────────────────────────┬────────────────────────────────────────┘
                             │ DatasetRecord
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│              PDFDocumentManager  (pdf/pdf_manager.py)               │
│  • Opens PDF from bytes or file path                                │
│  • Renders pages to PIL images @ 2× resolution (300 DPI effective) │
│  • Caches: rendered PIL pages, Vision API annotations per page      │
│  • Shared across ALL stages for one record — no re-opening         │
└────────────────────────────┬────────────────────────────────────────┘
                             │ shared manager
          ┌──────────────────┼──────────────────────┐
          ▼                  ▼                       ▼
    ┌──────────┐      ┌────────────┐         ┌──────────────┐
    │ STAGE 1  │      │ STAGE 2    │         │  STAGE 3     │
    │ Lat/Lon  │      │   Grid     │         │  Location    │
    │ latlong/ │      │  grid/     │         │  location/   │
    └────┬─────┘      └─────┬──────┘         └──────┬───────┘
         │                  │                        │
         │            ┌─────▼──────┐                 │
         │            │ STAGE 5    │                 │
         │            │   Dot      │                 │
         │            │  dot/      │                 │
         │            └─────┬──────┘                 │
         │                  │                        │
         ▼         ┌────────▼───────────────────────▼────┐
    ┌──────────┐   │              STAGE 4                │
    │ Direct   │   │              County                  │
    │ lat/lon  │   │           county/                    │
    │ output   │   └─────────────────────────────────────┘
    └────┬─────┘                   │
         │                         │
         └──────────┬──────────────┘
                    ▼
     processing_status.csv  (44 cols, one row per PDF, updated after every stage)
     metadata/{stem}/metadata.json
     logs/{stem}.log

                    ▼  (after all records done)
     run_coord_enrichment.py  →  dot_coordinates.csv
                    ▼
     build_map_data.py  →  docs/data/well_locations.json  →  GitHub Pages
```

**Stage skip rules:**
- `latlong` skipped when the collection's measured prior is < 5% (C1-C11;
  only C12-C13 print lat/lon often enough to be worth the OCR)
- `grid` + `location` skipped ONLY when lat/lon was actually FOUND (this run
  or prior) — a "latlong-format" collection does NOT guarantee lat/lon is
  present; files without it proceed through grid/STR/county normally
  (C12 multi-page variants route to page 3 via PAGE_HINTS)
- `location` is attempted for ALL tiers (run_location=True everywhere).
  A per-page illegibility guard (≥15 OCR tokens) skips truly blank or
  unreadable PAGES — never a blanket tier skip. (The old early-tier skip
  silently lost 698 records and was removed.)
- `dot` skipped when grid was not detected; if the grid PNG was deleted
  but bbox is stored, the crop is regenerated on the fly

---

## 2. PDF Source & Rendering

### `scan_dataset.py` → `dataset_index.csv`

Scans source ZIPs or flat folders and writes one row per PDF.

| Field | Type | Example |
|---|---|---|
| `pdf_stem` | str | `35037119060000_JACOB SPOCOGEE 10_1982466` |
| `pdf_path` | str | `D:\ExportedFolderContents (1)\1911\01 - January\...pdf` |
| `zip_path` | str | `D:\ExportedFolderContents (1).zip` (empty if --flat) |
| `internal_path` | str | `1911/01 - January/name.pdf` (path inside ZIP) |
| `collection` | str | `ExportedFolderContents (1)` |
| `collection_num` | int | `1` |
| `year` | str | `1911` |
| `month` | str | `01 - January` |
| `file_size_bytes` | int | `47382` |
| `scan_timestamp` | str | `2026-05-28T14:32:01` |

### `PDFDocumentManager` (`pdf/pdf_manager.py`)

```python
manager = PDFDocumentManager(pdf_bytes_or_path, resolution_multiplier=2)

# Iterates PIL images at 2× (≈300 DPI for typical 150 DPI scans)
for page_num, pil_image in manager.iter_pil_pages():
    ...

# OCR cache — Vision API called once per page, result reused by every stage
manager._ocr_cache[page_num]   # list[AnnotateImageResponse]

page_count = manager.page_count()   # int, usually 1 or 2
```

**Rendering:** `RESOLUTION_MULTIPLIER = 2` (config). Each A4 page renders to
~1700×2200 px. Increase to 3 for higher OCR accuracy (slower + more memory).

---

## 3. Stage-by-Stage Flow

### Stage 1 — Lat/Lon (`latlong/latlong_extractor.py`)

**When run:** Late and Modern tiers only (collections 11+). Early/transition skip.

**Input:** `PDFDocumentManager`, scans up to `MAX_LATLONG_PAGES=2` pages.

**Algorithm:**
```
Page OCR (Cloud Vision API, cached)
    │
    ├─► Labeled decimal search    "33.12345°N 96.54321°W"
    ├─► DMS pattern search        "33° 7' 24" N  96° 32' 35" W"
    ├─► Unlabeled decimal search  two adjacent float tokens (heuristic)
    └─► Form header block         lat/lon in printed Form 1002A header
```

**Return dict:**
```python
{
  "detected":    True,
  "lat":         "35.4521",   # decimal degrees, string
  "lon":         "-96.3214",
  "page":        0,
  "confidence":  95,          # 0-100; <80 → review flag
  "error":       None,
}
```

**Fallback chain:**  
If not found → `max_pages` expands to 99 on retry. If still not found → stage
marked `failed` with `error_type=not_found`.

---

### Stage 2 — Grid (`grid/scoring.py`)

**When run:** Always (all tiers), UNLESS lat/lon was found this run.

**Input:** `PDFDocumentManager`, pages scanned **FORWARD (page 1 first)** — the grid
sits on the FIRST page for the vast majority of forms across all 13 collections.
(Reverse-order scanning was removed: it produced false-positive detections on
back-page tables before reaching the real grid.) Exception: known multi-page
sub-formats (recipes.PAGE_HINTS — e.g. C12 files ≥4 pages carry grid/county/STR
on page 3) get their data page tried first.

**Algorithm (6 CV methods tried in order):**
```
Page image (PIL @ 2×)
    │
    ├─► 1. Structural anchor (grid/anchors.py)
    │       Vision API OCR → search for "spot well located" / "Locate Well"
    │       → crop fixed region above/below anchor → run extractors on crop
    │
    └─► 2. Full-page CV (if anchor fails)
            ├─ extract_grid_region_adaptive  (adaptive threshold)
            ├─ extract_grid_region_otsu      (Otsu binarisation)
            ├─ extract_grid_region_canny     (Canny edge detector)
            ├─ extract_grid_region_hough     (Hough line transform)
            ├─ extract_grid_region_corners   (Harris corner detection)
            └─ extract_grid_region_rotated   (rotated/skewed grids)
```

**Size filter (first pass):**
```
GRID_W_STRICT = (280, 850) px   GRID_H_STRICT = (280, 850) px
```
**Relaxed retry:**
```
GRID_W_LOOSE  = (150, 1200) px  GRID_H_LOOSE  = (150, 1200) px
```

**Return dict:**
```python
{
  "detected":    True,
  "page":        1,           # 0-indexed page where grid was found
  "bbox":        (x0,y0,x1,y1),
  "method":      "otsu",      # which CV extractor succeeded
  "confidence":  85,          # 0-100; <80 → review flag
  "image_path":  "grids/.../stem_page_01_grid.png",
  "error":       None,
}
```

**Output file:** `grids/{collection}/{year}/{month}/{stem}/{stem}_page_NN_grid.png`
— a square-cropped, perspective-corrected 8×8 grid image.

---

### Stage 3 — Location / STR (`location/location_extractor.py`)

**When run:** ALL tiers. Per-page illegibility guard (ILLEGIBLE_WORD_THRESHOLD=15
OCR tokens) skips unreadable pages individually; printed SEC/TWP/RGE labels on
early forms extract fine even when the VALUES are handwritten.

**Input:** `PDFDocumentManager`, all pages.

**Algorithm:**
```
Page OCR (Cloud Vision API, cached)
    │
    ├─► illegibility guard: < 15 word tokens → skip page
    │
    ├─► Strategy 1: Grouped keyword extraction (location/grouping.py)
    │       find_keywords_lists() → boxes for "section", "township", "range"
    │       choose_group() → vertical-overlap pairing (min_overlap=0.35)
    │       get_unified_bounding_box() → crop region
    │       _extract_str(raw_text) → (sec, twp, rng)
    │           ├─ _SEC_RE: "sec(tion)? N"
    │           ├─ _TWP_RE: "t(ownship|wn|vp|wp) N[NS]"
    │           ├─ _RNG_RE: "r(ange|ge) N[EW]"
    │           └─ _SEC_OF_RE: "SECTION SW/4 of N" (1911 Form B fallback)
    │
    └─► Strategy 2: Per-keyword fallback (if < 2 fields found)
            For each keyword box: extend right 500px, capture tokens,
            pick first plausible number.

    Accept when ≥ 2 of (section, township, range) populated.
    Confidence = (fields_found × 100) / 3  → 33 / 66 / 100
```

**Return dict:**
```python
{
  "detected":        True,
  "section":         "19",
  "township":        "18",
  "range":           "12",
  "page":            0,
  "confidence":      100,     # <100 (partial STR) → review flag
  "quadrant_pdf":    "NW-SW", # OCR-extracted quadrant label
  "quadrant_db":     "NW SW", # DB-normalised form
  "image_path":      "locations/.../stem_page_01_location_crop.png",
  "annotated_path":  "locations/.../stem_page_01_location_page.png",
  "raw_text":        "Section 19 Township 18 Range 12 ...",
  "error":           None,
}
```

**Retry:** Loose `min_overlap=0.15` on second attempt.

**Output files:**
- `locations/.../stem_page_NN_location_crop.png` — tight crop around STR label
- `locations/.../stem_page_NN_location_page.png` — full page with blue bounding box

---

### Stage 4 — County (`county/county_extractor.py`)

**When run:** All tiers.

**Input:** `PDFDocumentManager`, up to `MAX_COUNTY_PAGES=2` pages.

**Algorithm (3-pass, zero Gemini calls on typical records):**
```
Page OCR (Vision API, cached)
    │
    ├─► Pass 0: OCR direct-anchor (FREE — no API call)
    │       find_keyword_box("county") → collect tokens LEFT of box
    │       fuzzy_match(text, COUNTY_LIST_CLEAN, threshold=72)
    │       If score ≥ 95 → ACCEPT immediately
    │       If 72 ≤ score < 95 → TENTATIVE (proceed to Pass 1 to confirm)
    │
    ├─► Pass 1: Gemini Flash on keyword crop (cheap)
    │       Crop region around "County" keyword
    │       Prompt: "Find county base name. Valid: [77 names]. Reply ONLY the name."
    │       Rate limit: 3s between calls (GEMINI_MIN_CALL_GAP_S)
    │       Fuzzy-match response → score ≥ 95 → ACCEPT
    │
    └─► Pass 2: Gemini Pro on same crop (only if Pass 1 < threshold)
            Prompt: "Identify county name. Valid: [full names list]."
            Fuzzy-match → score ≥ 72 → ACCEPT

Fallback: county_score < 86 → needs_review flag in processing_status.csv
```

**Rate limiting (county/prompts.py):**
```
_CALL_GAP = 3.0s   (GEMINI_MIN_CALL_GAP_S env var)
On 429:  backoff = 30s × 2^(attempt-1), max 300s, jitter ±20%, 6 retries
Key rotation: GOOGLE_API_KEY=key1,key2,key3 → auto-rotate on exhaustion
```

**Return dict:**
```python
{
  "detected":     True,
  "name":         "Creek County",
  "pass1_result": "creek",      # Flash model response
  "pass2_result": "",           # Pro model response (if invoked)
  "fuzzy_score":  98,           # 0-100; <86 → review flag
  "confidence":   98,
  "page":         0,
  "image_path":   "counties/.../stem_page_01_county_crop.png",
  "annotated_path":"counties/.../stem_page_01_county_page.png",
  "error":        None,
}
```

**Output files:**
- `counties/.../stem_page_NN_county_crop.png`
- `counties/.../stem_page_NN_county_page.png` (full page, green bounding box)

---

### Stage 5 — Dot / U-Net (`dot/dot_extractor.py`)

**When run:** Only when grid was detected (has a saved grid PNG).

**Input:** Grid PNG image written by Stage 2.

**Algorithm:**
```
grid_dir/stem_page_NN_grid.png  (512×512 px, 8×8 sections)
    │
    ├─► _get_model()  (lazy singleton — loads once per process)
    │       torch.load(unet_best.pth, map_location=cpu)
    │       UNet(in_channels=1, out_channels=1, features=[16,32,64,128])
    │       model.eval()
    │
    └─► DotDetector.predict_image(grid_path)
            Resize to model input (512×512)
            Forward pass → probability heatmap
            Threshold per tier:
                early=0.55, transition=0.52, mid=0.50, late=0.47, modern=0.45
            Find blobs above threshold (min_area=8 px²)
            max_dots=1 → keep highest-confidence blob
            Map pixel position → (row, col) in 8×8 grid (1-indexed)
            Compute (x_norm, y_norm) = fraction within cell [0.0–1.0]
            Determine NW quadrant label from (row, col)
```

**Return dict:**
```python
{
  "detected":  True,
  "row":       3,           # 1–8
  "col":       5,           # 1–8
  "nw":        "NW-NE-SW",  # NW quadrant label from grid position
  "x_norm":    0.62,        # fractional position within cell
  "y_norm":    0.41,
  "confidence": 88,         # <70 → review flag
  "image_path": "dots/.../stem_dot_overlay.png",
  "error":      None,
}
```

**Checkpoint fallback chain:**
```
UNET_CHECKPOINT env var
    → /app/unet_best.pth          (Docker: /app is working dir)
    → /app/../unet_best.pth       (repo root in container)
    → D:\project_modular\unet_best.pth  (local dev Windows)
→ FileNotFoundError with clear message if all fail
```

---

### Post-pipeline — Coordinate Enrichment (`run_coord_enrichment.py`)

**Reads:** `success.csv` (or `dot_locations.csv`)
**Queries:** RDS PostgreSQL PLSS database (Oklahoma grid database)
**Writes:** `dot_coordinates.csv`

**10-strategy resolution order (`coord/plss_resolver.py`):**
```
1. quadrant_direct    — cell bbox from plss_grid for specific quadrant label
2. exact_county       — section+twp+NS+rng+EW + county ILIKE
3. exact_no_county    — same without county
4. county_constrained — NS/EW inferred from county geographic constraints
5. ns_fallback        — try N then S when north_south direction missing
6. ew_fallback        — try W then E when east_west direction missing
7. ns_ew_fallback     — both directions tried
8. county_stripped    — strip "County" suffix / try first word
9. section_adjacent   — try section ±1 (OCR off-by-one recovery)
10. section_centroid  — fall back to section centre polygon
```

**Return dot_coordinates.csv columns:**
```
pdf_stem, collection, year, month, well_name,
section, township, range, county_name,
dot_row, dot_col, dot_nw,
lat, lon,
resolution   (strategy code from above)
```

---

## 4. Output Directory & File Schemas

```
$OUTPUT_ROOT/
├── dataset_index.csv             ← scan_dataset.py output
├── processing_status.csv         ← 44-col master tracker (see below)
├── success.csv                   ← all-stages-done records
├── review.csv                    ← records needing human check
├── dot_locations.csv             ← dot + STR for RDS enrichment
├── latlong_records.csv           ← records with direct lat/lon
├── dot_coordinates.csv           ← final (lat, lon) after RDS
├── failure_analysis.csv          ← breakdown by stage × error_type × tier
├── run_insights.json             ← per-run timing + count summary
├── quota_events.json             ← Gemini API key rotation log
│
├── grids/{collection}/{year}/{month}/{stem}/
│   └── {stem}_page_NN_grid.png       512×512 px perspective-corrected
│
├── locations/{collection}/{year}/{month}/{stem}/
│   ├── {stem}_page_NN_location_crop.png   tight STR crop
│   └── {stem}_page_NN_location_page.png  full page + blue bbox
│
├── counties/{collection}/{year}/{month}/{stem}/
│   ├── {stem}_page_NN_county_crop.png
│   └── {stem}_page_NN_county_page.png    full page + green bbox
│
├── dots/{collection}/{year}/{month}/{stem}/
│   └── {stem}_dot_overlay.png           grid with predicted dot marked
│
├── metadata/{collection}/{year}/{month}/{stem}/
│   └── metadata.json                    all extracted fields + stage status
│
├── logs/{collection}/{year}/{month}/{stem}/
│   └── {stem}.log                       DEBUG-level per-PDF log
│
└── manual_review/
    ├── failed_records.csv
    └── review_queue.csv                 low-confidence records for inspection
```

### `processing_status.csv` — 44 columns

```
IDENTITY (8):
  pdf_stem, pdf_path, zip_path, collection, collection_num, year, month, model_tier

LATLONG STAGE (6):
  latlong_status, latlong_confidence, latlong_error_type,
  latlong_lat, latlong_lon, latlong_page

GRID STAGE (6):
  grid_status, grid_confidence, grid_error_type,
  grid_page, grid_method, grid_image_path

LOCATION STAGE (8):
  location_status, location_confidence, location_error_type,
  location_section, location_township, location_range,
  location_quadrant_pdf, location_quadrant_db

COUNTY STAGE (5):
  county_status, county_confidence, county_error_type,
  county_name, county_score

DOT STAGE (8):
  dot_status, dot_confidence, dot_error_type,
  dot_row, dot_col, dot_nw, dot_x_norm, dot_y_norm

COORD AUDIT (2):
  coord_derivation, coord_latlong_source

HOUSEKEEPING (1):
  last_updated

Status values: pending | done | failed | skipped
```

---

## 5. Tier System & Stage Gates

| Tier | Collections | Era | run_latlong | run_location | STR strategy |
|---|---|---|---|---|---|
| `early` | 1–6 | ~1911–1940s | ❌ | ❌ | str_keywords |
| `transition` | 7–8 | ~1950s | ❌ | ❌ | str_keywords |
| `mid` | 9–10 | ~1960s–70s | ❌ | ✅ | location_keyword |
| `late` | 11–12 | ~1980s–90s | ✅ | ✅ | location_keyword |
| `modern` | 13+ | ~2000–2024 | ✅ | ✅ | location_keyword |

**Decision flow for each record:**
```
tier = tier_for(collection_num)

if tier in (early, transition):
    latlong   → SKIPPED
    location  → SKIPPED
    grid      → RUN  (grid + U-Net dot are the primary data signals)
    county    → RUN
    dot       → RUN  (if grid found)

if tier == mid:
    latlong   → SKIPPED
    grid      → RUN
    location  → RUN  (STR via "Location:" keyword)
    county    → RUN
    dot       → RUN  (if grid found)

if tier in (late, modern):
    latlong   → RUN  (may find decimal coordinates)
    if lat/lon found:
        grid      → SKIPPED
        location  → SKIPPED
    else:
        grid      → RUN
        location  → RUN
    county    → RUN
    dot       → RUN  (if grid found)
```

---

## 6. Thresholds & Review Flags

Every stage result below its threshold is written to `review_queue.csv`
and marks `needs_review=True` in `processing_status.csv`.

| Stage | Threshold | Review trigger |
|---|---|---|
| `latlong` | `LATLONG_REVIEW_BELOW = 80` | confidence < 80 |
| `grid` | `GRID_REVIEW_BELOW = 80` | confidence < 80 |
| `location` | `LOCATION_REVIEW_BELOW = 100` | any of sec/twp/rng missing |
| `county` | `COUNTY_REVIEW_BELOW = 86` | fuzzy_score < 86 |
| `dot` | `DOT_REVIEW_BELOW = 70` | confidence < 70 |
| County OCR | `FUZZY_MATCH_THRESHOLD = 72` | minimum to record at all |
| County auto-accept | `RETRY_CONFIDENCE_THRESHOLD = 95` | auto-accept, skip Pass 2 |
| Illegibility | `ILLEGIBLE_WORD_THRESHOLD = 15` | skip OCR on page |

**Retry expansion:**
```
Grid:     strict (280–850 px)  →  relaxed (150–1200 px)  on first retry
Location: min_overlap 0.35     →  0.15                   on retry
Latlong:  max_pages 2          →  99 (all pages)         on retry
County:   crop_scale 1.0       →  1.5 (wider crop)       on retry
          → full_page=True if crop retry still fails
```

**U-Net dot thresholds by tier:**
```
early=0.55  transition=0.52  mid=0.50  late=0.47  modern=0.45
```
(Older records have bolder ink dots → higher threshold catches them cleanly.
Modern faint dot marks use a lower threshold to avoid misses.)

---

## 7. Logging

**Per-PDF log:** `logs/{collection}/{year}/{month}/{stem}/{stem}.log`
- Level: DEBUG (captures every regex match, OCR token, API response)
- Format: `HH:MM:SS [LEVEL] module.stem: message`
- Every stage start/end, elapsed time, detected=True/False, confidence
- All OCR word counts, Vision API calls, Gemini calls, retry attempts

**Process-level log:** stdout + `pipeline_run.log`
- Level: INFO by default
- Per-record one-liner: `[N / total]  well_name  OK / PARTIAL / FAILED`
- Per-stage line: `Grid    done 0.8s  conf=92  method=otsu`

**Stage quality summary** (printed at end of each run):
```
── Extraction Quality ──────────────────────────
  Lat/Lon              1,240 / 4,607   (26%)
  Full STR             3,850 / 4,607   (83%)   ← expected after v16 fix
  Any STR field        4,500 / 4,607   (97%)
  Dot detected         4,100 / 4,607   (89%)

── Avg Confidence (done records) ───────────────
  Lat / Lon             94
  Grid                  88
  Location              91
  County                96
  Dot                   79

── Top Counties ─────────────────────────────────
  Creek County         1,241
  Carter County          631
  ...
```

**Error type codes** (written to `{stage}_error_type`):
```
api_error            Vision API / Gemini network or quota failure
model_load_failed    U-Net checkpoint not found or corrupt
grid_image_not_found Grid stage did not save a PNG (dot stage needs it)
keyword_not_found    OCR found no anchor keyword on any page
no_match             Deterministic extractor returned no candidate
not_detected         Heuristic found nothing plausible
not_found            Stage completed but result below acceptance threshold
invalid_crop         Bounding box computed as empty region
exception            Unhandled Python exception (message in error field)
```

---

## 8. U-Net in AWS Batch

### Model files

| File | Size | Location |
|---|---|---|
| `unet_best.pth` | 5.7 MB | `D:\project_modular\unet_best.pth` |
| `unet_dot_detector.py` | 44 KB | `D:\project_modular\unet_dot_detector.py` |

### How they get into Docker

`aws/setup_codebuild.py` zips the entire project and explicitly appends
both files at lines 163–164:
```python
for fname in ["unet_dot_detector.py", "unet_best.pth"]:
    zf.write(PROJECT_ROOT / fname, fname)
```

The buildspec copies everything to `/app/` in the container. The
`dot_extractor.py` fallback chain:
```
UNET_CHECKPOINT env var        (set by run_batch_job.py → /app/unet_best.pth)
  → /app/unet_best.pth
  → /app/../unet_best.pth
  → D:\project_modular\unet_best.pth  (Windows local fallback)
```

### Inference in Batch containers (CPU only)

```
FARGATE_SPOT containers — NO GPU
torch loaded with map_location="cpu"
Inference per grid image: ~0.3–0.8 seconds (512×512, CPU)
Model: UNet(in_channels=1, out_channels=1, features=[16,32,64,128])
       ~1.8M parameters — lightweight, fast on CPU
```

### Batch job flow (AWS)

```
aws/setup_codebuild.py  →  CodeBuild  →  ECR push: osu-pipeline:v16-str-fix
aws/apply_new_image.py  →  Register new job def revision
                        →  Submit array job (N slices)
                              Each slice: run_batch_job.py
                                   reads  200–500 records from S3 index
                                   runs   main.py (all stages, 4 workers)
                                   checkpoints to S3 every 300s
                                   uploads results slice on completion
aws/collect_results.py  →  merge S3 slices → processing_status.csv
aws/monitor.py          →  live dashboard of slice progress
```

**U-Net is used on every Batch container** — it runs identically to local.
The model is bundled into the Docker image, not downloaded at runtime.

---

## 9. Four-Pass Execution Strategy

### Pass 1 — Full Pipeline Run

**Goal:** Process all records for the first time. Get baseline metrics.

```bash
# Local (Windows) — single collection test
cd D:\project_modular\project
python main.py --scan --source D:\ --output D:\project_outputs

# AWS Batch — full dataset
python aws/setup_codebuild.py      # build + push v16-str-fix image
python aws/apply_new_image.py      # register job def, submit array job
python aws/monitor.py              # watch progress
python aws/collect_results.py      # merge results → processing_status.csv
```

**What you get:**
- `processing_status.csv` — one row per PDF, all stages attempted
- Grid PNGs, location crops, county crops, dot overlays
- Stage quality summary printed at end

**Expected Pass 1 results (with v16-str-fix):**
```
Grid:      ~100%  (very reliable)
County:    ~99%   (OCR-anchor mostly handles it)
Dot:       ~89%   (depends on grid quality)
Full STR:  ~83%   (up from 69% after location "of N" fix)
Lat/Lon:   ~26%   (late/modern tiers only)
```

---

### Pass 2 — Retry Failed Records

**Goal:** Re-run stages that failed in Pass 1 with relaxed parameters.

The resume logic (`--resume` is default) automatically retries `failed` stages.
Re-running main.py a second time is sufficient — it skips `done` and `skipped`,
retries `failed`.

```bash
# Re-run locally — only processes failed records, uses relaxed thresholds
python main.py --output D:\project_outputs

# Or force a specific stage to re-run (e.g., location):
python main.py --output D:\project_outputs --stage location
```

**What retry does differently:**
- Grid: uses loose size band (150–1200 px) instead of strict (280–850)
- Location: uses min_overlap=0.15 instead of 0.35
- County: crop_scale=1.5, then full_page=True
- Latlong: scans all 99 pages instead of just 2

**Expected Pass 2 improvement:**
- ~2–5% additional records move from `failed` → `done`
- Grid method breakdown shifts toward `adaptive` and `rotated` (harder grids)

---

### Pass 3 — Manual Review & Inspection

**Goal:** Human review of low-confidence and partial records.

**Tools:**

**A. `inspect_grids.py`** — tkinter GUI for grid QA
```bash
cd D:\project_modular\project
python inspect_grids.py   # opens visual labeler
```
- Loads grid PNGs from output folder
- Press Y (correct), N (wrong grid), S (skip)
- Writes `inspection.csv` with labels
- Use to flag grid detection errors → those records need `--no-resume --stage grid`

**B. `setup_review_queue.py`** — generates review workbooks
```bash
python setup_review_queue.py --status D:\project_outputs\processing_status.csv
```
- Reads all records with `needs_review=True`
- Groups by failure type: partial STR, low county score, low dot confidence
- Writes `manual_review/review_queue.csv` with image paths for human lookup

**C. Direct CSV inspection** — open `review.csv` in Excel
- Columns: `section`, `township`, `range`, `county_name`, `grid_image_path`
- For records with grid path: open the PNG to visually verify
- Add corrections directly in a `corrections.csv` column

**D. `run_coord_enrichment.py`** — test RDS resolution
```bash
python run_coord_enrichment.py \
    --input D:\project_outputs\success.csv \
    --output D:\project_outputs\dot_coordinates.csv \
    --status D:\project_outputs\processing_status.csv
```
- Shows how many dots resolve to coordinates
- `coord_resolution_failures.csv` lists records where RDS lookup failed

---

### Pass 4 — Final Enrichment & Map

**Goal:** Resolve all dots to (lat, lon), update the GitHub Pages map.

```bash
# Step 1: Run coord enrichment on all resolved records
cd D:\project_modular\project
python run_coord_enrichment.py \
    --input D:\project_outputs\success.csv \
    --output D:\project_outputs\dot_coordinates.csv

# Step 2: Build the GeoJSON for the map
python build_map_data.py \
    --dots D:\project_outputs\dot_coordinates.csv \
    --latlong D:\project_outputs\latlong_records.csv \
    --output D:\project_modular\docs\data\well_locations.json

# Step 3: Push to GitHub Pages
cd D:\project_modular
git add docs/data/well_locations.json
git commit -m "Update well map data"
git push origin master
```

**What the map shows:**
- Green dots: lat/lon extracted directly from PDF (late/modern)
- Blue dots: dot interpolated via U-Net + PLSS RDS (early/mid)
- Click a dot: well name, collection, year, county, section/township/range

**`dot_coordinates.csv` columns:**
```
pdf_stem, collection, year, month, well_name,
section, township, range, county_name,
dot_row, dot_col, dot_nw,
lat, lon,
resolution        (RDS strategy that resolved the coordinate)
```

---

## 10. Coordination Map

### Function → Output Table

| Function | Module | Primary output | Written when |
|---|---|---|---|
| `scan_dataset.scan_zips()` | `scan_dataset.py` | `dataset_index.csv` | `--scan` flag |
| `PDFDocumentManager.__init__` | `pdf/pdf_manager.py` | In-memory PIL cache | Once per record |
| `process_single_grid()` | `grid/scoring.py` | `grids/.../stem_grid.png` | Grid detected |
| `process_single_location()` | `location/location_extractor.py` | `locations/.../crop.png` | STR found |
| `process_single_county()` | `county/county_extractor.py` | `counties/.../crop.png` | County found |
| `process_single_dot()` | `dot/dot_extractor.py` | `dots/.../overlay.png` | Dot detected |
| `ProcessingStatus.mark_done()` | `utils/processing_status.py` | `processing_status.csv` | After every stage |
| `write_summary_csvs()` | `main.py` | `success.csv`, `review.csv` | End of run |
| `write_dot_locations_csv()` | `main.py` | `dot_locations.csv` | End of run |
| `write_latlong_csv()` | `main.py` | `latlong_records.csv` | End of run |
| `enrich_with_coordinates()` | `coord/coord_enricher.py` | `dot_coordinates.csv` | Post-pipeline |
| `build_map_data.py` | `build_map_data.py` | `docs/data/well_locations.json` | Manual trigger |

### Stage-to-Stage Data Handoff

```
Stage 2 (Grid) writes:    grids/.../stem_grid.png
Stage 5 (Dot) reads:      same PNG path via grid_dir/stem_page_*_grid.png glob

Stage 3 (Location) writes: location_section/township/range in processing_status
Stage 4 (County) writes:   county_name in processing_status
Post-pipeline reads:        both → dot_locations.csv for RDS enrichment

Stage 5 (Dot) writes:      dot_row, dot_col, dot_nw, x_norm, y_norm
RDS resolver reads:         section+township+range+county+dot_row+dot_col
                            → (lat, lon) via bilinear interpolation in PLSS cell
```

### OCR Cache Reuse Map

```
Vision API call  →  cached at manager._ocr_cache[page_num]
                    ├── Grid stage reads annotations (anchor search)
                    ├── Location stage reads annotations (keyword grouping)
                    └── County stage reads annotations (keyword box)

Cost: 1 Vision API call per page per record (not 1 per stage)
Typical: 2-page PDF = 2 Vision calls, shared across all 3 OCR stages
```

---

*Generated by Claude Code. Last updated: 2026-05-29. Pipeline version: v16-str-fix.*

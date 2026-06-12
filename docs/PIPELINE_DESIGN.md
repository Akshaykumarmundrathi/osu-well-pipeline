# OSU Well Records Pipeline — Design & Data Flow

> **Audience**: developers extending or debugging the pipeline, or building the AWS Batch deployment.
> Updated: 2026-05-29

---

## 1. Single-PDF Processing Flow

Every PDF passes through up to five sequential stages. Each stage reads from the
shared `PDFDocumentManager` (which caches rendered page images) and writes its own
output files plus a row in `processing_status.csv`.

```
┌─────────────────────────────────────────────────────────────────┐
│  PDF SOURCE                                                       │
│  Local ZIP  → utils/zip_reader.get_pdf_bytes(zip_path, key)      │
│  S3 object  → utils/s3_reader.get_pdf_bytes_s3_flat(s3://uri)    │
└──────────────────────────┬──────────────────────────────────────┘
                           │ raw PDF bytes (io.BytesIO, no temp file)
                           ▼
          ┌──────────────────────────────────┐
          │  PDFDocumentManager              │
          │  pdf/pdf_manager.py              │
          │  • Opens PDF with pypdfium2      │
          │  • Renders pages at 2× DPI       │
          │  • Caches PIL images per page    │
          │  • Shared across ALL stages      │
          └──────┬───────────────────────────┘
                 │ manager passed to each stage function
       ┌─────────┼──────────────────────────────────────┐
       │         │                                        │
       ▼         ▼                                        ▼
  ┌─────────┐ ┌──────────────────────────┐  ┌───────────────────────┐
  │ latlong │ │  grid + dot (stages 2+5) │  │  location + county    │
  │ stage 1 │ │  grid/scoring.py         │  │  (stages 3+4)         │
  └────┬────┘ │  dot/dot_detector.py     │  │  location/extractor   │
       │      └─────────┬────────────────┘  │  county/extractor     │
       │                │                   └──────────┬────────────┘
       │                │                              │
       ▼                ▼                              ▼
  latlong_lat      grids/{coll}/               locations/{coll}/
  latlong_lon      {year}/{month}/{stem}/       {year}/{month}/{stem}/
  (direct from     {stem}_page_NN_grid.png        *_location_crop.png
  printed form)    [+ dots drawn on grid]          *_location_page.png
                                               counties/{coll}/{...}/
                                                 *_county_crop.png
                                                 *_county_page.png
       │                │                              │
       └────────────────┴──────────────────────────────┘
                                │
                                ▼
             ┌──────────────────────────────────────┐
             │  Per-record outputs                  │
             │  metadata/{...}/{stem}/metadata.json │
             │  logs/{...}/{stem}.log               │
             │  processing_status.csv  (one row)    │
             └──────────────────────────────────────┘
```

### Stage dispatch (main.py → `_dispatch()`)

| Stage | Condition to run | Key function |
|-------|-----------------|--------------|
| `latlong` | `TIER_CONFIG[tier]["run_latlong"]` = True (late/modern only) | `latlong/extractor.process_single_latlong()` |
| `grid` | Always | `grid/scoring.process_single_grid()` |
| `location` | `TIER_CONFIG[tier]["run_location"]` = True (ALL tiers; per-page illegibility guard in the extractor) | `location/extractor.process_single_location()` |
| `county` | Always | `county/extractor.process_single_county()` |
| `dot` | After grid: `grid_status == done` | `dot/dot_detector.process_single_dot()` |

---

## 2. Post-Pipeline Flow (enrichment → map)

After all Batch jobs finish, run these two scripts locally (or via `collect_results.py`):

```
processing_status.csv   (produced by main.py inside Batch)
         │
         ▼
run_coord_enrichment.py
  • _normalise_row(): maps location_section → section, etc.
  • coord/coord_enricher.enrich_with_coordinates()
       └─ coord/plss_resolver.PLSSResolver.resolve()
            └─ PostgreSQL PLSS database (AWS RDS)
  • Writes:
       dot_coordinates.csv              ← primary enriched output
       coord_resolution_failures.csv   ← unresolved records + reason
       coord_resolution_log.csv        ← compact per-record trace
         │
         ▼
build_map_data.py
  • Reads dot_coordinates.csv
  • Filters to Oklahoma bounding box
  • Builds GeoJSON FeatureCollection
  • Writes docs/data/well_locations.json
  • git add + commit + push → GitHub Pages
         │
         ▼
  https://akshaykumarmundrathi.github.io/osu-well-pipeline/
```

---

## 3. Output Directory Tree

```
<OUTPUT_ROOT>/
├── dataset_index.csv             ← source manifest (pdf_stem, zip_path, collection, year, month)
├── processing_status.csv         ← master per-PDF status (65 columns, see §4)
├── dot_coordinates.csv           ← enriched coordinates (40 columns, see §5)
├── coord_resolution_failures.csv ← RDS misses with reason codes
├── coord_resolution_log.csv      ← compact enrichment trace
│
├── grids/{collection}/{year}/{month}/{stem}/
│   └── {stem}_page_NN_grid.png           ← detected 8×8 grid crop
│
├── locations/{collection}/{year}/{month}/{stem}/
│   ├── {stem}_page_NN_location_crop.png  ← Section/Township/Range text crop
│   └── {stem}_page_NN_location_page.png  ← full page with blue bounding box
│
├── counties/{collection}/{year}/{month}/{stem}/
│   ├── {stem}_page_NN_county_crop.png    ← county keyword crop
│   └── {stem}_page_NN_county_page.png    ← full page with green bounding box
│
├── dots/{collection}/{year}/{month}/{stem}/
│   └── {stem}_dot_annotated.png          ← grid with U-Net dot overlaid
│
├── metadata/{collection}/{year}/{month}/{stem}/
│   └── metadata.json                     ← all extracted fields, one PDF
│
├── logs/{collection}/{year}/{month}/{stem}/
│   └── {stem}.log                        ← DEBUG-level per-PDF log
│
└── manual_review/
    └── failed_records.csv                ← records needing human review
```

---

## 4. `processing_status.csv` — Column Reference (65 columns)

### Identity (10 cols)
| Column | Type | Description |
|--------|------|-------------|
| `pdf_stem` | str | Filename without extension — primary key |
| `pdf_path` | str | `s3://` URI (Batch) or local path |
| `zip_path` | str | Source ZIP file path (local only) |
| `internal_path` | str | Path inside the ZIP |
| `collection` | str | Collection folder name e.g. `ExportedFolderContents (9)` |
| `collection_num` | int | Numeric collection index (1–13+) |
| `year` | str | 4-digit year from folder structure |
| `month` | str | Month string from folder structure |
| `model_tier` | str | `early` / `transition` / `mid` / `late` / `modern` |
| `decade` | str | e.g. `1970s` |

### Lat/Lon stage (9 cols)
| Column | Values / Description |
|--------|---------------------|
| `latlong_status` | `pending` / `done` / `failed` / `skipped` |
| `latlong_confidence` | 0–100 |
| `latlong_error_type` | `api_error` / `no_match` / `parse_error` / … |
| `latlong_lat` | float string, WGS-84 |
| `latlong_lon` | float string, WGS-84 |
| `latlong_well_type` | `oil` / `gas` / `injection` / … |
| `latlong_page` | page index (0-based) |
| `latlong_method` | `labeled_decimal` / `dms` / `unlabeled_decimal` / `form_1002a` |
| `latlong_form_type` | form identifier string |

### Header block (11 cols) — Form 1002A / Locate Well header
| Column | Description |
|--------|-------------|
| `header_county` | County from header |
| `header_section` | Section from header |
| `header_township` | Township from header |
| `header_range` | Range from header |
| `header_quad_raw` | Raw quadrant text (e.g. "NW NW NE") |
| `header_quad_type` | `three_part` / `two_part` / `feet_from` |
| `header_quad_db` | Canonical DB format (e.g. "NW-NW-NE") |
| `header_quad_row` | Grid row 1–8 (derived from quadrant) |
| `header_quad_col` | Grid col 1–8 (derived from quadrant) |
| `header_feet` | Distance in feet (feet_from type only) |
| `header_rel_x` / `header_rel_y` | Normalized position 0.0–1.0 |

### Grid stage (5 cols)
| Column | Values / Description |
|--------|---------------------|
| `grid_status` | `pending` / `done` / `failed` / `skipped` |
| `grid_confidence` | 0–100 |
| `grid_error_type` | error slug |
| `grid_page` | page index where grid was found |
| `grid_method` | `adaptive` / `otsu` / `canny` / `hough` / `rotated` / `corners` |
| `grid_image_path` | relative path to saved grid PNG |

### Location stage (9 cols)
| Column | Values / Description |
|--------|---------------------|
| `location_status` | `pending` / `done` / `failed` / `skipped` |
| `location_confidence` | 0–100 |
| `location_error_type` | error slug |
| `location_section` | Section string e.g. `14` |
| `location_township` | Township string e.g. `5N` |
| `location_range` | Range string e.g. `3W` |
| `location_quadrant_pdf` | Raw quadrant from PDF text |
| `location_quadrant_db` | Canonical DB format (e.g. `SW-NE`) |
| `location_quadrant_row` / `_col` / `_confidence` | derived grid position |

### County stage (4 cols)
| Column | Values / Description |
|--------|---------------------|
| `county_status` | `pending` / `done` / `failed` / `skipped` |
| `county_confidence` | 0–100 |
| `county_error_type` | error slug |
| `county_name` | e.g. `Carter County` |
| `county_score` | fuzzy match score 0–100 |

### Dot stage (7 cols)
| Column | Values / Description |
|--------|---------------------|
| `dot_status` | `pending` / `done` / `failed` / `skipped` |
| `dot_confidence` | 0–100 |
| `dot_error_type` | `model_load_failed` / `grid_image_not_found` / … |
| `dot_row` | U-Net output row 1–8 |
| `dot_col` | U-Net output col 1–8 |
| `dot_nw` | NW-corner quadrant label e.g. `NW-SW-NE` |
| `dot_x_norm` / `dot_y_norm` | Normalized dot position within 8×8 cell (0.0–1.0) |

### Coordinate derivation audit (7 cols)
| Column | Values |
|--------|--------|
| `coord_derivation` | `latlong_direct` / `dot_interpolation` / `not_resolved` |
| `coord_latlong_source` | `labeled_decimal` / `dms` / `unlabeled_decimal` / `form_1002a` / `""` |
| `coord_section_source` | `header_block` / `ocr_extracted` / `not_found` |
| `coord_township_source` | `header_block` / `ocr_extracted` / `inferred` / `not_found` |
| `coord_range_source` | `header_block` / `ocr_extracted` / `inferred` / `not_found` |
| `coord_county_used` | county string used for RDS resolution |
| `coord_dot_source` | `unet` / `ocr_quadrant` / `both` / `not_found` |

### Metadata
| Column | Description |
|--------|-------------|
| `last_updated` | UTC timestamp `YYYY-MM-DDTHH:MM:SS` |

---

## 5. `dot_coordinates.csv` — Column Reference (~40 columns)

Produced by `run_coord_enrichment.py`. Inherits identity + stage columns from
`processing_status.csv` after `_normalise_row()` strips the `location_` prefix:

### Key remapping (intentional — see `run_coord_enrichment._normalise_row()`)

| `processing_status.csv` | `dot_coordinates.csv` | Notes |
|-------------------------|----------------------|-------|
| `location_section` | `section` | Canonical name for RDS lookup |
| `location_township` | `township` | |
| `location_range` | `range` | |
| `location_quadrant_db` | `quadrant_db` | |
| `dot_nw` | `dot_nw` | Unchanged |
| `county_name` | `county_name` | Unchanged |

### Enrichment-added columns
| Column | Description |
|--------|-------------|
| `resolved_lat` | Exact WGS-84 latitude (bilinear interpolation from U-Net dot position) |
| `resolved_lon` | Exact WGS-84 longitude |
| `resolution` | Strategy used: `quadrant_direct` / `exact_county` / `section_adjacent` / … |
| `resolution_pass` | Pass number 1–5 |
| `sec_minx/miny/maxx/maxy` | Section bounding box from PLSS DB |
| `cell_corners` | JSON dict `{tl/tr/bl/br: [lon,lat]}` |
| `rel_x` / `rel_y` | Normalized dot position within cell |
| `flags` | List of QA flags |

### GeoJSON (`well_locations.json`) properties
Built from `dot_coordinates.csv` by `build_map_data.py`:

| Property | Source |
|----------|--------|
| `pdf_stem` | identity |
| `year` | identity |
| `collection` | identity |
| `section` / `township` / `range` | PLSS coordinates |
| `county_name` | county stage |
| `resolution` | enrichment strategy (controls marker style on map) |
| `confidence` | grid_confidence |
| `lat` / `lon` | resolved_lat / resolved_lon |

---

## 6. Column Naming Convention

| Prefix | Meaning |
|--------|---------|
| `latlong_*` | Output from the lat/lon direct-read stage |
| `header_*` | Parsed from the printed form header block (Form 1002A) |
| `grid_*` | Output from the OpenCV grid-detection stage |
| `location_*` | Output from the OCR Section/Township/Range stage |
| `county_*` | Output from the Gemini/Vision county-extraction stage |
| `dot_*` | Output from the U-Net dot-detection stage |
| `coord_*` | End-to-end audit columns (how was the final lat/lon derived?) |

---

## 7. Status Values

All `{stage}_status` columns use exactly these four values:

| Value | Meaning |
|-------|---------|
| `pending` | Not yet processed |
| `done` | Completed successfully |
| `failed` | Processing error — will be retried on next run |
| `skipped` | Stage does not apply to this tier (e.g. latlong on early collections) |

---

## 8. RDS / PLSS Coordinate Resolution Strategies

`PLSSResolver.resolve()` tries strategies in this order (best → least precise):

| Strategy code | Method | Typical precision |
|--------------|--------|------------------|
| `quadrant_direct` | Cell-level bbox from plss_grid, exact quadrant | ~330 ft (cell centre) |
| `exact_county` | Section bbox + county ILIKE match | ±¼ section (~¼ mi) |
| `exact_no_county` | Section bbox, no county filter | ±¼ section |
| `county_constrained` | NS/EW inferred from county direction priors | ±¼ section |
| `ns_fallback` | Try N then S when direction missing | ±½ section |
| `ew_fallback` | Try W then E when direction missing | ±½ section |
| `ns_ew_fallback` | Try all 4 NS×EW combos | ±½ section |
| `county_stripped` | Strip 'County' suffix / try first word | ±½ section |
| `section_adjacent` | Try section ±1 (OCR off-by-one) | ~1 section (~1 mi) |
| `section_centroid` | Centre of section (no dot; Pass 5) | ±½ mi |
| `rds_miss` | No match found | unresolved |

---

## 9. Environment Variables Reference

All required and optional env vars:

| Variable | Required | Default | Used by |
|----------|----------|---------|---------|
| `GOOGLE_APPLICATION_CREDENTIALS` | Yes (local dev) | auto-detected from `credentials/` | config.py |
| `GOOGLE_API_KEY` | Yes | — | county/extractor (Gemini) |
| `GEMINI_FLASH_MODEL` | No | `gemini-2.0-flash-lite` | config.py |
| `GEMINI_PRO_MODEL` | No | `gemini-2.0-flash-lite` | config.py |
| `RDS_HOST` | Yes (enrichment) | — | coord/plss_resolver |
| `RDS_PORT` | No | `5432` | coord/plss_resolver |
| `RDS_DBNAME` | Yes (enrichment) | — | coord/plss_resolver |
| `RDS_USER` | Yes (enrichment) | — | coord/plss_resolver |
| `RDS_PASSWORD` | Yes (enrichment) | — | coord/plss_resolver |
| `OUTPUT_ROOT` | No | `project_outputs_local/` | config.py, run_coord_enrichment, build_map_data |
| `SOURCE_ROOT` | No | `D:\` (local only) | config.py |
| `REPO_ROOT` | No | parent of project/ | build_map_data |
| `D_PROJECT_ROOT` | No | script's own directory | run_coord_enrichment |
| `SMOKE_ROOT` | No | `smoke_test_output/` | smoke_test.py |
| `COLLECTION_BASE` | No | `D:\` | smoke_test.py |
| `INPUT_BUCKET` | Yes (Batch) | — | run_batch_job |
| `OUTPUT_BUCKET` | Yes (Batch) | — | run_batch_job |
| `INDEX_KEY` | Yes (Batch) | — | run_batch_job |
| `GOOGLE_CREDS_SECRET_ID` | Yes (Batch) | `osu-pipeline/credentials` | run_batch_job |
| `RDS_CREDS_SECRET_ID` | Yes (Batch) | `osu-pipeline/rds` | run_batch_job |
| `UNET_CHECKPOINT` | Yes (dot stage) | `/app/unet_best.pth` | dot/dot_detector |
| `SLICE_SIZE` | No | `200` | run_batch_job |
| `WORKERS` | No | `4` | run_batch_job |
| `CHECKPOINT_INTERVAL_S` | No | `300` | run_batch_job |

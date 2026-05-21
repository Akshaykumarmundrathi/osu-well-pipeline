
```markdown
# Context Restore — Oklahoma Well Records Pipeline

You are resuming work on a Python data-extraction pipeline at `D:\project_modular\`.
Read this brief before doing anything. Don't re-explore the codebase first — the
map below is current. Only Read specific files when you actually need to edit them.

## Objective

Extract structured location data from ~631,856 Oklahoma oil-well PDF scans
(ExportedFolderContents (N).zip / year / month / *.pdf). For each PDF, produce:
lat/lon (if printed on doc), section-township-range grid coordinates,
section/township/range text, county name, well name, well type (oil/gas/water).
Final goal: estimate lat/lon for every well (vague-but-close is acceptable) via
extrapolation from county + STR + grid-dot when explicit lat/lon is absent.

## Stages (in execution order)

1. **latlong** — OCR cover page, regex for `Lat: 36.1234 / Lon: -97.5678`
   (or unlabelled decimal pairs). If found, **skip grid + location**.
2. **grid** — OpenCV detect the 8×8 STR-grid box on the back page.
   Six extractors (adaptive/otsu/canny/hough/rotated/corners) → pick largest valid.
3. **location** — OCR + regex extract Section / Township (N/S) / Range (E/W).
   Reject single-field hits (need ≥2 valid).
4. **county** — Vision OCR locates "County" keyword → crop → Gemini Flash
   (pass 1, threshold 95). If <95, fallback to Gemini Pro (pass 2, threshold 86).
   Fuzzy-match against 77-county list with rapidfuzz.

## File map (D:\project_modular\project\)

```
main.py                  Pipeline orchestrator, CLI, retry logic, CSV writers
scan_dataset.py          ZIP/folder scanner → dataset_index.csv; OutputPathBuilder
config.py                Paths, models, thresholds, county list, stage constants
latlong/latlong_extractor.py    Decimal-coord regex; first 2 pages only
grid/extractors.py              6 OpenCV methods sharing _largest_quad_bbox helper
grid/scoring.py                 process_single_grid; iterates pages REVERSED
grid/filters.py                 is_valid_candidate size sanity check
location/location_extractor.py  STR extraction with strict N/S/E/W regex
location/grouping.py            Keyword-box grouping + vertical_overlap
county/county_extractor.py      Two-pass Gemini + rapidfuzz match
county/prompts.py               prompt_pass1 (Flash), prompt_pass2 (Pro)
ocr/vision_api.py               Google Vision client (singleton + 3-retry backoff)
ocr/preprocessing.py            grayscale + contrast + binarize
pdf/pdf_manager.py              fitz wrapper; _iter(converter) shared by PIL/cv2
pdf/rendering.py                pixmap → PIL / cv2 (BGR via slice, no cvtColor)
utils/processing_status.py      CSV-backed per-stage status; SAVE_INTERVAL=25 batched
utils/logging_utils.py          File: DEBUG; Console: ERROR (clean output via print)
utils/zip_reader.py             ZIP listing + byte extraction
utils/io_utils.py               annotate_page only (others dropped as unused)
```

## Key invariants

- **Resume by default** — `processing_status.csv` tracks per-stage DONE/FAILED/
  PENDING/SKIPPED. Re-running skips DONE stages. `atexit.register(force_save)`.
- **Retry policy** — failed records retried ONCE per run (`_RETRIED` set in main.py).
  Month boundary retries that month's failures; year boundary retries year's.
  Deterministic failures (`no_match`, `keyword_not_found`) shouldn't be retried
  twice — single-retry policy is what prevents it.
- **Latlong is OPTIONAL** — most docs (~99%) have no decimal coords. Absence is
  NORMAL, not PARTIAL. A record with grid+location+county done = "OK".
- **Vision API resilience** — 503 / UNAVAILABLE triggers client reset +
  exponential backoff [3, 6, 15]s. Other exceptions propagate.
- **No double Vision calls** — location reuses page-level annotations via
  `_annotations_in_box` (filters by bbox centre instead of re-calling on crop).
- **One Vision API call per page is the cost unit** — when designing changes,
  count Vision calls × 631k records.

## Output files (D:\project_outputs\)

```
dataset_index.csv         All discovered PDFs (one row each)
processing_status.csv     Per-stage status (rewritten every 25 updates)
summary.csv               Final per-record extraction summary
latlong_records.csv       Subset where decimal coords were found
manual_review/failed_records.csv   Per-stage failure log (append-only)
grids/locations/counties/<col>/<year>/<month>/<stem>/   Cropped + annotated images
metadata/<col>/<year>/<month>/<stem>/metadata.json     Raw per-stage output
logs/<col>/<year>/<month>/<stem>.log                   Per-PDF debug log
```

## How to run

```
python main.py --scan --source D:\ --output D:\project_outputs   # first run
python main.py --output D:\project_outputs                       # resume
python main.py --flat ..\pdfs --output D:\project_outputs_test   # test folder
python main.py --pdf path\to\file.pdf --limit 1                  # single file
python main.py --status --output D:\project_outputs              # progress check
```

Models: Gemini 2.5 Flash + Pro (requires `GOOGLE_API_KEY`).
Vision: Google Cloud Vision (requires `GOOGLE_APPLICATION_CREDENTIALS` JSON).
Both wired in config.py.

## Console output style (current)

Clean, no timestamps/levels. Inline per-stage updates:
```
  [    44 / 631,856]  JOHN F RALSTON 1
                ExportedFolderContents (1) | 1911 | 02 - February  (2 pages)
  Lat / Lon     not found  (1s)
  Grid          already done
  Location      sec=14  twp=12N  rng=5W   (100%)  (1s)
  County        Creek County   (100% match)  (4s)
                OK
```
File logs keep full DEBUG detail with timestamps.

## Recent commits (working state)

1. `Clean console output: inline stage results, no timestamps, running totals`
2. `Fix failure handling: optional latlong status, retry-once, reject rubbish`
   - location regex now requires N/S on township, E/W on range
   - section validated to PLSS range 1–36
   - need ≥2 valid STR fields or `detected=False`
   - latlong capped at first 2 pages
   - grid iterates pages reversed
   - `_RETRIED` set prevents double retry
3. `Deduplicate code and add docstrings`
   - grid extractors share `_largest_quad_bbox` + `_crop_with_buffer`
   - PDF manager: single `_iter(converter)`
   - vision_api: `_ocr_bytes` helper
   - io_utils trimmed to only `annotate_page`

## Known open items (not blocking)

- Grid size filter `_W_MIN=280, _W_MAX=850` may miss non-standard formats
- Gemini end-to-end accuracy not fully verified at scale
- OCR is NOT cached across stages — each stage re-OCRs the same page if it
  needs it. Latlong now caps at 2 pages, mitigating worst case.

## Working style preferences

- **Minimize tokens.** Prefer Edit over Write, Grep over reading whole files,
  inline updates over preambles, terse end-of-turn summaries.
- **No emojis** unless explicitly asked.
- **No speculative features, no premature abstractions** — bug fixes don't
  need surrounding cleanup, one-shot ops don't need helpers.
- **Don't comment WHAT** the code does — only WHY when non-obvious.
- **Commit only when asked.** Verify with `git status` before committing.
- **Worktree path:** `D:\project_modular\.claude\worktrees\lucid-wescoff-3f582a`
  — but edit files at the real project path `D:\project_modular\project\*`
  to avoid linter reverts.

## Auto-memory location

`C:\Users\akshay\.claude\projects\D--project-modular\memory\` — auto-loaded
via MEMORY.md index. Project overview lives there.
```

Drop it in at the start of any session by saying *"Read CONTEXT.md, then [your task]"* and I'll have full state without spending tokens on re-exploration.
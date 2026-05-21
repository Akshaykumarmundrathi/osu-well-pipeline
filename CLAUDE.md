# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Extracts structured data (Section, Township, Range, County, grid location) from scanned Oklahoma oil/gas well record PDFs (1911–2024). Source data lives in `ExportedFolderContents (N).zip` archives on `D:\`. All code lives in `D:\project_modular\project\`.

## How to Run

```bash
cd D:\project_modular\project

# Scan source ZIPs + process all records
python main.py --scan --source D:\ --output D:\project_outputs

# Resume after crash (default behaviour)
python main.py --output D:\project_outputs

# Test with sample PDFs (no ZIP needed)
python main.py --flat ..\pdfs --output D:\project_outputs_test

# One stage only
python main.py --flat ..\pdfs --stage grid --output D:\project_outputs_test

# Single PDF
python main.py --pdf ..\pdfs\somefile.pdf --output D:\project_outputs_test

# Progress check
python main.py --status --output D:\project_outputs

# Scan only (writes dataset_index.csv, no processing)
python scan_dataset.py --source D:\ --output D:\project_outputs\dataset_index.csv
python scan_dataset.py --flat ..\pdfs
python scan_dataset.py --summary
python scan_dataset.py --validate
```

## Architecture

### Data flow

```
ZIP file -> scan_dataset.py -> dataset_index.csv
                                      |
                               main.py (per record loop)
                                      |
                         _make_manager(record)          <- opens PDF from ZIP bytes or file path
                                      |
                    PDFDocumentManager (shared across stages)
                         /            |            \
              grid/scoring   location/location   county/county
                   |              extractor          extractor
              OpenCV (6 methods)  Vision OCR    Vision OCR + Gemini 2-pass
                   |              |            |
              grid PNG      crop+annotated PNG  crop+annotated PNG
                                      |
                               metadata.json  (per PDF)
                               processing_status.csv (all PDFs)
                               logs/{stem}.log (per PDF)
```

### Stage return contract

Every `process_single_*` function returns a dict with at minimum:
```python
{"detected": bool, "confidence": int, "error": str|None, "image_path": str|None}
```
Grid adds: `page, bbox, method`. Location adds: `section, township, range, raw_text, annotated_path`. County adds: `name, pass1_result, pass2_result, fuzzy_score, annotated_path`.

### Resume / crash recovery

`ProcessingStatus` (`utils/processing_status.py`) is the source of truth. It writes a row per PDF to `processing_status.csv` after every stage update. On re-run, stages already marked `done` are skipped per-record. Stages marked `failed` are retried. Force a re-run with `--no-resume`.

### PDF source abstraction

`_make_manager(record)` in `main.py` returns a `PDFDocumentManager` from either:
- `record.zip_path` set → reads bytes from ZIP via `utils/zip_reader.get_pdf_bytes()`
- `record.zip_path` empty → opens file path directly

The manager is created **once per record** and passed to all three stage functions. No stage opens the PDF independently.

### No temp files

`ocr/preprocessing.py` and `ocr/vision_api.py` use `io.BytesIO` throughout. No `temp_image.png` or `preprocessed_image.png` are written to disk.

## Key Constraints

- **Google Cloud Vision API** credentials: set `GOOGLE_APPLICATION_CREDENTIALS` to the service account JSON (already wired in `config.py` relative to the project root: `../smiling-breaker-423712-h3-aff7ac746ad4.json`).
- **Gemini API**: requires `GOOGLE_API_KEY` env var. County extraction silently skips the Gemini step if this is not set and `setup_gemini()` raises.
- **rapidfuzz** is optional — county fuzzy matching falls back to `difflib` if not installed.
- PDFs are expected to have ≤ 2 pages. Grid appears on one page only. County keyword search tries page 0 then page 1.
- Grid detection size filter (`_W_MIN=280, _W_MAX=850` in `grid/scoring.py`) was calibrated for 2× resolution rendering. Adjust if PDFs render at a different DPI.

## Output Layout

```
D:\project_outputs\
├── dataset_index.csv
├── processing_status.csv          # open in Excel to see progress
├── grids\{collection}\{year}\{month}\{stem}\
│   └── {stem}_page_NN_grid.png
├── locations\...\
│   ├── {stem}_page_NN_location_crop.png
│   └── {stem}_page_NN_location_page.png   # full page with blue bounding box
├── counties\...\
│   ├── {stem}_page_NN_county_crop.png
│   └── {stem}_page_NN_county_page.png     # full page with green bounding box
├── metadata\...\{stem}\metadata.json      # all extracted fields + confidence
├── logs\...\{stem}.log                    # DEBUG-level per-PDF log
└── manual_review\failed_records.csv
```

## Config Changes

All paths are in `config.py` as `pathlib.Path` objects. The two most commonly changed values:
- `OUTPUT_ROOT = Path(r"D:\project_outputs")` — change to redirect all output
- `RESOLUTION_MULTIPLIER = 2` — increase if OCR quality is poor, decrease for speed

## Adding a New Stage

1. Create `yourmodule/your_extractor.py` with `process_single_X(manager, output_dir, pdf_stem, logger) -> dict`
2. Add `STAGE_X = "x"` to `config.py` and append to `ALL_STAGES`
3. Add a branch in `main._dispatch()`
4. Add `{stage}_status`, `{stage}_confidence`, `{stage}_detail` columns to `utils/processing_status._FIELDNAMES`

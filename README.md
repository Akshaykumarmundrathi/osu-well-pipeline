# Oklahoma Historical Well Records — Digitization Pipeline

**571,446 scanned oil & gas well documents (1911–2024) → structured, mapped, verifiable geographic data.**

🗺️ **[Live interactive map](https://akshaykumarmundrathi.github.io/osu-well-pipeline/)** — **51,559 wells mapped** across all 77 Oklahoma counties (12,662 at exact printed coordinates; growing as eras are processed), hover for details, open the original scanned record, submit corrections inline.

---

## What this is

Oklahoma's historical well records exist as scanned paper forms spanning a century of changing layouts — typed, handwritten, photocopied, multi-page. This pipeline reads them at scale and extracts:

| Field | How |
|---|---|
| **County** | Vision OCR structural anchors + fuzzy matching against the 77-county list, Gemini fallback |
| **Section / Township / Range** | OCR keyword grouping with per-era search regions measured from 2,781 hand-reviewed forms |
| **Well location dot** | OpenCV grid detection (6 methods + measured-envelope priors) → U-Net dot segmentation |
| **Coordinates** | PLSS database resolution (4.5M-cell PostgreSQL): quadrant-cell precision, county-pinned direction disambiguation, section-centroid fallback |
| **Direct lat/lon** | Text extraction on modern (2013+) digital forms |

## Architecture

```
scanned PDF (local zip / S3)
   └─ PDFDocumentManager (cached page renders)
        ├─ latlong/   text coordinates (modern forms)
        ├─ grid/      anchor phrase → measured-envelope crop → full-page CV
        │               └─ form classifier (era-aware, ground-truth guarded)
        ├─ location/  STR extraction (recipe regions → zone hints → full page,
        │               2.5x re-OCR retry for dropped labels)
        ├─ county/    structural anchors → fuzzy → Gemini, multi-page aware
        └─ dot/       U-Net (192x192, regression-gated retraining)
   └─ processing_status.csv  (resumable, crash-safe, run-scoped loading)
   └─ coord/ PLSS resolver (RDS) → dot_coordinates.csv
   └─ build_map_data.py → docs/ (GitHub Pages map)
```

**Design law:** hints and priors *bound and order* the search — they never terminate it. Every stage has a full-page/fallback path, and "suspect" detections are flagged, not silently trusted.

## Human-in-the-loop verification

- **Map verify panel** — anyone can compare a well against its original PDF and submit a correction; a GitHub Action applies it, re-resolves coordinates against the PLSS database, and republishes automatically (`.github/workflows/apply_corrections.yml`).
- **Annotation campaigns** (`project/annotate_campaigns.py`) — issue-targeted manual review with drag-box + single-key labeling; 2,781 reviewed forms produced the per-collection search envelopes in `project/location/recipes.py`.
- **Predict-then-correct dot labeling** (`project/inspect_dots.py`) — the model proposes, a human accepts/corrects; labels feed regression-gated U-Net retraining (`project/retrain_unet.py`).

## Quality machinery

- `project/audit_consistency.py` — cross-examines every extracted county against the county of the resolved PLSS cell (caught a systematic E/W mirror affecting ~2,000 wells)
- `project/sweep_suspect_dones.py` — era-wide detection of silently-wrong "done" records via measured envelopes
- `project/audit_c2345.py` — 11-section extraction quality report
- `project/ASSUMPTION_AUDIT.md` — every pipeline restriction, questioned with evidence
- `project/ISSUES_AND_FIXES.md` — issue registry with root causes and fix paths

## Running

```bash
cd project
pip install -r ../requirements.txt

# Single PDF
python main.py --pdf path/to/record.pdf --output ./out

# Full run against an index (resumable; crashed runs continue where they left off)
python main.py --index dataset_index.csv --output ./out --workers 4

# Quality audit
python audit_c2345.py
```

Credentials via `.env` (see `.env.example`): Google Cloud Vision, Gemini API (optional, key rotation + cooldown built in), PLSS PostgreSQL. AWS Batch deployment under `aws/`.

## Status

Extraction rates on the 1926–1950 validation corpus: grid 77–95% by era, location 64% (35% before ground-truth-guided fixes), county 83%, dot 76%. Map grows continuously as eras are processed and corrections land.

*Oklahoma State University research project. Source records courtesy of the Oklahoma Corporation Commission archives.*

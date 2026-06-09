# Pipeline Redesign — Post Test100 Analysis
*Generated 2026-06-08 after completing 1300-record test across all 13 collections*

---

## 1. Test100 Run Audit

### 1.1 Timings
| Stage | Avg | Max | Bottleneck |
|---|---|---|---|
| Grid (OpenCV) | 3.7s | 6.7s | PDF render + 6 detection methods |
| Location (Vision API) | 1.9s | 4.4s | API RTT |
| County (Vision + Gemini) | 2.5s | 5.5s | Gemini 3s rate-limit |
| Dot (U-Net CPU) | 4.9s | 12.2s | Inference on CPU |
| **Total** | **13.1s** | | **Gemini rate-limit is #1 bottleneck** |

With 4 workers: 18/min theoretical, 7-10/min observed (45-55% efficiency).
**Root cause**: Gemini default = 3s between calls → max 20 Gemini calls/min → caps at ~20 records/min total before other bottlenecks.

### 1.2 API Cost Projection (570K records)
| API | Calls | Rate | Est. Cost |
|---|---|---|---|
| Google Cloud Vision | 1.05M (×2/record) | $1.50/1K | **$1,576** |
| Gemini 2.5 Flash-Lite | 421K (county pass2) | $0.075/1M tok | **$32** |
| AWS Batch (FARGATE_SPOT) | 570K × 13s / 4 CPU | ~$0.04/vCPU-hr | **~$40** |
| **Total** | | | **~$1,650** |

Vision API is the main cost driver. Gemini is negligible. Compute is nearly free on SPOT.

### 1.3 Failure Taxonomy — Root Causes

#### Location `not_found` (330/1300 = 25.4%)
All 330 are `T2_MED` form type (small top-left portrait grid). Root causes:
- Form classifier correctly identifies T2_MED
- But location STRATEGY comes from TIER (collection), not form type
- MID tier (Coll 9-10) assigns `location_keyword` strategy, but T2_MED forms need `str_keywords`
- EARLY tier forms (Coll 1-8) assign `run_location=False` by default; the per-record override only fires when form_type is in `{T1_LARGE, T2_MED, T3_SMALL}` — but the STR zone is then miscalculated
- **Fix**: route by `form_type`, not by `tier`, for location strategy

#### County `no_match` (327/1300 = 25.2%)
Distribution: C4=37, C5=45, C6=41, C7=29, C8=32, C9=13, C10=8, C11=21, C12=76, C13=50
Pattern: anchor finds some OCR text near the county label, but fuzzy-match threshold rejects it.
Root causes:
- For T2_MED forms (C4-C8): OCR reads adjacent text ("OPERATING COMPANY", well name) as county
- For C12-C13: form layout changed; anchor searching wrong region
- Gemini pass2 doesn't help because it gets the same bad crop
- **Fix**: tighter crop around county label; lower fuzzy threshold for Gemini pass (trust Gemini more)

#### LatLon `done` with empty values (389/500 = 77.8% wasted pass)
- 389 records ran the full latlong Vision API call and found NOTHING
- Only 111 records extracted usable coordinates
- Worst: C11 (97/100 ran, only 3 succeeded), C9/C10 (99/100 ran, 1-2 succeeded)
- **TIER_CONFIG is wrong**: claims C11 (LATE) has lat/lon on >85% of forms — it's more like 3-29%
- Real breakdown by actual data: C9=1%, C10=1%, C11=3%, C12=29%, C13=77%
- **Fix**: calibrate `run_latlong` threshold per collection-num from actual data; mark as `failed(no_text_found)` not `done(empty)` so it's queryable

#### Dot `not_detected` (322/382 = 84% of dot failures)
Distribution: heavily weighted to C9=47, C10=43, C11=84, C12=40
U-Net was trained on EARLY tier grids (large bottom-left 8×8 grids).
C9-C11 use portrait top-center grids with different dot appearance.
**Fix**: threshold tuning per tier, or fine-tune U-Net on MID/LATE samples.

#### `grid_image_not_found` (60 records)
Legacy artifact — grid PNG was deleted before dot ran in a previous run (before fix `38690f8`).
Will clear on any resume. No action needed — these become `pending` on fresh runs.

---

## 2. Architecture Redesign

### 2.1 Core Principles (Immutable)
1. **Single-pass, no retries** — every record processed once per run; failures are classified and logged
2. **Resume = skip everything non-pending** — done AND failed AND skipped are all terminal on resume
3. **Form-type is authority** — tier is a coarse prior; form_type detected per PDF overrides all strategy decisions
4. **Cloud-native** — S3 as source+sink, AWS Batch as compute, no local-only code paths
5. **Idempotent** — running the pipeline twice produces identical results for completed records

### 2.2 What Gets Removed
| Current | Why Remove | Replacement |
|---|---|---|
| `--no-retry` flag | Was a test hack; single-pass should be default | Default behavior |
| Month/year retry sweeps (`_retry_failed`) | Never helped; burned API quota | Deleted |
| `SAVE_INTERVAL=25` batching | Correct but `skip_failed` makes saves rare | Keep, reduce to 50 |
| `TIER_CONFIG[run_location=False]` default | Form-type override makes this redundant | Form-type routing |
| Gemini 3s hardcoded rate limit | Blocks parallelism | Per-key rate limiter |
| Hardcoded grid size filter `_W_MIN=280` | Calibrated for 2× only | Dynamic per form-type |
| `api_cache.py` with `.tmp` ext | Defender issue (same as CSV) | `.new` ext |

### 2.3 Tier System Redesign
Current tiers conflate two independent concepts:
- **Form layout** (grid position, STR format, county format) — detected per PDF
- **Data presence** (whether lat/lon or STR is even on the form) — a statistical prior

New approach: **form_type drives extraction**; **collection_num drives data-presence priors**.

```
form_type (detected per-PDF by grid classifier):
  T1_LARGE    → bottom-left landscape grid, STR in header, County above grid
  T2_MED      → top-left portrait grid, STR right-of-grid, County above or right
  T3_SMALL    → tiny top-left/center portrait, SEC/TWP/RGE printed left of grid
  MID_CENTER  → top-center portrait, "LOCATE WELL" anchor, STR left-of-grid
  LATE_RIGHT  → top-right, stacked County/SEC/TWP/RGE tabular layout
  DIGITAL     → no grid detected; lat/lon in header text
  UNKNOWN     → grid found but layout unrecognized

collection_num drives ONLY:
  - Whether to attempt lat/lon extraction (run_latlong prior)
  - U-Net model tier selection
  - STR label variant hint
```

Calibrated `run_latlong` priors from test100 actual data:
```python
_LATLONG_PRIOR = {
    1: 0.00, 2: 0.00, 3: 0.00, 4: 0.00, 5: 0.00, 6: 0.00,  # EARLY
    7: 0.00, 8: 0.00,                                         # TRANSITION
    9: 0.01, 10: 0.01,                                        # MID (1% actual)
    11: 0.03, 12: 0.29,                                       # LATE (3%, 29% actual)
    13: 0.77,                                                  # MODERN (77% actual)
}
# Only run latlong stage if prior > 0.05 (saves API call on C9-C11)
RUN_LATLONG_THRESHOLD = 0.05
```

### 2.4 API Key Rotation
```python
# config.py
import itertools, os

def _load_keys(env_var: str) -> list[str]:
    """Load comma-separated API keys from env var. Validates non-empty."""
    raw = os.environ.get(env_var, "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys or [os.environ.get(env_var.rstrip("S"), "")]  # fallback: singular

GEMINI_API_KEYS = _load_keys("GOOGLE_API_KEYS")   # comma-separated in .env
VISION_API_KEYS = _load_keys("GOOGLE_VISION_KEYS")  # multiple GCP service accounts

# Round-robin key pool (one pool per process, not per-record)
class KeyPool:
    def __init__(self, keys): self._cycle = itertools.cycle(keys)
    def next(self): return next(self._cycle)
```

Rate limit: each Gemini key gets a 1s gap (not 3s) — test showed no 429s on 3s.
With 4 keys: effective throughput = 4 keys × 60/1s = 240 Gemini calls/min. No longer a bottleneck.

### 2.5 Cloud-Native Job Structure

For 570K records:
```
AWS Batch (FARGATE_SPOT):
  ├── 13 array jobs, one per collection
  │   Each job: 4 vCPU, 8 GB RAM, 4 workers
  │   Processes: ~44K records / job
  │   Time: ~44K × 13.1s / 4 workers / 3600 = ~40 hours per job (parallel)
  │   
  ├── Checkpoint: S3 every 100 records
  │   s3://bucket/checkpoints/{collection}/processing_status.csv
  │
  └── SIGTERM handler: uploads checkpoint, sends SNS alert

Total wall time: ~40 hours (13 collections in parallel)
Total cost: Vision $1,576 + compute $40 + misc $20 = ~$1,640
```

For 1000-records-per-collection test:
```
13 × 1000 = 13,000 records
Local: 4 workers → 13,000 × 13.1s / 4 = ~11.9 hours (sequential collections)
AWS Batch: 13 parallel jobs → ~55 min wall time
```

### 2.6 GitHub Skip Integration
Current GitHub Pages map has coordinates from previous test runs.
New pipeline should:
1. Pull `dot_coordinates.csv` from GitHub repo before starting
2. Mark all PDF stems in that CSV as `coord_derivation=already_mapped` in processing_status
3. Skip grid+location+county+dot for those records (only county + STR needed for completeness)

Implementation:
```python
def _load_already_mapped(repo_path: Path) -> set[str]:
    """Return set of pdf_stem values already in GitHub Pages map."""
    coord_csv = repo_path / "docs" / "data" / "well_locations.json"
    # or dot_coordinates.csv from last run
    ...
```

### 2.7 Failure Classification (Final Taxonomy)
```
GRID:
  grid_not_found           — no candidate above threshold (truly no grid)
  grid_size_mismatch       — candidate found but wrong size for form type
  grid_pdf_error           — PDF could not be rendered

LOCATION:
  str_not_found            — OCR ran, no SEC/TWP/RGE pattern matched
  str_partial              — found 1-2 fields but not all 3
  location_pdf_error       — Vision API returned empty response

COUNTY:
  county_text_not_found    — anchor keyword absent from OCR text
  county_no_oklahoma_match — found text but no Oklahoma county fuzzy-matched
  county_pdf_error         — Vision API error

LATLONG:
  ll_no_text               — ran, no coordinate pattern in OCR text (was: done+empty — WRONG)
  ll_out_of_bounds         — found numbers but outside Oklahoma bounds
  ll_pdf_error             — Vision API error

DOT:
  dot_no_grid              — grid PNG not available
  dot_not_detected         — U-Net ran, no dot above threshold
  dot_model_error          — model file missing or failed to load
```

---

## 3. Immediate Implementation (Priority Order)

### P0 — This session
1. ~~Make skip_failed default for --resume~~ (done: `104cc03`)
2. ~~Remove --no-retry flag~~ (undo — just make skip_failed=True always for --resume)
3. Fix latlong `done+empty` → `failed(ll_no_text)` classification
4. Calibrate `run_latlong` per collection-num (disable for C9-C11 based on data)
5. Fix location strategy: route by form_type for T2_MED, not tier

### P1 — Before 1000-record test
6. Gemini key rotation (support GOOGLE_API_KEYS comma-separated)
7. Update api_cache.py to use `.new` extension (Defender issue same as CSV)
8. Remove retry sweep code entirely (not just gate-flag it)
9. Run 1000-record test per collection

### P2 — Before full 570K run
10. County crop refinement for T2_MED (no_match root cause)
11. U-Net threshold calibration per form_type
12. GitHub already-mapped skip integration
13. Multi-key Vision API rotation

---

## 4. Code Quality Audit — Files to Condense

| File | Current Lines | Issues | Target |
|---|---|---|---|
| `main.py` | ~1900 | retry sweep dead code, 3 `_retry_failed` call sites | ~1400 |
| `config.py` | ~450 | TIER_CONFIG wrong priors, latlong flags inconsistent | ~380 |
| `latlong/extractor.py` | ~340 | `done+empty` bug, run_latlong wrong for C9-C11 | ~280 |
| `county/extractor.py` | ~? | no_match not classified by subtype | review |
| `utils/processing_status.py` | ~400 | PENDING = unused after init; can simplify counts() | ~350 |
| `run_test100.py` | ~500 | phases structure, sample size hardcoded | keep |

---

## 5. 1000-Record Test Plan

```bash
# Local (tests the fixes before AWS run):
python run_test100.py --n 1000 --workers 4 --output D:\project_outputs_test1000

# AWS (parallel, 13 jobs):
python aws/orchestrate_robust.py --n-per-collection 1000 --workers 4
```

Expected results with P0+P1 fixes:
- Location rate: 59% → 72%+ (T2_MED fix)
- County rate: 74% → 80%+ (no_match reduction)  
- LatLon: C9-C11 rate stays 1-3% (correct — those forms don't have it)
- Dot: unchanged until P2 (U-Net threshold tuning)
- Total enrichable: 63% → 70%+

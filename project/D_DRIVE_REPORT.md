# D: Drive — Deep Folder-by-Folder Report & Pattern Mine (Jun-16)

Every folder on D:, what it holds, and the patterns/insights mineable from it.

## A. Source data (ground truth — never delete)
| Folder | Contents | Notes |
|---|---|---|
| `ExportedFolderContents (1..13)` | The **571,446 source 1002A PDFs** (the entire corpus), foldered year/month | C1=1911 … C13=2024. Identical set to the 13 ZIPs. |

## B. The processed-data mine: `D:\project_outputs\`
| Subfolder | Files (approx) | What it is | Pattern value |
|---|---|---|---|
| `api_cache/` | **118,624** | Cached Vision OCR results (keyed by image bytes) | **The $-saver.** Any record already here re-processes FREE. ~95k unique images. |
| `counties/` | **106,293** | County crop + green-box annotated PNGs per record | Visual QA of county extraction. |
| `grids/` | ~tens of thousands | Grid crop PNGs (the "spot well" grid) | Training material for the dot detector. |
| `locations/` | ~tens of thousands | Location crop + blue-box page PNGs | STR-region QA. |
| `metadata/` | **52,004** | Per-record `metadata.json` (structural fingerprint) | **Richest mine** — see §D. |
| `logs/` | ~44,700 | Per-PDF DEBUG logs | Error-trace mining. |
| `failures/` | 5 CSVs | Clean per-stage failure lists + summary | Human-inspectable failure queue. |
| `dots/`, `dot_labels/` | small | Dot-detection outputs + manual labels | U-Net QA. |
| CSVs | — | dataset_index (571,446), processing_status (~515k), dot_coordinates, master_ledger | The trackers. |

## C. The manual-annotation GOLDMINE: `D:\review_campaigns\`
Hand-drawn bounding boxes (grid / STR / county / lat-long, as %-of-page) + rich
notes, organized per collection. **This is the data that tunes the pipeline.**

| Campaign | Collection(s) | Index | **Annotated** | Status |
|---|---|---|---|---|
| `c8_layout` | C8 (1980s) | 477 | **511** ✓ | mined → C8 grid envelope x~0.04 y~0.16 w~0.13 h~0.16, STR x~0.26 |
| `early30s_loc` | C2/C3 (1930s) | 194 | **195** ✓ | mineable for 1930s location envelopes |
| `c7_grid` | C7 (1970s) | 143 | **0** | **queued — not yet annotated** |
| `c6_county` | C6 (1960s) | 126 | **0** | **queued — not yet annotated** |
| `c12_modern` | C12 (2014+) | 165 | **0** | **queued — not yet annotated** |

**ACTIONABLE:** completing the 3 un-annotated campaigns (c6/c7/c12, ~434 records)
would yield ground-truth GRID/STR/COUNTY envelopes for those collections — the
same lever that lifted C8. This is free, high-value pipeline tuning waiting on
~1–2 hours of human box-drawing.

## D. Structural patterns from 52,004 metadata fingerprints
- **Form types seen:** MID (mid-page form, C9-10) most common in sample, then
  T4_NOANCHOR (no anchor phrase), T2_MED, LATE, T1_LARGE, UNKNOWN.
- **Grid-detection methods used:** `anchor_above_adaptive` dominates (~43% —
  anchor phrase + adaptive threshold), then `adaptive`, `rotated` (skew
  correction), `envelope_adaptive`/`envelope_rotated` (the recipe envelopes,
  ~10%), `canny` variants. → the envelope recipes are actively helping ~1-in-10.
- **bbox convention is mixed** ([x,y,w,h] vs [x0,y0,x1,y1]) across records — a
  known data-hygiene issue; width stats from metadata are unreliable until
  normalized.

## E. Model training data
| Folder | Files | Contents |
|---|---|---|
| `well_dot_detector/` | 5,568 | Original U-Net dev: grid_images_final, `manual_labels.csv` (**550 dot labels**), predictions, label tools |
| `unet_retrain/` | 4,615 | Retraining set: images, labels, holdout (93), baseline.json, train.log, loss curve |
| Total hand-labeled dots | **~643** | The U-Net training ground truth |

## F. Tools / misc
| Folder | Use |
|---|---|
| `tools/` (107 files) | tessdata, tesserocr wheel, micromamba, pip cache (the OCR-engine experiments) |
| `inspection_pdfs/`, `tmp_test_5pdfs/` | tiny dev test sets |
| `project_modular/` | the code repo (project/, docs/, .git) |
| `project_outputs_sample/` | run indexes (c11/c12/c13/campaign), logs, keeper/watchdog scripts |

## G. Not project (leave alone)
`$RECYCLE.BIN`, `DockerDesktopWSL`, `System Volume Information`, `Seagate`, `PC`,
`photos_google`, `starthere_&warranty`, `d`, `tmp` (empty).

## Top takeaways
1. **`review_campaigns/` is an under-used asset** — 3 collections (C6/C7/C12) are
   queued for annotation; finishing them = free per-collection envelope tuning.
2. **`api_cache/` (118k) makes targeted re-extraction free** — the lever behind
   the section-recovery and any future cached re-runs.
3. **`metadata/` (52k) is the structural-pattern source** — form-type/method
   distributions per decade; normalize the bbox convention to unlock width stats.
4. **~643 hand dot-labels + 706 box annotations** are the curated ground truth
   for model + envelope improvement — the highest-value human work on disk.

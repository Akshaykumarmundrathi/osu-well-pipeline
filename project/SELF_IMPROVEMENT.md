# Self-Improvement Strategies for U-Net and Grid Detector

Auto-generated reference for iterative model improvement using pipeline output data.
Run `python review.py` first to regenerate the training CSV exports before each iteration.

---

## 1. Grid Detector (`grid/scoring.py`)

### What it does
Six OpenCV-based detection methods (adaptive threshold, Canny edges, Hough lines, etc.)
scored against expected grid dimensions (_W_MIN=280, _W_MAX=850).  
The highest-scoring candidate wins; confidence = scoring function output.

### How to improve iteratively

#### Step 1 — Understand failure modes
```
D:\project_outputs_local\manual_review\grid_training_negatives.csv
```
Open these 27 records (grid_not_detected + grid_low_confidence).  
For each, manually note WHY the grid wasn't found:
- Faint/torn form → preprocessing issue (increase contrast in `ocr/preprocessing.py`)
- Grid outside expected size → adjust `_W_MIN / _W_MAX` bounds
- Wrong page → try page 1 (currently tries page 0 only)
- Rotated grid → add a rotation-correction pre-pass

#### Step 2 — Build a positive set from high-confidence records
```
D:\project_outputs_local\manual_review\grid_training_positives.csv  (331 records)
```
For each record, `grid_image_path` points to the extracted grid PNG.
These are confirmed-correct crops usable as:
- Visual regression test set (run detector → compare bounding box to saved PNG)
- Parameter calibration (histogram the `grid_confidence` distribution to find
  the optimal score threshold)

#### Step 3 — Calibrate scoring thresholds
```python
# In grid/scoring.py — tune these constants using the positives CSV:
_W_MIN = 280     # increase if many false positives from small artifacts
_W_MAX = 850     # decrease if over-merged regions are being selected
_SCORE_MIN = 0.3 # minimum normalised score to accept a candidate
```
Run the detector on the 331 positives and plot confidence vs known-good flag
to find the threshold where precision >= 0.95.

#### Step 4 — Multi-scale pass
Currently renders at 2x. For the 27 failed records, try:
```python
# In config.py:
RESOLUTION_MULTIPLIER = 3  # 3x for a second attempt on failed records
```
Add a fallback: if `grid_confidence < 30` at 2x, retry at 3x before marking failed.

#### Step 5 — Manual annotation for a labelled dataset
Export `grid_training_negatives.csv` to a labelling tool (LabelImg / CVAT).
Draw bounding boxes on the actual grid.  Export as YOLO or COCO format.
Train a lightweight YOLO-nano detector on the annotated set; swap it in as a
5th detection method and let the scoring ensemble vote.

---

## 2. U-Net Dot Detector (`dot/dot_extractor.py`, `unet_best.pth`)

### What it does
Runs a U-Net on the extracted 512×512 grid PNG to produce a heatmap.
Peak of the heatmap = dot location in 8×8 grid coordinates.
Outputs: row, col, nw (quadrant), x_norm, y_norm, confidence.

### Training signal from pipeline output

| CSV | Records | Use |
|-----|---------|-----|
| `dot_training_positives.csv` | 173 | Confirmed correct: high confidence + not on edge |
| `dot_training_negatives.csv` | 87 | Hard negatives: no-dot (skipped) + edge FPs |
| `dot_calibration.csv` | 0 | Borderline: review these to set threshold |
| `grade_A_complete.csv` | 153 | Gold standard: county + STR + grid + dot all good |

### Improvement steps

#### Step 1 — Confidence calibration
```
dot_calibration.csv: records with 50 <= dot_confidence < 70
```
Manually verify which are correct in `dot_calibration.csv`.
If >60% are correct → lower `_DOT_LOW_CONF_THRESHOLD` in `review.py` to 55.
If <40% are correct → raise threshold to 75.

#### Step 2 — Edge position analysis (priority)
```
dot_quality_issues.csv: 60 records with dot on row/col 0 or 7
```
These are the boundary false positives. Manually verify each.
Expected: ~50% are genuine dots near the corner, ~50% are FPs (background noise).

Implementation fix: add a soft penalty in `dot_extractor.py`:
```python
# After finding peak in heatmap — penalise boundary positions:
EDGE_PENALTY = 0.25   # multiply confidence by this if on boundary
if row in (0, GRID_SIZE - 1) or col in (0, GRID_SIZE - 1):
    confidence *= (1 - EDGE_PENALTY)
```

#### Step 3 — Hard-negative mining
`dot_training_negatives.csv` contains 87 grid PNGs where no dot should appear
(dot_skipped = no grid, dot_position_edge = boundary FP).

Add these as negative examples in the next training epoch:
```python
# In your training script:
# Negative samples: grid PNGs from dot_training_negatives.csv
# Label: all-zero heatmap  (no dot present)
# Weight: 2x sample weight to increase hard-negative influence
```

#### Step 4 — Fine-tuning with active learning loop
```
1. Run pipeline on new year (e.g. 1912)
2. python review.py --year 1912
3. Open dot_calibration.csv — review borderline records
4. Annotate correct dot positions in the grid image (click on PDF viewer)
5. Add to training set with corrected label
6. Fine-tune: python dot/train.py --resume unet_best.pth --epochs 10
7. Evaluate on dot_training_positives.csv — confirm no regression
8. Replace unet_best.pth if new model improves F1 on the test set
```

#### Step 5 — Per-tier threshold tuning
Early (1911) forms have bold hand-drawn dots → high confidence expected.
Transition (1920s) forms may have faded dots → lower threshold needed.

```python
# In dot/dot_extractor.py — tier-specific thresholds:
_CONF_THRESHOLD = {
    "early":      0.65,
    "transition": 0.50,
    "mid":        0.55,
    "late":       0.60,
    "modern":     0.65,
}
```

Use `review.py --collection 1 --year 1920` confidence distributions to calibrate.

#### Step 6 — Ensemble for low-confidence predictions
When `dot_confidence < 0.65`:
- Run the U-Net a second time at 1.5x crop padding  
- Run a classical blob detector (SimpleBlobDetector) as a fallback
- Take the prediction that agrees between the two approaches

---

## 3. Combined Extractor prompt tuning (`extract/combined_extractor.py`)

### Current performance (1911)
- County detection: 93% (335/358)
- Full S+T+R: 48% (172/358)
- Missing township: 34 records — township typically written as "T.18N" (OCR skips the N)
- Missing range: 33 records — range written as "R.3" without E/W direction

### Prompt improvements
```python
# In county/prompts.py — prompt_combined additions:
# 1. Normalise "T.18" → "18N" when context shows Township column heading
# 2. Try OCR regex fallback first: re.search(r'T\.?\s*(\d+)([NS])', text)
# 3. For range direction: infer E/W from known county geography if Gemini null
```

### Township normalisation fix (quick win)
In `extract/combined_extractor.py`, the regex `_parse_str_from_text()` currently
looks for `T(\d+)([NS])`. Add alternates:
```python
TWP_RE = re.compile(
    r'[Tt](?:wp|vp|ownship)?\.?\s*([0-9]{1,3})\s*([NSns])?',
    re.IGNORECASE,
)
```
If direction (N/S) is missing, default to "N" for Oklahoma (all townships are N
except the strip along Texas border which is S — can be inferred from county name).

---

## 4. Measurement cadence

After each year completes, run:
```bash
python review.py --output D:\project_outputs_local --collection 1 --year YYYY
```

Track these metrics year over year:
| Metric | 1911 baseline | Target |
|--------|--------------|--------|
| County detection | 93% | 97% |
| Full S+T+R | 48% | 65% |
| Grade A | 43% | 55% |
| dot_position_edge rate | 18% | <8% |
| grid_not_detected | 7.5% | <4% |

If a year's metrics drop significantly vs baseline, check:
1. Whether the form layout changed (transition year)
2. Whether OCR confidence dropped (different scan quality)
3. Whether any new county name variants appeared

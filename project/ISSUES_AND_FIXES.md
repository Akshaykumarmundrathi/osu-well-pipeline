# Issues Registry — C2-C5 run findings & inspection-based fixes

Updated 2026-06-10. Each issue lists: evidence, root cause (or hypothesis),
the inspection tool to study it, and the fix path. Manual review outputs
feed back into code/model changes.

## Inspection toolkit

| Script | What it does | Output |
|---|---|---|
| `capture_structures.py` | Harvests every metadata.json into one structural-fingerprint CSV (grid bbox/AR/zone, form type, anchor, STR presence, dot position, timings) + prints pivots | `$OUTPUT_ROOT/form_structures.csv` |
| `inspect_locations.py` | Samples grid-done/location-failed records into a flat review folder with grid + full-page PNGs and OCR snippets | `$OUTPUT_ROOT/location_review/` |
| `inspect_dots.py` | tkinter click-labeler over dot-failure grid PNGs; labels written in the exact `manual_labels.csv` schema U-Net retraining consumes | `$OUTPUT_ROOT/dot_labels/manual_labels.csv` |
| `inspect_grids.py` | (existing) Y/N/S grid labeling GUI | `inspection.csv` |
| `audit_c2345.py` | 11-section quality audit of the C2-C5 sample | stdout |
| `fix_county_shortmatch.py` | Detects + resets records poisoned by the short-text county bug | resets rows in processing_status.csv |

---

## P1+P2 — ROOT CAUSE FOUND: casing-table false positive → MID misclassification

**These two issues turned out to be the SAME bug.** Verified visually on
`X_ADELINE L LOWRANCE 38_2373600` (1941, C3):

1. The grid detector picked the mid-page **CASING RECORD / WATER SANDS
   table** (626×512 px median) instead of the real PLSS grid (208×219) at
   top-left.
2. The form classifier saw a top-center rectangle and labeled it **MID** —
   a form type that only exists 1983-2000 (C9-10).
3. MID hints sent location looking LEFT of the "grid" (wrong), county to
   the wrong region, and the dot stage ran U-Net on a casing table.

**Measured blast radius (form_structures.csv):** 449 early-tier records
classified MID → location 1% (7/449), dot 8% (39/443). In 1939-47:
MID records 0% location vs non-MID 46%. The entire "1940s location
decline" is this bug.

**Fix applied (this session):**
- `config.py`: `TIER_GRID_W_MAX = {early: 565, transition: 565}` — real
  early grids max 558 px wide (p99=411, n=1000 dot-verified), tables are
  577-812. Cap rejects the table at candidate level so the real grid wins.
- `grid/scoring.py`: applies the cap; passes `tier` to the classifier.
- `grid/form_classifier.py`: top-center / top-right zones on early or
  transition tier now return FORM_UNKNOWN + STR_ANY open hints instead of
  MID/LATE.

**Verified:** ADELINE re-run now detects the real grid (272×275, top_left,
T4_NOANCHOR) and county = Nowata. Location still fails on that record —
the typed header STR ("SEC 30 TWP 25N RGE 17E") isn't matched by the
upper_right/vstack patterns → study with `location_review/` pack (P5).

**Repair:** after run completes: `python fix_mid_misclass.py --apply`
(resets all stages for early-tier MID records), then re-run those records.

**Still open (true MID forms, C9-10):** the U-Net resizes everything to
192×192; on genuine 626×512 MID-era grids the dot shrinks ~9× in area vs
training data — label with `inspect_dots.py --form-type MID` on C9-10
records and retrain (see U-Net section).

### U-Net: one model or two?

**One model is enough — and preferred.** Reasons:

1. The pipeline already resizes every grid PNG to **192×192** before
   inference (`IMG_SIZE = 192` in `unet_dot_detector.py`), so the network
   never sees the original size. Input dimension is NOT the problem.
2. U-Net is fully convolutional — after training on a mixed dataset it
   handles visual variety fine; "dot on a grid" is the same low-level task
   across formats.
3. The 7% MID failure is a **training-data coverage gap**, not an
   architecture limit: the model has never seen MID line spacing/AR, so its
   probability map stays under threshold.

**Retraining recipe:**
- Label 150-300 MID grids with `inspect_dots.py` (mix of dot-present and
  dot-absent; absent ones become negative masks — already supported via
  `write_manual_mask` with empty dots list).
- Merge with the existing training set (do NOT train MID-only — that
  forgets the early forms).
- Optional but recommended: letterbox (pad-to-square) instead of stretch
  in `DotDataset` so tall MID grids (AR 0.56) aren't distorted; apply the
  same letterbox at inference.
- Validate per-form-type, not just globally — keep a held-out split for
  T2_MED/T1/T4 to confirm no regression.

**When two models would be justified:** only if mixed retraining measurably
degrades early-form accuracy (>2-3 pts on the held-out split) — unlikely at
this dataset size, and a second model doubles maintenance + Docker size.
A cheaper middle ground is per-tier *thresholds*, which the code already
supports (`_threshold_for_tier`).

## P3 — Short-text county fuzzy match (FIXED, repair queued)

**Evidence:** 92 records got "Blaine County" from a stray OCR `'N'`
(WRatio partial-matched `n`⊂`blaine` at 90).
**Fix:** committed `addb7e4` — reject candidates <3 chars.
**Repair:** after run completes: `python fix_county_shortmatch.py --apply`
then `python run_c2345.py --stage county --workers 2`.

## P4 — Missing township/range direction suffixes

**Evidence:** of 852 done locations: 406 townships lack N/S, 332 ranges
lack E/W. Values otherwise 98% plausible.
**Fix path:** infer the suffix from the resolved county's PLSS extent during
coord enrichment (most OK counties are unambiguous). No OCR work needed.
Implement in `coord/plss_resolver.py` as a fallback before declaring
resolution failure.

## P5 — Range is the weakest STR field

**Evidence:** missing range 184 vs township 115 vs section 151 (of 852).
**Inspect:** check `location_review/index.csv` raw_text snippets — is range
present in OCR but unparsed (pattern gap) or absent (crop gap)?
**Fix path:** depends on inspection outcome.

## P6 — Silent pipeline freezes on Windows

**Evidence:** resume5 froze 4 h at record 2,311 (workers alive, no errors,
no lock files); earlier PID 7732 died similarly.
**Mitigations in place:** Vision gRPC 30 s timeout (5e392c1), `cmd.exe /c`
detached launch, stall-detecting monitor (10 min no-growth alert).
**Open:** root cause unconfirmed — suspect multiprocessing pool deadlock on
worker death. Cloud run sidesteps this (Batch auto-retries the slice).

## P7 — processing_status.csv rename contention (WinError 5)

**Evidence:** `_save_locked rename attempt N failed` whenever ANY process
holds the CSV open (Excel, audit scripts, Import-Csv).
**Rule:** never read the live CSV directly — copy it first
(`cp processing_status.csv snapshot.csv`). Audit scripts updated to do this.
Pipeline retries + re-saves next cycle, so transient contention is harmless.

## P8 — Gemini quota / key rotation thundering herd

**Evidence:** all 12 free-tier keys hit 500 RPD on 2026-06-09; workers
converge on the same key after rotation.
**Fix path (pending):** per-worker key striping (worker i owns keys
i, i+N, i+2N, ...) — needs worker index passed into `setup_gemini()`.
For cloud: paid-tier key removes the problem entirely.

## P9 — 1931 location rate 30% / T4_NOANCHOR

**Evidence:** 1931 worst pre-1939 year; T4_NOANCHOR dominant (613 grids).
Full-page search without anchor is harder.
**Inspect:** `python inspect_locations.py --year-from 1931 --year-to 1931`.

## P10 — Debug image volume (~2 MB/record)

**Evidence:** 11.7 GB for ~6K records (counties 4.4 GB = full-page annotated
PNGs are the biggest).
**Decision needed before cloud run:** keep crops, drop full-page annotated
PNGs at scale (or sample them, e.g. 1-in-50) → ~1.1 TB → ~100 GB.
Disk watchdog already prunes at 4 GB inside Batch containers.


## P11 — C2-era wells marked with hollow CIRCLES, not dots (530 records)

**Evidence (Jun-12):** 530 C2 dot failures; U-Net max activation <0.15 on
58/60 sampled (model literally blind to them). Visual inspection: 1926-40
forms mark the well with a small hollow "o"/"O", sometimes typed — the
model was trained exclusively on filled ink dots. Forms also carry a
PRINTED circle at grid centre (decoy) and "160" acreage labels whose 0/6
loops are circle-shaped.

**CV fallback attempted and REJECTED:** Hough + ring-contrast guards both
failed visual verification (circled digit zeros, smudges, empty cells —
2 rounds of overlay review). The well-circle vs printed-zero distinction
is semantic; pure geometry can't make it safely.

**Fix path:** U-Net round-2 with circle-style labels. The 530 retained C2
grid crops are ideal labeling material (clean, distinct marks).
~120-150 human clicks via inspect_dots.py (predict-then-correct) gives the
training set; retrain_unet.py gate protects existing accuracy.
**HUMAN ASK: one C2 labeling session (~20-30 min).**

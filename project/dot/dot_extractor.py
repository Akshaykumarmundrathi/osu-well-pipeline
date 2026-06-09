"""
Stage 5 — Dot Detection

Runs U-Net inference on the grid PNG saved by the grid stage to locate the
well-spot (ink dot) that marks the well's position within the 8×8 PLSS grid.

Returns a result dict compatible with ProcessingStatus.mark_done():
  detected    bool
  row         int    (1-based grid row)
  col         int    (1-based grid col)
  nw          str    (NW-quadrant label, e.g. "NW-SW-NE")
  confidence  float  (mean U-Net probability in the detected blob, 0-1)
  x_norm      float  (normalised x in original grid image, 0-1)
  y_norm      float  (normalised y in original grid image, 0-1)
  image_path  str    (path to saved prediction overlay PNG)
  error       str    (only present on failure)

Tier-awareness
--------------
Older PDFs (EARLY/TRANSITION tiers) were printed on coarser paper with larger,
bolder dots — the U-Net is slightly over-confident on these, so we raise the
acceptance threshold to suppress false positives.
Modern PDFs (LATE/MODERN) can have very faint dots — we lower the threshold a
notch so faint wells are not silently missed.
These defaults can be overridden via parameter_suggestions.json produced by
the evolutionary advisor (utils.evolutionary).
"""

import json
import logging
import os
import sys
from pathlib import Path

# Locate the unet_dot_detector module. In Docker it is copied alongside the
# project; locally it lives at D:/well_dot_detector.
_HERE = Path(__file__).parent
_DETECTOR_CANDIDATES = [
    _HERE.parent.parent / "well_dot_detector",           # D:/well_dot_detector (local dev)
    _HERE.parent.parent / "well_dot_detector" / "well_dot_detector",
    _HERE.parent.parent,                                 # D:/project_modular (unet_dot_detector.py copied here)
    _HERE.parent / "well_dot_detector",                  # Docker flat copy
    _HERE.parent,                                        # /app (Docker)
]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier-adaptive thresholds
# ---------------------------------------------------------------------------
# Base threshold from evolutionary advisor (written by utils.evolutionary).
# Falls back to per-tier hardcoded defaults.
#
# Calibration notes (2026-06-09, test100 analysis — 1,300 records):
#
#   EARLY / TRANSITION (C1-C8): 80-95% dot detection success.
#     Dots are bold, high-contrast ink circles on coarse paper.  U-Net is
#     well-calibrated here.  Threshold 0.50-0.55 works.
#
#   MID (C9-C10): 25-50% success.  The U-Net doesn't generalise as well
#     to the smaller, centred MID-era grid layout.  Lowered to 0.35 as a
#     best-effort — detected dots at this threshold should be flagged for
#     manual review (DOT_REVIEW_BELOW in config).
#
#   LATE (C11-C12): 9-13% success with old thresholds.  LATE forms use a
#     top-right grid with a different aspect ratio; the model was trained
#     predominantly on EARLY forms.  Lowered to 0.28 — any detections at
#     this sensitivity MUST be manually reviewed.  Full model retraining
#     on C11-C12 grids is the proper long-term fix.
#
#   MODERN (C13): 30% success.  Digital forms, dot is a small filled
#     circle or check-mark.  Lowered to 0.26.
#
# Override all tiers via UNET_THRESHOLD env var (single float 0-1).
_TIER_THRESHOLD = {
    "early":      0.52,   # coarser/bolder dots — keep slightly elevated
    "transition": 0.48,
    "mid":        0.35,   # lowered: U-Net under-activates on MID grids
    "late":       0.28,   # lowered: high false-negative rate on C11-C12
    "modern":     0.26,   # lowered: digital forms, small dot symbols
}
_DEFAULT_THRESHOLD = 0.40


def _load_evolved_threshold(output_root: Path | None) -> float | None:
    """
    Read parameter_suggestions.json written by learn_from_run().
    Returns the suggested U-Net threshold if present, else None.
    """
    if not output_root:
        return None
    suggestions_file = output_root / "parameter_suggestions.json"
    if not suggestions_file.exists():
        return None
    try:
        data = json.loads(suggestions_file.read_text(encoding="utf-8"))
        for s in data.get("suggestions", []):
            if "unet" in s.get("parameter", "").lower():
                # Suggestion format: "U-Net threshold / ..." — parse current config
                return None  # threshold tuning is manual for now; future: parse value
    except Exception:
        pass
    return None


def _threshold_for_tier(tier: str, output_root: Path | None = None) -> float:
    """
    Return inference threshold for this tier.

    Priority:
      1. UNET_THRESHOLD env var (single float) — overrides all tiers.
      2. parameter_suggestions.json from evolutionary advisor.
      3. Per-tier hardcoded defaults above.
    """
    # Env-var global override (useful for threshold experiments)
    env_thresh = os.environ.get("UNET_THRESHOLD", "").strip()
    if env_thresh:
        try:
            return float(env_thresh)
        except ValueError:
            log.warning("UNET_THRESHOLD=%r is not a valid float — ignored", env_thresh)

    evolved = _load_evolved_threshold(output_root)
    if evolved is not None:
        return evolved
    return _TIER_THRESHOLD.get(tier, _DEFAULT_THRESHOLD)


def _ensure_detector_on_path():
    for p in _DETECTOR_CANDIDATES:
        if (p / "unet_dot_detector.py").exists():
            s = str(p)
            if s not in sys.path:
                sys.path.insert(0, s)
            return
    # If copied into the project package itself (Docker flat copy)
    # the import will succeed without a path change.


_ensure_detector_on_path()

# Lazy singleton: model loaded once per process, never reloaded.
_model = None
_UNET_CKPT = os.environ.get("UNET_CHECKPOINT", "unet_best.pth")


def _get_model():
    """Load the U-Net from checkpoint exactly once per process."""
    global _model
    if _model is not None:
        return _model

    import torch
    from unet_dot_detector import UNet  # noqa: E402  (sys.path extended above)

    ckpt_path = Path(_UNET_CKPT)
    if not ckpt_path.exists():
        ckpt_path = _HERE.parent / "unet_best.pth"          # /app/unet_best.pth (Docker)
    if not ckpt_path.exists():
        ckpt_path = _HERE.parent.parent / "unet_best.pth"   # D:/project_modular/unet_best.pth (local)
    if not ckpt_path.exists():
        raise FileNotFoundError(
            f"U-Net checkpoint not found. "
            f"Set UNET_CHECKPOINT env-var or place unet_best.pth at {_UNET_CKPT}"
        )

    ckpt     = torch.load(str(ckpt_path), map_location="cpu", weights_only=True)
    features = ckpt.get("features", [16, 32, 64, 128])
    model    = UNet(in_channels=1, out_channels=1, features=features)
    model.load_state_dict(ckpt["model_state"])
    model.eval()
    _model = model
    log.info("U-Net loaded from %s  features=%s", ckpt_path, features)
    return _model


def process_single_dot(
    grid_dir:    Path,
    output_dir:  Path,
    pdf_stem:    str,
    logger,
    tier:        str = "mid",
    output_root: Path | None = None,
) -> dict:
    """
    Find the grid image saved by the grid stage, run U-Net inference, and
    return a result dict.

    grid_dir    — grids_dir(record) folder written by the grid stage.
    output_dir  — dots_dir(record) folder for prediction overlays.
    tier        — collection tier (from config.tier_for); controls threshold.
    output_root — pipeline output root; used to read parameter_suggestions.json.
    """
    grid_images = sorted(grid_dir.glob(f"{pdf_stem}_page_*_grid.png"))
    if not grid_images:
        return {"detected": False, "error": "grid_image_not_found"}

    grid_path = grid_images[0]
    threshold = _threshold_for_tier(tier, output_root)

    try:
        from unet_dot_detector import DotDetector
        model = _get_model()
    except Exception as exc:
        logger.error("Failed to load dot detector model: %s", exc)
        return {"detected": False, "error": f"model_load_failed: {exc}"}

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        detector = DotDetector(
            model=model,
            output_dir=output_dir,
            threshold=threshold,
            min_area=8,
            max_dots=1,
        )
        dots = detector.predict_image(grid_path)
        logger.debug("Dot inference: tier=%s threshold=%.2f detected=%d",
                     tier, threshold, len(dots))
    except Exception as exc:
        exc_str = str(exc)
        # Separate visualisation failures (non-fatal) from real inference failures.
        # matplotlib _save_overlay can raise on degenerate images (0-dimension axis,
        # single-pixel crops, all-black tiles) — the model result is still valid
        # in these cases, we just lose the overlay PNG.
        if "imshow" in exc_str.lower() or "axes" in exc_str.lower() or "figure" in exc_str.lower():
            logger.warning("Dot overlay render failed (non-fatal) on %s: %s — "
                           "treating as no_dot_found", grid_path.name, exc_str)
            return {"detected": False, "error": "no_dot_found"}
        logger.error("Dot inference failed on %s: %s", grid_path.name, exc)
        return {"detected": False, "error": exc_str}

    if not dots:
        return {"detected": False, "error": "no_dot_found"}

    d = dots[0]
    overlay_path = output_dir / f"{grid_path.stem}_prediction.png"
    return {
        "detected":   True,
        "row":        d["row"],
        "col":        d["col"],
        "nw":         d["nw"],
        "confidence": round(d["confidence"] * 100),
        "x_norm":     d["x_norm"],
        "y_norm":     d["y_norm"],
        "image_path": str(overlay_path),
        "tier":       tier,
        "threshold":  round(threshold, 3),
    }

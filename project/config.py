import os
from pathlib import Path

# -- Credentials ---------------------------------------------------------------
# Credentials are loaded from environment variables only, NEVER from a path
# baked into the repo. Set GOOGLE_APPLICATION_CREDENTIALS to the GCP
# service-account JSON path; GOOGLE_API_KEY to your Gemini key.
#
# Local dev fallback: if a `credentials/` directory exists next to the project
# root and contains exactly one *.json file, use it. This keeps developer
# machines working without committing the customer's credential filename.
_HERE = Path(__file__).parent
_LOCAL_CREDS_DIR = _HERE.parent / "credentials"

if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    if _LOCAL_CREDS_DIR.is_dir():
        _candidates = sorted(_LOCAL_CREDS_DIR.glob("*.json"))
        if len(_candidates) == 1:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_candidates[0])

# -- Models --------------------------------------------------------------------
# gemini-2.0-flash-lite: best free-tier model (30 RPM, fast, cheap).
# Override via env vars: GEMINI_FLASH_MODEL / GEMINI_PRO_MODEL.
# Both default to flash-lite — Pro has 0 free-tier quota and is unnecessary
# for county classification (simple 77-class task).
MODEL_FLASH_NAME = os.environ.get("GEMINI_FLASH_MODEL", "gemini-2.0-flash-lite")
MODEL_PRO_NAME   = os.environ.get("GEMINI_PRO_MODEL",   "gemini-2.0-flash-lite")

# -- Source / Output Paths -----------------------------------------------------
# Override with environment variables for cloud / Docker deployments.
# Local Windows dev: falls back to D:\ defaults.
# Linux / Docker / CI: falls back to /tmp paths so the container works without
# any env var set (though run_batch_job.py always sets OUTPUT_ROOT=/tmp/output).
import sys as _sys
_DEFAULT_SOURCE = r"D:" if _sys.platform == "win32" else str(Path.home())
_DEFAULT_OUTPUT = r"D:\project_outputs" if _sys.platform == "win32" else "/tmp/output"
del _sys
SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", _DEFAULT_SOURCE))
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", _DEFAULT_OUTPUT))

# Filenames only — callers that need a full path do output_root / CONSTANT.
DATASET_INDEX_CSV     = "dataset_index.csv"
PROCESSING_STATUS_CSV = "processing_status.csv"

# Sub-directory names inside any output_root
GRIDS_SUBDIR         = "grids"
LOCATIONS_SUBDIR     = "locations"
COUNTIES_SUBDIR      = "counties"
DOTS_SUBDIR          = "dots"
METADATA_SUBDIR      = "metadata"
LOGS_SUBDIR          = "logs"
MANUAL_REVIEW_SUBDIR = "manual_review"

# Absolute paths for local (non-cloud) runs — derived from OUTPUT_ROOT
GRIDS_DIR          = OUTPUT_ROOT / GRIDS_SUBDIR
LOCATIONS_DIR      = OUTPUT_ROOT / LOCATIONS_SUBDIR
COUNTIES_DIR       = OUTPUT_ROOT / COUNTIES_SUBDIR
DOTS_DIR           = OUTPUT_ROOT / DOTS_SUBDIR
METADATA_DIR       = OUTPUT_ROOT / METADATA_SUBDIR
LOGS_DIR           = OUTPUT_ROOT / LOGS_SUBDIR
MANUAL_REVIEW_DIR  = OUTPUT_ROOT / MANUAL_REVIEW_SUBDIR
FAILED_RECORDS_CSV = MANUAL_REVIEW_DIR / "failed_records.csv"

# -- Stages --------------------------------------------------------------------
STAGE_LATLONG  = "latlong"   # runs first; if coords found, grid+location skipped
STAGE_GRID     = "grid"
STAGE_LOCATION = "location"
STAGE_COUNTY   = "county"
STAGE_DOT      = "dot"       # U-Net dot detection on saved grid image
ALL_STAGES     = (STAGE_LATLONG, STAGE_GRID, STAGE_LOCATION, STAGE_COUNTY, STAGE_DOT)

# -- Page / Crop ---------------------------------------------------------------
PAGE_TO_PROCESS         = 0
EXTEND_LEFT_PIXELS      = 700
EXTEND_RIGHT_PIXELS     = 400
VERTICAL_PADDING_PIXELS = 50
RESOLUTION_MULTIPLIER   = 2

# -- Grid ----------------------------------------------------------------------
GRID_ROWS, GRID_COLS = 8, 8
STD_GRID_SIZE        = 512

# -- Keywords ------------------------------------------------------------------
LOCATION_KEYWORDS = {
    "section":  ["section", "sec", "sec."],
    "township": ["township", "twn", "tvp", "twp"],
    "range":    ["range", "rge"],
}

COUNTY_KEYWORDS = [
    "county", "county.", "county..",
    "count",  "County", "County.", "County..", "Count",
]

# -- County Reference ----------------------------------------------------------
COUNTY_LIST_RAW = [
    'Garfield County',     'Harper County',      'Tillman County',
    'Pushmataha County',   'Beckham County',     'Lincoln County',
    'Kingfisher County',   'Jackson County',     'Delaware County',
    'Greer County',        'Texas County',       'Bryan County',
    'McIntosh County',     'Beaver County',      'Washington County',
    'Coal County',         'Caddo County',       'McCurtain County',
    'Sequoyah County',     'Okmulgee County',    'Blaine County',
    'Cimarron County',     'Osage County',       'Harmon County',
    'Muskogee County',     'Oklahoma County',    'Payne County',
    'Ellis County',        'Mayes County',       'Grady County',
    'Pottawatomie County', 'Jefferson County',   'Major County',
    'Woodward County',     'Kay County',         'McClain County',
    'Choctaw County',      'Adair County',       'Nowata County',
    'Comanche County',     'Custer County',      'Canadian County',
    'Grant County',        'Tulsa County',       'Logan County',
    'Woods County',        'Atoka County',       'Le Flore County',
    'Pontotoc County',     'Hughes County',      'Dewey County',
    'Stephens County',     'Latimer County',     'Okfuskee County',
    'Alfalfa County',      'Love County',        'Cherokee County',
    'Seminole County',     'Noble County',       'Cotton County',
    'Haskell County',      'Garvin County',      'Kiowa County',
    'Cleveland County',    'Pawnee County',      'Marshall County',
    'Creek County',        'Ottawa County',      'Pittsburg County',
    'Roger Mills County',  'Wagoner County',     'Rogers County',
    'Johnston County',     'Murray County',      'Carter County',
    'Craig County',        'Washita County',
]

VALID_COUNTY_LIST_ORIGINAL = [n for n in COUNTY_LIST_RAW if isinstance(n, str)]

COUNTY_LIST_CLEAN = sorted(set(
    n.lower().replace(" county", "").strip()
    for n in VALID_COUNTY_LIST_ORIGINAL
))

COUNTY_MAP_CLEAN_TO_ORIGINAL = {
    n.lower().replace(" county", "").strip(): n
    for n in VALID_COUNTY_LIST_ORIGINAL
}

# -- Thresholds ----------------------------------------------------------------
# County fuzzy-match: accept matches above ACCEPT, but flag for manual review
# anything below REVIEW. Cursive / handwritten county names produce noisy OCR
# that won't clear 86 — lowering the floor lets them through, then the review
# flag asks a human to confirm.
FUZZY_MATCH_THRESHOLD      = 72   # minimum score to record as detected
RETRY_CONFIDENCE_THRESHOLD = 95   # auto-accept on Pass 1 if >= this

LATLONG_REVIEW_BELOW       = 80   # latlong_confidence < this -> needs_review
COUNTY_REVIEW_BELOW        = 86   # county_score < this -> needs_review
GRID_REVIEW_BELOW          = 80   # grid_confidence < this -> needs_review
LOCATION_REVIEW_BELOW      = 100  # any missing field (sec/twp/rng) -> needs_review
DOT_REVIEW_BELOW           = 70   # dot_confidence < this -> needs_review

# Minimum Tesseract word-count to consider a page "readable".
# Pages below this threshold are almost certainly blank or handwritten beyond
# what Tesseract can decode — skip the expensive grouping / Gemini step.
ILLEGIBLE_WORD_THRESHOLD   = 15

# -- Per-stage page caps -------------------------------------------------------
# First-pass page limits (kept small to control API cost). Retry path uses
# the *_RETRY values which scan deeper.
MAX_LATLONG_PAGES        = 2
MAX_LATLONG_PAGES_RETRY  = 99      # scan everything on retry
MAX_COUNTY_PAGES         = 2
MAX_COUNTY_PAGES_RETRY   = 99

# Collections numbered below this never run the latlong stage.
# Inspection data (Jun 2025):
#   Coll 8  → ~1 % of forms have lat/lon  (too rare, skip)
#   Coll 9  → ~10% have lat/lon           (worthwhile — enable)
#   Coll 10 → ~18% have lat/lon           (worthwhile — enable)
#   Coll 11+→ >85% have lat/lon           (dominant)
# Authoritative source is now tier_for() + TIER_CONFIG['run_latlong'].
# This constant is kept for backward compat with older code paths.
LATLONG_MIN_COLLECTION_NUM = 9

# -- Retry heuristics ----------------------------------------------------------
# Crop multiplier used by the county no_match retry strategy.
COUNTY_RETRY_CROP_SCALE  = 1.5
# Location pairing strictness; first pass uses the strict value, retry uses loose.
LOCATION_MIN_OVERLAP     = 0.35
LOCATION_MIN_OVERLAP_RETRY = 0.15
# Grid size band: strict first pass, then relaxed on retry.
#
# Calibrated from Jun 2025 manual inspection of ~700 sampled PDFs across
# all 13 collections (rendered at RESOLUTION_MULTIPLIER=2 → 1224×1584 px):
#
#   T2 medium grids (Colls 1–3, ~1911–1950):  W≈306px  H≈238–253px  AR≈1.21–1.29
#   T3 small grids  (Colls 4–9, ~1951–1987):  W≈147–159px  H≈253–269px  AR≈0.58–0.63
#   Mid-size grids  (Coll 10,   ~1988–2000):  W≈196px  H≈348px  AR≈0.56
#   Returning med   (Coll 11+,  ~2001–2024):  W≈245px  H≈348px  AR≈0.70
#
# All three filters (W, H, AR) were blocking T3 and later grids.
# The density filter (_MIN_LINE_DENSITY) handles false-positive rejection.
GRID_W_STRICT = (120, 900)   # was (280, 850) — T3 minimum ~147px
GRID_H_STRICT = (150, 900)   # was (280, 850) — T3 minimum ~238px
GRID_W_LOOSE  = (90,  1200)  # was (150, 1200) — retry: catch absolute outliers
GRID_H_LOOSE  = (90,  1200)  # was (150, 1200)

# -- Processing ----------------------------------------------------------------
MAX_WORKERS = max(1, os.cpu_count() - 1)


# -- Collection-tier dispatcher ------------------------------------------------
# -- Collection-tier dispatcher ------------------------------------------------
# Four natural breakpoints driven by when the OSU form layout changed:
#
#   Collections  1– 6  (EARLY)       ~1911–1940s
#     sec/twp/rge keywords + standard 8×8 grid; no lat/lon on form.
#   Collections  7– 8  (TRANSITION)  ~1950s
#     Mixed older/newer wells; sec/twp/rge still present but layout varies.
#   Collections  9–10  (MID)         ~1960s–70s
#     "Location:" line dominates; sec/twp/rge keywords less reliable.
#   Collections 11–13  (LATE/MODERN) ~1980s–2024
#     Decimal lat/lon printed on form; "Location:" + coordinates routine.
#
# Use tier_for(collection_num) at any decision point that needs to vary by
# decade. Update the boundaries here as more data arrives — never inline.
TIER_EARLY      = "early"
TIER_TRANSITION = "transition"
TIER_MID        = "mid"
TIER_LATE       = "late"
TIER_MODERN     = "modern"

_TIER_BOUNDARIES = [
    (1,   6,   TIER_EARLY),
    (7,   8,   TIER_TRANSITION),
    (9,   10,  TIER_MID),
    (11,  12,  TIER_LATE),
    (13,  9999, TIER_MODERN),
]

TIER_DESCRIPTIONS = {
    TIER_EARLY:      "Collections 1–6  (1911–1970)  — printed SEC/TWP/RGE, 8×8 dot grid, no lat/lon",
    TIER_TRANSITION: "Collections 7–8  (1971–1982)  — small grid (~13% wide), STR top-center, rare lat/lon",
    TIER_MID:        "Collections 9–10 (1983–2000)  — grid shifts to top-center, lat/lon on ~10-18% of forms",
    TIER_LATE:       "Collections 11–12 (2001–2018) — LOCATE WELL grid top-center/right, lat/lon dominant (>85%)",
    TIER_MODERN:     "Collections 13+  (2019–2024)  — digital forms, decimal degrees standard",
}

# Grid zone hints per tier — where the dot-grid image typically appears.
# Used by the anchor-crop system to set appropriate search margins.
# Values: 'top-left' | 'top-center' | 'top-right' | 'bot-left'
TIER_GRID_ZONES = {
    TIER_EARLY:      ["bot-left", "top-left"],          # Coll 1 bot-left; Colls 2-6 top-left
    TIER_TRANSITION: ["top-left"],                       # Colls 7-8: uniformly top-left
    TIER_MID:        ["top-left", "top-center"],         # Coll 9 mixed; Coll 10 top-center (91%)
    TIER_LATE:       ["top-center", "top-right"],        # Coll 11: center (80%) + right (17%)
    TIER_MODERN:     ["top-left", "top-center", "top-right"],  # insufficient data — allow all
}


def tier_for(collection_num: int | None) -> str:
    """
    Map a collection number to its strategy tier. Unknown/zero values
    fall back to 'early' (the most conservative regex-and-anchor stack).
    """
    if not collection_num:
        return TIER_EARLY
    for lo, hi, name in _TIER_BOUNDARIES:
        if lo <= collection_num <= hi:
            return name
    return TIER_EARLY


def decade_for(year: str | int | None) -> str:
    """
    Return the decade string for a year (e.g. '1911' → '1910s').
    Returns '' if year is empty / unparseable.
    """
    if not year:
        return ""
    try:
        y = int(str(year)[:4])
        return f"{(y // 10) * 10}s"
    except (ValueError, TypeError):
        return ""


# Per-tier flags. Add knobs here as the pipeline grows; consumers should
# look up via tier_for() then index this dict.
TIER_CONFIG = {
    # run_latlong:   attempt lat/lon extraction (Vision OCR on page 1).
    #                False for tiers where <2% of forms carry coordinates — not
    #                worth the extra API call per record.
    # run_location:  attempt SEC/TWP/RGE extraction via OCR keywords.
    #                False for very early tiers where values are handwritten and
    #                OCR quality is too low to be reliable.
    # location_strategy: 'str_keywords'      → look for SEC/TWP/RGE labels
    #                    'location_keyword'  → look for 'Location:' line
    #
    # Jun 2025 calibration from ~700 manually inspected samples:
    #   EARLY:      no lat/lon; SEC/TWP/RGE handwritten → run_location=False
    #   TRANSITION: <2% lat/lon (Coll 8); SEC/TWP/RGE printed → run_location=True
    #   MID:        10-18% lat/lon (Coll 9-10); Location: line dominant
    #   LATE+:      >85% lat/lon; Location: line + coordinates
    TIER_EARLY:      {"run_latlong": False, "run_location": False, "location_strategy": "str_keywords"},
    TIER_TRANSITION: {"run_latlong": False, "run_location": True,  "location_strategy": "str_keywords"},
    TIER_MID:        {"run_latlong": True,  "run_location": True,  "location_strategy": "location_keyword"},
    TIER_LATE:       {"run_latlong": True,  "run_location": True,  "location_strategy": "location_keyword"},
    TIER_MODERN:     {"run_latlong": True,  "run_location": True,  "location_strategy": "location_keyword"},
}

# Keywords used by the new 'Location:' extractor in mid/late/modern tiers.
# Variants account for OCR drops (the colon often isn't recognised).
LOCATION_LINE_KEYWORDS = [
    "location:", "location",  "locality:",  "locality",
    "well location", "surface location", "spot well location",
]

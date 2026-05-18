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
MODEL_FLASH_NAME = "gemini-2.5-flash"
MODEL_PRO_NAME   = "gemini-2.5-pro"

# -- Source / Output Paths -----------------------------------------------------
SOURCE_ROOT  = Path(r"D:")
OUTPUT_ROOT  = Path(r"D:\project_outputs")

DATASET_INDEX_CSV     = OUTPUT_ROOT / "dataset_index.csv"
PROCESSING_STATUS_CSV = OUTPUT_ROOT / "processing_status.csv"

GRIDS_DIR          = OUTPUT_ROOT / "grids"
LOCATIONS_DIR      = OUTPUT_ROOT / "locations"
COUNTIES_DIR       = OUTPUT_ROOT / "counties"
METADATA_DIR       = OUTPUT_ROOT / "metadata"
LOGS_DIR           = OUTPUT_ROOT / "logs"
MANUAL_REVIEW_DIR  = OUTPUT_ROOT / "manual_review"
FAILED_RECORDS_CSV = MANUAL_REVIEW_DIR / "failed_records.csv"

# -- Stages --------------------------------------------------------------------
STAGE_LATLONG  = "latlong"   # runs first; if coords found, grid+location skipped
STAGE_GRID     = "grid"
STAGE_LOCATION = "location"
STAGE_COUNTY   = "county"
ALL_STAGES     = (STAGE_LATLONG, STAGE_GRID, STAGE_LOCATION, STAGE_COUNTY)

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

COUNTY_REVIEW_BELOW        = 86   # county_score < this -> needs_review
GRID_REVIEW_BELOW          = 80   # grid_confidence < this -> needs_review
LOCATION_REVIEW_BELOW      = 100  # any missing field (sec/twp/rng) -> needs_review

# -- Per-stage page caps -------------------------------------------------------
# First-pass page limits (kept small to control API cost). Retry path uses
# the *_RETRY values which scan deeper.
MAX_LATLONG_PAGES        = 2
MAX_LATLONG_PAGES_RETRY  = 99      # scan everything on retry
MAX_COUNTY_PAGES         = 2
MAX_COUNTY_PAGES_RETRY   = 99

# Collections numbered below this never run the latlong stage — manual
# inspection of older ExportedFolderContents (N).zip archives confirms
# decimal-degree coordinates were not yet recorded on the forms. Saves
# one Vision API call per record (~50% of stage time for resume runs).
# Authoritative source is now tier_for() + TIER_CONFIG['run_latlong'].
# This constant is kept for backward compat with older code paths.
LATLONG_MIN_COLLECTION_NUM = 11

# -- Retry heuristics ----------------------------------------------------------
# Crop multiplier used by the county no_match retry strategy.
COUNTY_RETRY_CROP_SCALE  = 1.5
# Location pairing strictness; first pass uses the strict value, retry uses loose.
LOCATION_MIN_OVERLAP     = 0.35
LOCATION_MIN_OVERLAP_RETRY = 0.15
# Grid size band: strict first pass, then relaxed on retry.
GRID_W_STRICT = (280, 850)
GRID_H_STRICT = (280, 850)
GRID_W_LOOSE  = (150, 1200)
GRID_H_LOOSE  = (150, 1200)

# -- Processing ----------------------------------------------------------------
MAX_WORKERS = max(1, os.cpu_count() - 1)


# -- Collection-tier dispatcher ------------------------------------------------
# Manual inspection of the ZIP collections shows the form layout shifts
# every few collections. Each tier names a strategy bundle the pipeline
# can switch on.
#
#   early      (1-6)   sec/twp/rge keywords present + standard grid form
#   transition (7-8)   mixed older / newer wells; sec/twp/rge usually present
#   mid        (9-10)  "Location:" line dominates; sec/twp/rge less reliable
#   late       (11-12) latitude/longitude often printed; "Location:" still common
#   modern     (13+)   decimal degrees + "Location:" routine
#
# Use tier_for(collection_num) at any decision point that needs to vary by
# decade. Update the boundaries here as more data comes in — never inline.
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


# Per-tier flags. Add knobs here as the pipeline grows; consumers should
# look up via tier_for() then index this dict.
TIER_CONFIG = {
    TIER_EARLY:      {"run_latlong": False, "location_strategy": "str_keywords"},
    TIER_TRANSITION: {"run_latlong": False, "location_strategy": "str_keywords"},
    TIER_MID:        {"run_latlong": False, "location_strategy": "location_keyword"},
    TIER_LATE:       {"run_latlong": True,  "location_strategy": "location_keyword"},
    TIER_MODERN:     {"run_latlong": True,  "location_strategy": "location_keyword"},
}

# Keywords used by the new 'Location:' extractor in mid/late/modern tiers.
# Variants account for OCR drops (the colon often isn't recognised).
LOCATION_LINE_KEYWORDS = [
    "location:", "location",  "locality:",  "locality",
    "well location", "surface location", "spot well location",
]

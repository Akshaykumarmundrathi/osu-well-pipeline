import os
from pathlib import Path

# -- Credentials ---------------------------------------------------------------
_HERE = Path(__file__).parent
GOOGLE_CREDS = _HERE.parent / "smiling-breaker-423712-h3-aff7ac746ad4.json"
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(GOOGLE_CREDS))

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

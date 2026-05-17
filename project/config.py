import os
from pathlib import Path

# ── Credentials ───────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
GOOGLE_CREDS = _HERE.parent / "smiling-breaker-423712-h3-aff7ac746ad4.json"
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(GOOGLE_CREDS))

# ── Models ────────────────────────────────────────────────────────────────────
MODEL_FLASH_NAME = "gemini-2.5-flash"
MODEL_PRO_NAME   = "gemini-2.5-pro"

# ── Source / Output Paths ─────────────────────────────────────────────────────
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

# ── Stages ────────────────────────────────────────────────────────────────────
STAGE_GRID     = "grid"
STAGE_LOCATION = "location"
STAGE_COUNTY   = "county"
ALL_STAGES     = (STAGE_GRID, STAGE_LOCATION, STAGE_COUNTY)

# ── Page / Crop ───────────────────────────────────────────────────────────────
PAGE_TO_PROCESS         = 0
EXTEND_LEFT_PIXELS      = 700
EXTEND_RIGHT_PIXELS     = 400
VERTICAL_PADDING_PIXELS = 50
RESOLUTION_MULTIPLIER   = 2

# ── Grid ──────────────────────────────────────────────────────────────────────
GRID_ROWS, GRID_COLS = 8, 8
STD_GRID_SIZE        = 512

# ── Keywords ──────────────────────────────────────────────────────────────────
LOCATION_KEYWORDS = {
    "section":  ["section", "sec", "sec."],
    "township": ["township", "twn", "tvp", "twp"],
    "range":    ["range", "rge"],
}

COUNTY_KEYWORDS = [
    "county", "county.", "county..",
    "count",  "County", "County.", "County..", "Count",
]

# ── County Reference ──────────────────────────────────────────────────────────
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

# ── Thresholds ────────────────────────────────────────────────────────────────
FUZZY_MATCH_THRESHOLD      = 86
RETRY_CONFIDENCE_THRESHOLD = 95

# ── Processing ────────────────────────────────────────────────────────────────
MAX_WORKERS = max(1, os.cpu_count() - 1)

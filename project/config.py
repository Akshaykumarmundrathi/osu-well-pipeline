import os

GOOGLE_CREDS = "smiling-breaker-423712-h3-aff7ac746ad4.json"
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", GOOGLE_CREDS)

# =====================================================
# GEMINI
# =====================================================

MODEL_FLASH_NAME = "gemini-2.5-flash"
MODEL_PRO_NAME   = "gemini-2.5-pro"

# =====================================================
# FOLDERS / FILES
# =====================================================

PDF_FOLDER                 = "pdfs"
LOCATION_OUTPUT_FOLDER     = "location_data_final"
COUNTY_IMAGE_OUTPUT_FOLDER = "county_areas_extended_final"

COUNTY_SUMMARY_CSV = "county_area_extraction_summary_extended.csv"
COUNTY_FINAL_CSV   = "extracted_county_combined_passes.csv"

# =====================================================
# PAGE / CROP
# =====================================================

PAGE_TO_PROCESS         = 0
EXTEND_LEFT_PIXELS      = 700
EXTEND_RIGHT_PIXELS     = 400
VERTICAL_PADDING_PIXELS = 50

# =====================================================
# GRID
# =====================================================

GRID_ROWS, GRID_COLS = 8, 8
STD_GRID_SIZE        = 512

# =====================================================
# KEYWORDS
# =====================================================

LOCATION_KEYWORDS = {
    "section":  ["section", "sec", "sec."],
    "township": ["township", "twn", "tvp", "twp"],
    "range":    ["range", "rge"],
}

COUNTY_KEYWORDS = [
    "county", "county.", "county..",
    "count",  "County", "County.", "County..", "Count",
]

# =====================================================
# COUNTY REFERENCE DATA
# =====================================================

COUNTY_LIST_RAW = [
    'Garfield County',     'Harper County',      'Tillman County',
    'Pushmataha County',   'Beckham County',      'Lincoln County',
    'Kingfisher County',   'Jackson County',      'Delaware County',
    'Greer County',        'Texas County',        'Bryan County',
    'McIntosh County',     'Beaver County',       'Washington County',
    'Coal County',         'Caddo County',        'McCurtain County',
    'Sequoyah County',     'Okmulgee County',     'Blaine County',
    'Cimarron County',     'Osage County',        'Harmon County',
    'Muskogee County',     'Oklahoma County',     'Payne County',
    'Ellis County',        'Mayes County',        'Grady County',
    'Pottawatomie County', 'Jefferson County',    'Major County',
    'Woodward County',     'Kay County',          'McClain County',
    'Choctaw County',      'Adair County',        'Nowata County',
    'Comanche County',     'Custer County',       'Canadian County',
    'Grant County',        'Tulsa County',        'Logan County',
    'Woods County',        'Atoka County',        'Le Flore County',
    'Pontotoc County',     'Hughes County',       'Dewey County',
    'Stephens County',     'Latimer County',      'Okfuskee County',
    'Alfalfa County',      'Love County',         'Cherokee County',
    'Seminole County',     'Noble County',        'Cotton County',
    'Haskell County',      'Garvin County',       'Kiowa County',
    'Cleveland County',    'Pawnee County',       'Marshall County',
    'Creek County',        'Ottawa County',       'Pittsburg County',
    'Roger Mills County',  'Wagoner County',      'Rogers County',
    'Johnston County',     'Murray County',       'Carter County',
    'Craig County',        'Washita County',
]

VALID_COUNTY_LIST_ORIGINAL = [
    name for name in COUNTY_LIST_RAW if isinstance(name, str)
]

COUNTY_LIST_CLEAN = sorted(
    set(
        name.lower().replace(" county", "").strip()
        for name in VALID_COUNTY_LIST_ORIGINAL
    )
)

COUNTY_MAP_CLEAN_TO_ORIGINAL = {
    name.lower().replace(" county", "").strip(): name
    for name in VALID_COUNTY_LIST_ORIGINAL
}

# =====================================================
# FUZZY THRESHOLDS
# =====================================================

FUZZY_MATCH_THRESHOLD      = 86
RETRY_CONFIDENCE_THRESHOLD = 95

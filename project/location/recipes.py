"""
Per-collection ground-truth search regions — derived from MANUAL REVIEW.

Source: D:\\inspection_pdfs\\_notes.csv — 2,781 records hand-reviewed
(2 files/month/year/collection), with boxes drawn around the grid, county
and STR areas on page 1.  2,269 grid / 2,262 county / 2,261 STR boxes.

The envelopes below are the p5-p95 of the drawn boxes per collection,
in page-relative coordinates (x0, y0, x1, y1 as fractions of page size).
They are *measured*, not assumed — they correct, among other things, the
upper_right zone filter whose x>0.33 cutoff clipped the left edge of the
STR band on Collections 4-9 (drawn boxes start at x=0.18-0.29).

Usage contract (per ASSUMPTION_AUDIT.md): these regions BOUND the first
search pass; the caller must keep a full-page fallback for misses.
PAD expands each envelope edge to absorb form-print drift and the p5/p95
clipping.

Collections 12-13 have too few drawn boxes (1-3) — no recipe; the generic
zone hints + fallback apply.  C13 is gridless by design (text lat/lon).
"""

# Padding applied to every envelope edge at use time (fraction of page).
PAD = 0.04

# (x0, y0, x1, y1) page-relative p5-p95 of hand-drawn STR boxes.
STR_ENVELOPES: dict[int, tuple[float, float, float, float]] = {
    1:  (0.332, 0.082, 0.918, 0.244),   # n=350
    2:  (0.371, 0.039, 0.978, 0.228),   # n=355
    3:  (0.326, 0.050, 0.970, 0.145),   # n=240
    4:  (0.277, 0.050, 0.956, 0.138),   # n=165
    5:  (0.290, 0.077, 0.508, 0.109),   # n=120
    6:  (0.276, 0.056, 0.535, 0.125),   # n=239
    7:  (0.254, 0.061, 0.519, 0.128),   # n=214
    8:  (0.254, 0.073, 0.510, 0.150),   # n=70
    9:  (0.184, 0.088, 0.496, 0.210),   # n=105
    10: (0.179, 0.127, 0.798, 0.260),   # n=252
    11: (0.164, 0.140, 0.449, 0.258),   # n=147
}

# Hand-drawn county boxes.
COUNTY_ENVELOPES: dict[int, tuple[float, float, float, float]] = {
    1:  (0.021, 0.062, 0.518, 0.239),   # n=351
    2:  (0.048, 0.041, 0.646, 0.228),   # n=355
    3:  (0.208, 0.047, 0.581, 0.142),   # n=240
    4:  (0.174, 0.053, 0.583, 0.139),   # n=165
    5:  (0.190, 0.084, 0.276, 0.109),   # n=120
    6:  (0.170, 0.057, 0.323, 0.123),   # n=239
    7:  (0.153, 0.074, 0.321, 0.127),   # n=214
    8:  (0.153, 0.080, 0.300, 0.159),   # n=70
    9:  (0.036, 0.081, 0.300, 0.212),   # n=105
    10: (0.007, 0.135, 0.239, 0.254),   # n=252
    11: (0.003, 0.140, 0.221, 0.255),   # n=147
}

# Hand-drawn grid boxes — useful as a detection prior / false-positive check.
GRID_ENVELOPES: dict[int, tuple[float, float, float, float]] = {
    1:  (0.038, 0.124, 0.451, 0.990),   # n=351 (tall: incl. bottom-left era)
    2:  (0.034, 0.025, 0.423, 0.966),   # n=355
    3:  (0.049, 0.037, 0.346, 0.283),   # n=240
    4:  (0.043, 0.028, 0.351, 0.288),   # n=165
    5:  (0.053, 0.100, 0.184, 0.267),   # n=120
    6:  (0.040, 0.077, 0.212, 0.284),   # n=239
    7:  (0.022, 0.094, 0.202, 0.285),   # n=214
    8:  (0.022, 0.102, 0.199, 0.330),   # n=70
    9:  (0.046, 0.134, 0.580, 0.349),   # n=107
    10: (0.141, 0.093, 0.587, 0.940),   # n=253
    11: (0.343, 0.080, 0.934, 0.378),   # n=151
}


def str_region(collection_num: int | None,
               page_w: int, page_h: int) -> tuple[int, int, int, int] | None:
    """Padded pixel region where the STR is expected, or None if no recipe."""
    env = STR_ENVELOPES.get(collection_num or 0)
    if env is None:
        return None
    x0, y0, x1, y1 = env
    return (max(0, int((x0 - PAD) * page_w)),
            max(0, int((y0 - PAD) * page_h)),
            min(page_w, int((x1 + PAD) * page_w)),
            min(page_h, int((y1 + PAD) * page_h)))


def county_region(collection_num: int | None,
                  page_w: int, page_h: int) -> tuple[int, int, int, int] | None:
    """Padded pixel region where the county is expected, or None."""
    env = COUNTY_ENVELOPES.get(collection_num or 0)
    if env is None:
        return None
    x0, y0, x1, y1 = env
    return (max(0, int((x0 - PAD) * page_w)),
            max(0, int((y0 - PAD) * page_h)),
            min(page_w, int((x1 + PAD) * page_w)),
            min(page_h, int((y1 + PAD) * page_h)))

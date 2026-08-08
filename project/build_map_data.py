"""
build_map_data.py — Build well_locations.json and update the GitHub Pages map
=============================================================================

Pipeline:
  1. Reads dot_coordinates.csv  (output of run_coord_enrichment.py)
     • resolved_lat / resolved_lon  = bilinear-interpolated exact dot lat/lon
     • cell corners + cell_rel_x/y  = full audit trail
  2. For every resolved record, builds a GeoJSON Feature:
     • geometry = [dot_lon, dot_lat]   (the ink-dot position, not cell centre)
     • properties = all legible pipeline fields
  3. Writes well_locations.json → D:\\project_modular\\docs\\data\\
  4. Patches docs/index.html  (replaces embedded INLINE_GEOJSON with local fetch)
  5. Optionally: git add + commit + push  (--push flag)

Usage
-----
  python build_map_data.py                         # build + patch only
  python build_map_data.py --push                  # also commit & push to GitHub
  python build_map_data.py --enrich                # run coord enrichment first
  python build_map_data.py --output D:\\project_outputs_local
  python build_map_data.py --repo   D:\\project_modular

Prerequisites
-------------
  run_coord_enrichment.py must have been run first, OR use --enrich to run it now.
  The repo at --repo must be the cloned osu-well-pipeline GitHub repo.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Defaults ───────────────────────────────────────────────────────────────────
_DEFAULT_OUTPUT = Path(os.environ.get(
    "OUTPUT_ROOT",
    str(Path(__file__).parent.parent / "project_outputs_local"),
))
_DEFAULT_REPO = Path(os.environ.get(
    "REPO_ROOT",
    str(Path(__file__).parent.parent),
))

# Oklahoma bounding box (generous)
_LAT_MIN, _LAT_MAX = 33.5, 37.1
_LON_MIN, _LON_MAX = -103.1, -94.4

# Resolution source codes from plss_resolver — ordered best→worst precision
_RESOLVED_SOURCES = {
    "quadrant_direct",          # cell-level exact (best)
    "exact_county",             # section bbox + county match
    "exact_no_county",
    "county_constrained",
    "ns_fallback", "ew_fallback", "ns_ew_fallback",
    "county_stripped",
    "section_adjacent",         # section ±1 (least precise)
}

# ── Stem → (collection, year, month) backfill index ─────────────────────────────
# Some coordinate sources (C2 redo/regen shards, county-pinned re-resolutions)
# drop collection/year/month from dot_coordinates.csv. Without those three the
# S3 PDF URL can't be built and the map shows "PDF not yet on S3" even though the
# PDF IS on S3. We recover them by pdf_stem from dataset_index.csv (authoritative,
# one row per source PDF), falling back to processing_status.csv.
_STEM_META: dict | None = None

def _load_stem_meta(output_root: Path) -> dict:
    global _STEM_META
    if _STEM_META is not None:
        return _STEM_META
    _STEM_META = {}
    csv.field_size_limit(2_000_000)
    for fname in ("dataset_index.csv", "processing_status.csv"):
        path = output_root / fname
        if not path.exists():
            continue
        try:
            with path.open(newline="", encoding="utf-8", errors="replace") as f:
                for r in csv.DictReader(f):
                    st = r.get("pdf_stem") or r.get("stem") or ""
                    coll, yr, mo = (r.get("collection", ""), r.get("year", ""),
                                    r.get("month", ""))
                    if st and coll and yr and mo and st not in _STEM_META:
                        _STEM_META[st] = (coll, yr, mo)
        except Exception as exc:
            print(f"  WARNING: stem-meta load failed for {path.name}: {exc}")
    print(f"  Stem-meta backfill index: {len(_STEM_META):,} stems")
    return _STEM_META


# ── Geometry helpers ────────────────────────────────────────────────────────────

def _safe_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "", "None", "nan") else None
    except (ValueError, TypeError):
        return None


def _bilinear(tl_lon, tl_lat, tr_lon, tr_lat,
               bl_lon, bl_lat, br_lon, br_lat,
               rx: float, ry: float) -> tuple[float, float]:
    """
    Bilinear interpolation within a quad cell.
    rx 0→left/west, ry 0→top/north.
    Returns (lat, lon).
    """
    lon = (tl_lon*(1-rx)*(1-ry) + tr_lon*rx*(1-ry) +
           bl_lon*(1-rx)*ry     + br_lon*rx*ry)
    lat = (tl_lat*(1-rx)*(1-ry) + tr_lat*rx*(1-ry) +
           bl_lat*(1-rx)*ry     + br_lat*rx*ry)
    return round(lat, 7), round(lon, 7)


def _dot_latlon(row: dict) -> tuple[float | None, float | None]:
    """
    Return (dot_lat, dot_lon) for a row from dot_coordinates.csv.

    Primary:   resolved_lat / resolved_lon   (already bilinear-interpolated by
               plss_resolver using cell corners + cell_rel_x/y — this IS the
               exact ink-dot position within the PLSS cell)

    Fallback:  recompute from cell corners + cell_rel_x/y if primary is missing
               (handles edge cases where the CSV field was blank)
    """
    lat = _safe_float(row.get("resolved_lat"))
    lon = _safe_float(row.get("resolved_lon"))

    if lat is not None and lon is not None:
        if _LAT_MIN <= lat <= _LAT_MAX and _LON_MIN <= lon <= _LON_MAX:
            return lat, lon

    # Fallback: recompute bilinear from stored cell corners
    try:
        tl_lon = _safe_float(row["cell_tl_lon"]); tl_lat = _safe_float(row["cell_tl_lat"])
        tr_lon = _safe_float(row["cell_tr_lon"]); tr_lat = _safe_float(row["cell_tr_lat"])
        bl_lon = _safe_float(row["cell_bl_lon"]); bl_lat = _safe_float(row["cell_bl_lat"])
        br_lon = _safe_float(row["cell_br_lon"]); br_lat = _safe_float(row["cell_br_lat"])
        rx     = _safe_float(row["cell_rel_x"])
        ry     = _safe_float(row["cell_rel_y"])
        if None not in (tl_lon, tl_lat, tr_lon, tr_lat,
                        bl_lon, bl_lat, br_lon, br_lat, rx, ry):
            lat, lon = _bilinear(tl_lon, tl_lat, tr_lon, tr_lat,
                                  bl_lon, bl_lat, br_lon, br_lat, rx, ry)
            if _LAT_MIN <= lat <= _LAT_MAX and _LON_MIN <= lon <= _LON_MAX:
                return lat, lon
    except (KeyError, TypeError):
        pass

    return None, None


# ── GeoJSON builder ─────────────────────────────────────────────────────────────

def _well_name(pdf_stem: str) -> str:
    """Extract human-readable name from '11117524_A FRANKLIN 4_1031125' → 'A FRANKLIN 4'."""
    parts = pdf_stem.split("_")
    return parts[1] if len(parts) >= 2 else pdf_stem


def _clean_county(raw) -> str:
    """Normalise county to canonical 'Creek' form. Raw values mix case and an
    optional ' County' suffix ('creek', 'Creek County', 'CREEK'), which split
    one county into several site entries (site showed 148 counties vs 77)."""
    s = str(raw).strip() if raw not in (None, "", "None") else ""
    if not s:
        return ""
    s = re.sub(r"\s+county\.?$", "", s, flags=re.I).strip()
    return s.title()


def _clean_collection(raw: str) -> str:
    return (raw.replace("ExportedFolderContents ", "")
               .replace("ExportedFolderContents_", "")
               .replace(".zip", "").strip())


def _decade_label(year_str: str) -> str:
    try:
        y = int(year_str)
        d = (y // 10) * 10
        return f"{d}s"
    except (ValueError, TypeError):
        return ""


def _build_feature(row: dict, dot_lat: float, dot_lon: float) -> dict:
    """Convert one dot_coordinates row to a GeoJSON Feature."""

    def _pct(v):
        try: return int(float(v or 0))
        except (ValueError, TypeError): return 0

    def _str(v):
        return str(v).strip() if v not in (None, "", "None") else ""

    stem            = _str(row.get("pdf_stem"))
    year            = _str(row.get("year"))
    month           = _str(row.get("month"))
    collection_raw  = _str(row.get("collection"))
    dot_conf        = _pct(row.get("dot_confidence"))

    # Backfill missing collection/year/month from the stem index so the S3 URL
    # can be built (the PDF exists; only this metadata was dropped upstream).
    if not (collection_raw and year and month) and _STEM_META:
        meta = _STEM_META.get(stem)
        if meta:
            collection_raw = collection_raw or meta[0]
            year           = year or meta[1]
            month          = month or meta[2]

    props = {
        # Identity
        "well_name":          _well_name(stem),
        "pdf_stem":           stem,
        "collection":         _clean_collection(collection_raw),
        "year":               year,
        "month":              month,
        "decade":             _str(row.get("decade")) or _decade_label(year),
        # Location
        "county":             _clean_county(row.get("county_name")),
        "section":            _str(row.get("section")),
        "township":           _str(row.get("township")),
        "range":              _str(row.get("range")),
        # Dot coordinates (bilinear-interpolated ink-dot position). Not
        # duplicated under a separate dot_lat/dot_lon key — lat/lon (used by
        # the map JS) and geometry.coordinates already carry this value;
        # a third copy on all 51k+ features was pure size with no reader.
        "lat":                dot_lat,
        "lon":                dot_lon,
        "confidence":         dot_conf,
        # Resolution provenance
        "resolution":         _str(row.get("resolution_source")),
        "quadrant":           _str(row.get("effective_quad"))
                              or _str(row.get("unet_nw"))
                              or _str(row.get("ocr_quadrant_db")),
        # Dot detector
        "dot_confidence":     dot_conf,
        # PLSS quadrant cell corners — four corners of the ~200-acre cell
        # Used by the map to draw a highlight polygon showing the certain area
        # (even if the exact dot position is uncertain, the cell is definitive)
        # GeoJSON convention: [lon, lat]
        "cell_tl_lat":        _safe_float(row.get("cell_tl_lat")),
        "cell_tl_lon":        _safe_float(row.get("cell_tl_lon")),
        "cell_tr_lat":        _safe_float(row.get("cell_tr_lat")),
        "cell_tr_lon":        _safe_float(row.get("cell_tr_lon")),
        "cell_bl_lat":        _safe_float(row.get("cell_bl_lat")),
        "cell_bl_lon":        _safe_float(row.get("cell_bl_lon")),
        "cell_br_lat":        _safe_float(row.get("cell_br_lat")),
        "cell_br_lon":        _safe_float(row.get("cell_br_lon")),
        # NOTE: the PDF's S3 URL is intentionally NOT stored per-feature.
        # docs/index.html reconstructs it client-side (buildPdfUrl()) from
        # collection/year/month/pdf_stem, which are already present below —
        # storing the full URL on all 51k+ features repeated the AWS account
        # ID that many times in a public file for no benefit (~4.6MB of pure
        # redundancy). _s3_pdf_url() is kept for other callers/scripts.
        # Flags
        "flags":              _str(row.get("flags")),
    }

    return {
        "type":     "Feature",
        "geometry": {"type": "Point", "coordinates": [dot_lon, dot_lat]},
        "properties": props,
    }


def build_geojson(dot_csv: Path) -> tuple[dict, int, int]:
    """
    Read dot_coordinates.csv → GeoJSON FeatureCollection.
    Returns (geojson, n_included, n_skipped).
    """
    features  = []
    n_skipped = 0

    # Build the stem→meta backfill index from the same output root as dot_csv.
    _load_stem_meta(dot_csv.parent)

    print(f"Reading {dot_csv} ...")
    with dot_csv.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"  {len(rows):,} total rows")

    for row in rows:
        # Validity is decided by the COORDINATES (present + inside Oklahoma,
        # enforced in _dot_latlon) — not by resolution-source vocabulary.
        # The old _RESOLVED_SOURCES allowlist silently dropped every row whose
        # source label post-dated the list (county_pinned_*, corrected_*,
        # direct_latlong, p4_*, centroids): label evolution must never cost
        # wells. The source string still rides along as a popup property.
        dot_lat, dot_lon = _dot_latlon(row)
        if dot_lat is None:
            n_skipped += 1
            continue

        features.append(_build_feature(row, dot_lat, dot_lon))

    geojson = {
        "type":       "FeatureCollection",
        "generated":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "well_count": len(features),
        "features":   features,
    }
    return geojson, len(features), n_skipped


# ── index.html patch ────────────────────────────────────────────────────────────

_DATA_URL_INJECT = "const DATA_URL = './data/well_locations.json';"

# --------------------------------------------------------------------------
# Slide-out PDF panel CSS (injected into <style> block)
# --------------------------------------------------------------------------
_PDF_PANEL_CSS = """
  /* ── PDF slide-out panel ── */
  #pdf-panel {
    position: fixed; right: -660px; top: 0;
    width: 650px; height: 100vh;
    background: #0a1223;
    border-left: 1px solid rgba(96,165,250,.3);
    z-index: 3000;
    transition: right .35s cubic-bezier(.4,0,.2,1);
    display: flex; flex-direction: column;
    box-shadow: -8px 0 40px rgba(0,0,0,.65);
  }
  #pdf-panel.open { right: 0; }
  #pdf-panel-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.35); z-index: 2999;
  }
  #pdf-panel-overlay.show { display: block; }
  .pdf-panel-hdr {
    display: flex; align-items: center; gap: 10px;
    padding: 12px 16px; flex-shrink: 0;
    background: linear-gradient(135deg,#1B4F8A,#163d6e);
    border-bottom: 1px solid rgba(96,165,250,.2);
  }
  .pdf-panel-hdr .pdf-title {
    flex: 1; color: #fff; font-size: 13px; font-weight: 600;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .pdf-panel-hdr a.pdf-ext {
    color: #93c5fd; font-size: 11px; text-decoration: none;
    padding: 4px 8px; border: 1px solid rgba(96,165,250,.3);
    border-radius: 5px; white-space: nowrap;
  }
  .pdf-panel-hdr a.pdf-ext:hover { background: rgba(96,165,250,.15); }
  .pdf-panel-close {
    background: rgba(255,255,255,.1); border: none; color: #fff;
    font-size: 17px; cursor: pointer; border-radius: 6px;
    width: 30px; height: 30px; display: flex; align-items: center;
    justify-content: center; transition: background .15s; flex-shrink: 0;
  }
  .pdf-panel-close:hover { background: rgba(255,255,255,.2); }
  #pdf-panel-iframe { flex: 1; width: 100%; border: none; background: #fff; }
  .pdf-no-url {
    flex: 1; display: flex; align-items: center; justify-content: center;
    color: #6b7e99; font-size: 13px; text-align: center; padding: 24px;
  }
"""

# --------------------------------------------------------------------------
# Slide-out PDF panel HTML (injected before </body>)
# --------------------------------------------------------------------------
_PDF_PANEL_HTML = """
<!-- PDF slide-out panel -->
<div id="pdf-panel-overlay" onclick="closePdfPanel()"></div>
<div id="pdf-panel">
  <div class="pdf-panel-hdr">
    <span class="pdf-title" id="pdf-panel-title">Well Record PDF</span>
    <a id="pdf-panel-ext" class="pdf-ext" href="#" target="_blank" rel="noopener"
       style="display:none">&#8599; Open in tab</a>
    <button class="pdf-panel-close" onclick="closePdfPanel()" title="Close">&#215;</button>
  </div>
  <iframe id="pdf-panel-iframe" src="" frameborder="0" allowfullscreen></iframe>
  <div class="pdf-no-url" id="pdf-no-url" style="display:none">
    PDF not yet uploaded to S3 for this record.
  </div>
</div>
"""

# --------------------------------------------------------------------------
# JavaScript for the panel (injected just before </script>)
# --------------------------------------------------------------------------
_PDF_PANEL_JS = """
// ─── PDF slide-out panel ─────────────────────────────────────────────────────
function openPdfPanel(url, title) {
  const panel   = document.getElementById('pdf-panel');
  const overlay = document.getElementById('pdf-panel-overlay');
  const iframe  = document.getElementById('pdf-panel-iframe');
  const noUrl   = document.getElementById('pdf-no-url');
  const titleEl = document.getElementById('pdf-panel-title');
  const extLink = document.getElementById('pdf-panel-ext');

  titleEl.textContent = title || 'Well Record PDF';

  if (url) {
    iframe.src         = url;
    iframe.style.display = 'block';
    noUrl.style.display  = 'none';
    extLink.href         = url;
    extLink.style.display = 'inline-flex';
  } else {
    iframe.src         = '';
    iframe.style.display = 'none';
    noUrl.style.display  = 'block';
    extLink.style.display = 'none';
  }

  panel.classList.add('open');
  overlay.classList.add('show');
}

function closePdfPanel() {
  const panel   = document.getElementById('pdf-panel');
  const overlay = document.getElementById('pdf-panel-overlay');
  panel.classList.remove('open');
  overlay.classList.remove('show');
  // Unload iframe after animation to free memory
  setTimeout(() => {
    document.getElementById('pdf-panel-iframe').src = '';
  }, 380);
}

// Close panel on Escape key
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closePdfPanel();
});
"""


def patch_index_html(index_html: Path) -> bool:
    """
    Patch docs/index.html for the external-data + S3 PDF panel workflow:

    1. Replace large inline INLINE_GEOJSON with null + DATA_URL
    2. Fix S3 URL construction to use feature.s3_url property
    3. Inject slide-out PDF panel CSS, HTML, and JavaScript
    4. Update popup 'View PDF' button to open the panel

    Returns True if the file was changed, False if already fully patched.
    """
    if not index_html.exists():
        print(f"  WARNING: {index_html} not found — skipping patch")
        return False

    print(f"Patching {index_html} ...")
    content = index_html.read_text(encoding="utf-8")
    original = content

    # ── 1. Replace INLINE_GEOJSON ─────────────────────────────────────────────
    inline_pat = re.compile(
        r"(const|var|let)\s+INLINE_GEOJSON\s*=\s*(?!\s*null).*?;",
        re.DOTALL
    )
    if inline_pat.search(content):
        content = inline_pat.sub("const INLINE_GEOJSON = null;", content, count=1)
        print("  Replaced INLINE_GEOJSON with null.")

    # ── 1b. Remove old inline loadData that crashes on null.INLINE_GEOJSON ───
    # The HTML may have a legacy loadData that does `geojson = INLINE_GEOJSON;`
    # followed by `geojson.features` — null.features throws. Remove it entirely;
    # the correct loadData (further down in the script) handles null gracefully.
    old_inline_load = re.compile(
        r"async function loadData\(\)\s*\{[^}]*const geojson\s*=\s*INLINE_GEOJSON;"
        r".*?allFeatures\s*=\s*geojson\.features.*?\}",
        re.DOTALL
    )
    if old_inline_load.search(content):
        content = old_inline_load.sub(
            "// loadData defined below (fetch from DATA_URL with null-INLINE_GEOJSON support)",
            content, count=1,
        )
        print("  Removed legacy inline loadData (null crash prevention).")

    # ── 2. Inject or update DATA_URL ─────────────────────────────────────────
    data_url_pat = re.compile(r"(const|var|let)\s+DATA_URL\s*=\s*['\"].*?['\"];")
    if data_url_pat.search(content):
        content = data_url_pat.sub(_DATA_URL_INJECT, content, count=1)
    elif "const INLINE_GEOJSON = null;" in content:
        content = content.replace(
            "const INLINE_GEOJSON = null;",
            "const INLINE_GEOJSON = null;\n" + _DATA_URL_INJECT,
            1,
        )
        print("  Injected DATA_URL.")

    # ── 3b. Fix orphan ${pdfS3} reference (remove stale var after patch) ─────
    if '${pdfS3}' in content:
        content = content.replace('${pdfS3}', '${pdfUrl}')
        print("  Fixed orphan pdfS3 reference -> pdfUrl.")

    # ── 4. Change popup 'View PDF' button → open panel ───────────────────────
    old_pdf_btn = (
        '  <a class="pdf-link" href="${pdfUrl}" target="_blank" rel="noopener">'
        "\U0001F4C4 View Original Well Record PDF</a>"
    )
    new_pdf_btn = (
        '  ${pdfUrl\n'
        "    ? `<button class=\"pdf-link\" style=\"cursor:pointer;border:none;width:100%\"\n"
        '       onclick="openPdfPanel(\'${pdfUrl}\',\'${wellName || p.pdf_stem}\')">\n'
        "       \U0001F4C4 View Original Well Record PDF</button>`\n"
        "    : '<div style=\"text-align:center;color:#6b7e99;font-size:11px;padding:6px\">"
        "PDF not yet on S3</div>'}"
    )
    if old_pdf_btn in content:
        content = content.replace(old_pdf_btn, new_pdf_btn, 1)
        print("  Updated popup PDF button -> slide-out panel.")

    # ── 5. Inject CSS (before closing </style>) ───────────────────────────────
    css_sentinel = "/* ── PDF slide-out panel ── */"
    if css_sentinel not in content:
        content = content.replace("</style>", _PDF_PANEL_CSS + "\n</style>", 1)
        print("  Injected PDF panel CSS.")

    # ── 6. Inject HTML panel (before </body>) ────────────────────────────────
    html_sentinel = 'id="pdf-panel"'
    if html_sentinel not in content:
        content = content.replace("</body>", _PDF_PANEL_HTML + "\n</body>", 1)
        print("  Injected PDF panel HTML.")

    # ── 7. Inject JS functions (before closing </script>) ────────────────────
    js_sentinel = "function openPdfPanel("
    if js_sentinel not in content:
        content = content.replace(
            "initMap();\nloadData();\n</script>",
            _PDF_PANEL_JS + "\ninitMap();\nloadData();\n</script>",
            1,
        )
        print("  Injected PDF panel JavaScript.")

    if content != original:
        index_html.write_text(content, encoding="utf-8")
        print("  index.html updated.")
        return True

    print("  index.html already fully patched -- no changes needed.")
    return False


# ── Git operations ──────────────────────────────────────────────────────────────

def git_push(repo: Path, message: str) -> bool:
    """Stage docs/data/well_locations.json + docs/index.html, commit, push."""
    def _run(cmd, **kw):
        result = subprocess.run(
            cmd, cwd=str(repo), capture_output=True, text=True, **kw
        )
        if result.stdout.strip():
            print(" ", result.stdout.strip())
        if result.stderr.strip():
            print(" ", result.stderr.strip())
        return result.returncode == 0

    print(f"\nPushing to GitHub ({repo}) ...")

    _run(["git", "add",
          "docs/data/well_locations.json",
          "docs/index.html"])

    # Check if there's anything to commit
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True
    )
    if not status.stdout.strip():
        print("  Nothing to commit -- already up to date.")
        return True

    ok = _run(["git", "commit", "-m", message])
    if not ok:
        print("  Commit failed.")
        return False

    ok = _run(["git", "push"])
    if ok:
        print("  Pushed OK — GitHub Pages will update in ~1 min.")
        print("  https://akshaykumarmundrathi.github.io/osu-well-pipeline/")
    else:
        print("  Push failed — check credentials / network.")
    return ok


# ── Entry point ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Build well_locations.json and update the GitHub Pages map"
    )
    ap.add_argument("--output",   default=str(_DEFAULT_OUTPUT),
                    help="Pipeline output root containing dot_coordinates.csv")
    ap.add_argument("--repo",     default=str(_DEFAULT_REPO),
                    help="Path to the osu-well-pipeline git repo (has docs/)")
    ap.add_argument("--push",     action="store_true",
                    help="git commit + push to GitHub Pages after building")
    ap.add_argument("--enrich",   action="store_true",
                    help="Run run_coord_enrichment.py first to generate dot_coordinates.csv")
    ap.add_argument("--no-patch", action="store_true",
                    help="Skip patching docs/index.html")
    args = ap.parse_args()

    output_root = Path(args.output)
    repo        = Path(args.repo)
    dot_csv     = output_root / "dot_coordinates.csv"
    out_json    = repo / "docs" / "data" / "well_locations.json"
    index_html  = repo / "docs" / "index.html"

    print("\n-- Build Map Data -------------------------------------------------")
    print(f"  dot_coordinates.csv : {dot_csv}")
    print(f"  well_locations.json : {out_json}")
    print(f"  index.html          : {index_html}")
    print("--------------------------------------------------------------------\n")

    # ── Step 0: run enrichment if requested / needed ──────────────────────────
    if args.enrich or not dot_csv.exists():
        if not dot_csv.exists():
            if not args.enrich:
                print("ERROR: dot_coordinates.csv not found.")
                print("  Run first:  python run_coord_enrichment.py")
                print("  Or use:     python build_map_data.py --enrich")
                sys.exit(1)

        print("Running coordinate enrichment (this queries RDS — may take a few minutes)…\n")
        enricher = Path(__file__).parent / "run_coord_enrichment.py"
        result = subprocess.run(
            [sys.executable, str(enricher), "--output", str(output_root)],
            cwd=str(Path(__file__).parent),
        )
        if result.returncode != 0:
            print("ERROR: enrichment failed — see above.")
            sys.exit(1)
        print()

    if not dot_csv.exists():
        print(f"ERROR: {dot_csv} still not found after enrichment.")
        sys.exit(1)

    # ── Step 1: build GeoJSON ─────────────────────────────────────────────────
    geojson, n_feat, n_skip = build_geojson(dot_csv)

    if n_feat == 0:
        print("WARNING: no resolved records found — GeoJSON will be empty.")

    # ── Step 1b: preserve human-verified/corrected marks from the previous
    # GeoJSON. The corrections GitHub Action writes `verified: "human"`
    # ([VERIFIED-CORRECT] issues, confirming existing data was already right)
    # and `corrected: "human"` (CORRECTION issues that actually changed a
    # field/coordinate) directly into well_locations.json. dot_coordinates.csv
    # has no such columns, so a rebuild would silently erase those marks.
    # Carry both forward by pdf_stem.
    if out_json.exists():
        try:
            prior = json.loads(out_json.read_text(encoding="utf-8"))
            prior_marks = {
                f["properties"].get("pdf_stem"): {
                    "verified": f["properties"].get("verified"),
                    "corrected": f["properties"].get("corrected"),
                }
                for f in prior.get("features", [])
                if f["properties"].get("verified") or f["properties"].get("corrected")
            }
            carried = 0
            for f in geojson["features"]:
                marks = prior_marks.get(f["properties"].get("pdf_stem"))
                if marks:
                    if marks["verified"]:
                        f["properties"]["verified"] = marks["verified"]
                    if marks["corrected"]:
                        f["properties"]["corrected"] = marks["corrected"]
                    carried += 1
            if carried:
                print(f"  Carried forward {carried} human-verified/corrected mark(s).")

            # ── Step 1c: MONOTONIC GUARD — never drop an existing mapped well ──
            # A rebuild may only ADD or UPDATE wells, never remove one. Any well
            # present on the live site whose stem is absent from this build is
            # carried forward unchanged, so old successful data on the map can
            # never be disturbed by a new/partial run.
            new_stems = {f["properties"].get("pdf_stem") for f in geojson["features"]}
            kept = 0
            for f in prior.get("features", []):
                if f["properties"].get("pdf_stem") not in new_stems:
                    geojson["features"].append(f)
                    kept += 1
            if kept:
                geojson["well_count"] = len(geojson["features"])
                n_feat = len(geojson["features"])
                print(f"  Monotonic guard: carried forward {kept} existing well(s) "
                      f"not in this build (total {n_feat:,}).")
        except Exception as exc:
            print(f"  WARNING: could not read prior GeoJSON for verified marks: {exc}")

    # ── Step 2: write well_locations.json ─────────────────────────────────────
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(geojson, f, separators=(",", ":"))

    kb = out_json.stat().st_size // 1024
    print(f"\n  OK  {n_feat:,} wells -> {out_json}  ({kb} KB)")
    print(f"     {n_skip:,} records skipped (unresolved / out of Oklahoma bounds)")

    # ── Step 3: resolution quality breakdown ─────────────────────────────────
    src_counts: dict[str, int] = {}
    for f in geojson["features"]:
        src = f["properties"].get("resolution", "unknown")
        src_counts[src] = src_counts.get(src, 0) + 1
    print("\n  Resolution sources:")
    for src, cnt in sorted(src_counts.items(), key=lambda x: -x[1]):
        bar = "#" * min(30, cnt // max(1, n_feat // 30))
        print(f"    {src:30s} {bar} {cnt:4d}")

    # ── Step 4: patch index.html ──────────────────────────────────────────────
    if not args.no_patch:
        print()
        patch_index_html(index_html)

    # ── Step 5: git push ──────────────────────────────────────────────────────
    if args.push:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = f"data: update well_locations.json - {n_feat} wells ({now})"
        git_push(repo, msg)
    else:
        print(f"\n  To publish: python build_map_data.py --push")
        print(f"  Or manually:")
        print(f"    cd {repo}")
        print(f"    git add docs/data/well_locations.json docs/index.html")
        print(f"    git commit -m 'data: update {n_feat} wells'")
        print(f"    git push")

    print("\nDone.\n")


if __name__ == "__main__":
    main()

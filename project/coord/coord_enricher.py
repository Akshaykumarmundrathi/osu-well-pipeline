"""
Post-pipeline coordinate enrichment stage.

Reads success.csv and resolves each dot-detected record to an exact
(lat, lon) using:
  1. OCR-extracted quadrant_label (location_quadrant_db from PDF text)
  2. U-Net dot detector nw label (dot_nw)
  3. U-Net (row, col) + x_norm/y_norm bilinear fallback

Outputs (all in the same directory as dot_coordinates.csv)
----------------------------------------------------------
dot_coordinates.csv           — all dot records, resolved or not
coord_resolution_failures.csv — unresolved records with reason codes
coord_resolution_log.csv      — compact per-record trace

Silently skipped if RDS is unreachable.
All connections closed before return.  Zero idle charges.
"""

import csv
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_RESOLVED_SOURCES = frozenset({
    "quadrant_direct",
    "exact_county", "exact_no_county",
    "county_constrained",
    "ns_fallback", "ew_fallback", "ns_ew_fallback",
    "county_stripped", "section_adjacent",
})

# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------

_COORD_FIELDS = [
    # Identity
    "pdf_stem", "pdf_path", "collection", "year", "month",
    "model_tier", "decade",
    # PLSS address
    "section", "township", "range", "county_name",
    # Quadrant sources
    "ocr_quadrant_pdf", "ocr_quadrant_db",
    "unet_nw", "effective_quad",
    "quadrant_source",        # ocr | unet | rc_derived | none
    "quadrant_agreement",     # agree | disagree | single_source | none
    # Dot detection
    "dot_row", "dot_col", "dot_nw", "dot_confidence",
    "dot_x_norm", "dot_y_norm",
    # Resolution result
    "resolved_lat", "resolved_lon",
    "resolution_source",
    # Cell geometry audit
    "cell_minx", "cell_miny", "cell_maxx", "cell_maxy",
    "cell_tl_lon", "cell_tl_lat",
    "cell_tr_lon", "cell_tr_lat",
    "cell_bl_lon", "cell_bl_lat",
    "cell_br_lon", "cell_br_lat",
    "cell_rel_x", "cell_rel_y",
    # Flags / warnings
    "flags",
]

_FAILURE_FIELDS = [
    "pdf_stem", "pdf_path", "collection", "year", "month",
    "model_tier", "decade",
    "section", "township", "range", "county_name",
    "dot_row", "dot_col", "dot_x_norm", "dot_y_norm",
    "ocr_quadrant_db", "unet_nw",
    "resolution_source", "flags",
]

_LOG_FIELDS = [
    "pdf_stem",
    "section", "township", "range", "county_name",
    "ocr_quadrant_db", "unet_nw", "effective_quad",
    "dot_row", "dot_col", "dot_x_norm", "dot_y_norm",
    "resolved_lat", "resolved_lon",
    "resolution_source",
    "cell_minx", "cell_miny", "cell_maxx", "cell_maxy",
    "cell_rel_x", "cell_rel_y",
    "flags",
]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def enrich_with_coordinates(
    success_csv: Path,
    output_csv: Path,
    county_constraints_dir: Path | None = None,
    status_csv: Path | None = None,
    _print=print,
) -> int:
    """
    Resolve dot coordinates for all dot-detected records in success.csv.

    county_constraints_dir — directory containing county_constraints.json
                              (same as output_root).  Loads constraints to
                              improve direction-inference on partial data.
    status_csv             — if provided, write coord_derivation audit back to
                              processing_status.csv via mark_coord_audit().

    Returns count of resolved records.  Skips silently if RDS is down.
    """
    if not success_csv.exists():
        log.warning("enrich_with_coordinates: %s not found", success_csv)
        return 0

    from coord.plss_resolver import PLSSResolver, parse_township, parse_range

    resolver = PLSSResolver()

    # Load county constraints if available (built during pipeline startup or
    # a prior run).
    if county_constraints_dir is not None:
        resolver.load_county_constraints(county_constraints_dir)

    try:
        resolver._connect()
    except Exception as exc:
        _print(f"  coordinate enrichment skipped: {exc}")
        return 0

    # Load ProcessingStatus for audit write-back (optional)
    ps = None
    if status_csv is not None and status_csv.exists():
        try:
            from utils.processing_status import ProcessingStatus
            ps = ProcessingStatus(status_csv)
        except Exception:
            ps = None

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    failures_csv = output_csv.parent / "coord_resolution_failures.csv"
    log_csv      = output_csv.parent / "coord_resolution_log.csv"

    n_total = n_resolved = n_failed = n_skipped = 0

    try:
        with (
            success_csv.open(newline="", encoding="utf-8") as fin,
            output_csv.open("w",   newline="", encoding="utf-8") as fout,
            failures_csv.open("w", newline="", encoding="utf-8") as ffail,
            log_csv.open("w",      newline="", encoding="utf-8") as flog,
        ):
            reader = csv.DictReader(fin)
            writer = csv.DictWriter(fout,  fieldnames=_COORD_FIELDS,
                                    extrasaction="ignore")
            wfail  = csv.DictWriter(ffail, fieldnames=_FAILURE_FIELDS,
                                    extrasaction="ignore")
            wlog   = csv.DictWriter(flog,  fieldnames=_LOG_FIELDS,
                                    extrasaction="ignore")
            writer.writeheader()
            wfail.writeheader()
            wlog.writeheader()

            for row in reader:
                n_total += 1
                coord_row, fail_row, log_row = _process_row(
                    row, resolver, parse_township, parse_range
                )
                if coord_row is None:
                    n_skipped += 1
                    continue

                writer.writerow(coord_row)
                wlog.writerow(log_row)
                resolved = coord_row["resolved_lat"] != ""
                if resolved:
                    n_resolved += 1
                else:
                    n_failed += 1
                    wfail.writerow(fail_row)

                # Write derivation audit back to processing_status
                if ps is not None:
                    _write_coord_audit(ps, coord_row, row)

    except Exception as exc:
        _print(f"  coordinate enrichment error: {exc}")
        log.error("enrich_with_coordinates: %s", exc, exc_info=True)
    finally:
        resolver.close()
        if ps is not None:
            try:
                ps.force_save()
            except Exception:
                pass

    _print(
        f"  dot_coordinates.csv          "
        f"({n_resolved:,} resolved  {n_failed:,} failed  {n_skipped:,} skipped)"
        f"  -> {output_csv}"
    )
    _print(f"  coord_resolution_failures.csv -> {failures_csv}")
    _print(f"  coord_resolution_log.csv      -> {log_csv}")
    return n_resolved


# ---------------------------------------------------------------------------
# Per-row processing
# ---------------------------------------------------------------------------

def _process_row(row: dict, resolver, parse_township, parse_range):
    """
    Returns (coord_dict, failure_dict, log_dict) or (None, None, None) to skip.
    """
    if str(row.get("dot_found", "")).lower() != "true":
        return None, None, None

    section  = str(row.get("section",              "") or "").strip()
    township = str(row.get("township",             "") or "").strip()
    rng_raw  = str(row.get("range",                "") or "").strip()
    county   = str(row.get("county_name",          "") or "").strip()
    dot_row  = str(row.get("dot_row",              "") or "").strip()
    dot_col  = str(row.get("dot_col",              "") or "").strip()
    x_norm_s = str(row.get("dot_x_norm",           "") or "").strip()
    y_norm_s = str(row.get("dot_y_norm",           "") or "").strip()
    # Quadrant labels from two independent sources
    ocr_quad_pdf = str(row.get("location_quadrant_pdf", "") or "").strip()
    ocr_quad_db  = str(row.get("location_quadrant_db",  "") or "").strip()
    unet_nw      = str(row.get("dot_nw",                "") or "").strip()

    if not (section and dot_row and dot_col):
        return None, None, None
    if not (township or rng_raw):
        return None, None, None

    try:
        x_norm = float(x_norm_s) if x_norm_s else None
    except ValueError:
        x_norm = None
    try:
        y_norm = float(y_norm_s) if y_norm_s else None
    except ValueError:
        y_norm = None

    twp_num, ns = parse_township(township)
    rng_num, ew = parse_range(rng_raw)

    # Determine quadrant agreement between OCR and U-Net sources
    quad_source, quad_agreement = _quadrant_meta(ocr_quad_db, unet_nw)

    result = resolver.resolve(
        section=section,
        township_num=twp_num, ns=ns,
        range_num=rng_num,    ew=ew,
        county=county,
        dot_row=dot_row, dot_col=dot_col,
        x_norm=x_norm, y_norm=y_norm,
        quadrant_label=ocr_quad_db or None,
        unet_nw=unet_nw or None,
    )

    # Determine what was actually used as the effective quadrant
    effective_quad = (ocr_quad_db or unet_nw or
                      _row_col_to_label_safe(dot_row, dot_col))

    lat_out = round(result["lat"], 7) if result["lat"] is not None else ""
    lon_out = round(result["lon"], 7) if result["lon"] is not None else ""
    flags   = "; ".join(result.get("flags", []))

    sec_minx  = result.get("sec_minx") or ""
    sec_miny  = result.get("sec_miny") or ""
    sec_maxx  = result.get("sec_maxx") or ""
    sec_maxy  = result.get("sec_maxy") or ""
    cell_rel_x = result.get("rel_x") if result.get("rel_x") is not None else ""
    cell_rel_y = result.get("rel_y") if result.get("rel_y") is not None else ""

    corners = result.get("cell_corners") or {}
    tl = corners.get("tl") or ("", "")
    tr = corners.get("tr") or ("", "")
    bl = corners.get("bl") or ("", "")
    br = corners.get("br") or ("", "")

    coord_row = {
        "pdf_stem":       row.get("pdf_stem",      ""),
        "pdf_path":       row.get("pdf_path",      ""),
        "collection":     row.get("collection",    ""),
        "year":           row.get("year",          ""),
        "month":          row.get("month",         ""),
        "model_tier":     row.get("model_tier",    ""),
        "decade":         row.get("decade",        ""),
        "section":        section,
        "township":       township,
        "range":          rng_raw,
        "county_name":    county,
        "ocr_quadrant_pdf": ocr_quad_pdf,
        "ocr_quadrant_db":  ocr_quad_db,
        "unet_nw":          unet_nw,
        "effective_quad":   effective_quad,
        "quadrant_source":  quad_source,
        "quadrant_agreement": quad_agreement,
        "dot_row":        dot_row,
        "dot_col":        dot_col,
        "dot_nw":         unet_nw,
        "dot_confidence": row.get("dot_confidence", ""),
        "dot_x_norm":     x_norm_s,
        "dot_y_norm":     y_norm_s,
        "resolved_lat":   lat_out,
        "resolved_lon":   lon_out,
        "resolution_source": result["source"],
        "cell_minx": sec_minx, "cell_miny": sec_miny,
        "cell_maxx": sec_maxx, "cell_maxy": sec_maxy,
        "cell_tl_lon": tl[0], "cell_tl_lat": tl[1],
        "cell_tr_lon": tr[0], "cell_tr_lat": tr[1],
        "cell_bl_lon": bl[0], "cell_bl_lat": bl[1],
        "cell_br_lon": br[0], "cell_br_lat": br[1],
        "cell_rel_x":  cell_rel_x,
        "cell_rel_y":  cell_rel_y,
        "flags": flags,
    }

    fail_row = {
        "pdf_stem":    row.get("pdf_stem", ""),
        "pdf_path":    row.get("pdf_path", ""),
        "collection":  row.get("collection", ""),
        "year":        row.get("year", ""),
        "month":       row.get("month", ""),
        "model_tier":  row.get("model_tier", ""),
        "decade":      row.get("decade", ""),
        "section":     section,
        "township":    township,
        "range":       rng_raw,
        "county_name": county,
        "dot_row":     dot_row,
        "dot_col":     dot_col,
        "dot_x_norm":  x_norm_s,
        "dot_y_norm":  y_norm_s,
        "ocr_quadrant_db": ocr_quad_db,
        "unet_nw":         unet_nw,
        "resolution_source": result["source"],
        "flags": flags,
    }

    log_row = {
        "pdf_stem":    row.get("pdf_stem", ""),
        "section":     section,
        "township":    township,
        "range":       rng_raw,
        "county_name": county,
        "ocr_quadrant_db": ocr_quad_db,
        "unet_nw":         unet_nw,
        "effective_quad":  effective_quad,
        "dot_row":     dot_row,
        "dot_col":     dot_col,
        "dot_x_norm":  x_norm_s,
        "dot_y_norm":  y_norm_s,
        "resolved_lat":  lat_out,
        "resolved_lon":  lon_out,
        "resolution_source": result["source"],
        "cell_minx": sec_minx, "cell_miny": sec_miny,
        "cell_maxx": sec_maxx, "cell_maxy": sec_maxy,
        "cell_rel_x": cell_rel_x,
        "cell_rel_y": cell_rel_y,
        "flags": flags,
    }

    return coord_row, fail_row, log_row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_coord_audit(ps, coord_row: dict, src_row: dict):
    """Write coord_derivation provenance back to ProcessingStatus."""
    stem = coord_row.get("pdf_stem", "")
    if not stem:
        return
    src = coord_row.get("resolution_source", "")
    resolved = coord_row.get("resolved_lat", "") != ""

    if resolved:
        if src == "quadrant_direct":
            derivation = "dot_interpolation_quad"
        else:
            derivation = "dot_interpolation"
    else:
        derivation = "not_resolved"

    # Was lat/lon directly in the PDF? Check if latlong stage found it
    if str(src_row.get("latlong_found", "")).lower() == "true":
        derivation = "latlong_direct"

    section_src = "header_block" if src_row.get("header_section") else (
        "ocr_extracted" if src_row.get("section") else "not_found"
    )
    twp_src = "header_block" if src_row.get("header_township") else (
        "ocr_extracted" if src_row.get("township") else "not_found"
    )
    rng_src = "header_block" if src_row.get("header_range") else (
        "ocr_extracted" if src_row.get("range") else "not_found"
    )

    quad_src = coord_row.get("quadrant_source", "none")
    dot_src = ("unet" if quad_src == "unet" else
               "ocr_quadrant" if quad_src == "ocr" else
               "both" if quad_src == "both" else "not_found")

    try:
        ps.mark_coord_audit(
            stem,
            derivation=derivation,
            latlong_source=src_row.get("latlong_method", ""),
            section_source=section_src,
            township_source=twp_src,
            range_source=rng_src,
            county_used=coord_row.get("county_name", ""),
            dot_source=dot_src,
        )
    except Exception:
        pass


def _quadrant_meta(ocr_db: str, unet_nw: str) -> tuple[str, str]:
    """
    Returns (source, agreement) strings describing the quadrant evidence.
    source:    'ocr'|'unet'|'both'|'none'
    agreement: 'agree'|'disagree'|'single_source'|'none'
    """
    has_ocr  = bool(ocr_db)
    has_unet = bool(unet_nw)

    if has_ocr and has_unet:
        source = "both"
        agreement = "agree" if ocr_db == unet_nw else "disagree"
    elif has_ocr:
        source, agreement = "ocr", "single_source"
    elif has_unet:
        source, agreement = "unet", "single_source"
    else:
        source, agreement = "none", "none"
    return source, agreement


def _row_col_to_label_safe(dot_row_s: str, dot_col_s: str) -> str:
    try:
        from ocr.quadrant_extractor import row_col_to_label
        label = row_col_to_label(int(dot_row_s), int(dot_col_s))
        return label or ""
    except Exception:
        return ""

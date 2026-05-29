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

# ---------------------------------------------------------------------------
# Canonical field normalisers
# ---------------------------------------------------------------------------
# Import from the sibling module in the same package.  Fallbacks are no-ops
# (plain strip) so the enricher degrades gracefully if the file is missing.
try:
    from coord.field_normalizer import (
        norm_section, norm_township, norm_range, norm_county, norm_quadrant,
    )
except ImportError:
    def norm_section(v):                 return str(v or "").strip()   # type: ignore[misc]
    def norm_township(v):                return str(v or "").strip()   # type: ignore[misc]
    def norm_range(v):                   return str(v or "").strip()   # type: ignore[misc]
    def norm_county(v, county_map=None): return str(v or "").strip()   # type: ignore[misc]
    def norm_quadrant(v):                return str(v or "").strip()   # type: ignore[misc]

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

    # Load per-county direction statistics (one RDS query, ~4.5 M rows).
    # Improves NS/EW direction-fallback ordering when OCR gives bare numbers
    # like "13" instead of "13N" — most-probable direction is tried first.
    try:
        resolver.load_county_direction_stats()
        _print(
            f"  county direction stats: {len(resolver._county_direction_stats):,} counties"
        )
    except Exception as _dex:
        _print(f"  county direction stats unavailable ({_dex}) — using geographic priors")

    try:
        resolver._connect()
    except Exception as exc:
        _print(f"  coordinate enrichment skipped: {exc}")
        return 0

    # -- Load ALL rows into memory so we can prefetch before processing -------
    # This replaces the previous streaming approach; memory cost is negligible
    # (a few MB for thousands of records) versus the massive RDS query savings.
    with success_csv.open(newline="", encoding="utf-8") as fin:
        all_rows = list(csv.DictReader(fin))

    _print(f"  prefetching PLSS bboxes for {len(all_rows):,} records…")
    resolver.prefetch_for_records(all_rows)
    stats = resolver.cache_stats()
    # stats are 0/0 until resolve() calls happen, but log the cache sizes now
    _print(
        f"  cache warmed: {len(resolver._section_cache):,} sections, "
        f"{len(resolver._cell_cache):,} cells "
        f"(~{len(resolver._section_cache) + len(resolver._cell_cache)} "
        f"cache hits expected vs {len(all_rows)} records)"
    )

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
        # ── Pass 1: normal enrichment with statistical direction ordering ─────
        _print(f"  Pass 1: processing {len(all_rows):,} records…")
        # Each entry: (coord_row, fail_row, log_row, orig_row) or None (skipped)
        results: list = [None] * len(all_rows)
        rds_miss_indices: list[int] = []

        for i, row in enumerate(all_rows):
            n_total += 1
            coord_row, fail_row, log_row = _process_row(
                row, resolver, parse_township, parse_range
            )
            if coord_row is None:
                continue   # n_skipped counted in write phase
            results[i] = (coord_row, fail_row, log_row, row)
            if coord_row.get("resolution_source") == "rds_miss":
                rds_miss_indices.append(i)

        n_p1_resolved = sum(
            1 for t in results
            if t and t[0].get("resolved_lat") != ""
        )
        _print(
            f"  Pass 1 resolved: {n_p1_resolved:,}  |  "
            f"rds_miss queued for Pass 2: {len(rds_miss_indices):,}"
        )

        # ── Pass 2: exhaustive NS×EW sweep + wider section adjacency ─────────
        n_pass2_resolved = 0
        if rds_miss_indices:
            _print(
                f"  Pass 2: {len(rds_miss_indices):,} records -> "
                f"exhaustive direction sweep + section adj..."
            )
            for idx in rds_miss_indices:
                orig_row = all_rows[idx]
                p2_coord, p2_fail, p2_log = _process_row_pass2(
                    orig_row, resolver, parse_township, parse_range
                )
                if p2_coord is not None and p2_coord.get("resolved_lat") != "":
                    results[idx] = (p2_coord, p2_fail, p2_log, orig_row)
                    n_pass2_resolved += 1
            _print(
                f"  Pass 2 resolved: +{n_pass2_resolved:,}  "
                f"(permanent rds_miss: "
                f"{len(rds_miss_indices) - n_pass2_resolved:,})"
            )

        # ── Pass 3: infer missing PLSS field for bounds_invalid records ──────
        # Records where exactly one of township_num / range_num is None can be
        # resolved by querying the RDS for a uniquely-matching counterpart.
        # This recovers wells where OCR extracted only one PLSS coordinate.
        bounds_invalid_indices = [
            i for i, t in enumerate(results)
            if t and t[0].get("resolution_source") == "bounds_invalid"
        ]
        n_pass3_resolved = 0
        if bounds_invalid_indices:
            _print(
                f"  Pass 3: {len(bounds_invalid_indices):,} bounds_invalid records "
                f"-> infer missing PLSS field from RDS..."
            )
            for idx in bounds_invalid_indices:
                orig_row = all_rows[idx]
                p3_coord, p3_fail, p3_log = _process_row_pass3(
                    orig_row, resolver, parse_township, parse_range
                )
                if p3_coord is not None and p3_coord.get("resolved_lat") != "":
                    results[idx] = (p3_coord, p3_fail, p3_log, orig_row)
                    n_pass3_resolved += 1
            _print(
                f"  Pass 3 resolved: +{n_pass3_resolved:,}  "
                f"(permanent bounds_invalid: "
                f"{len(bounds_invalid_indices) - n_pass3_resolved:,})"
            )

        # ── Pass 4: resolved-neighbor (lease) inference ───────────────────────
        # For wells whose PLSS field(s) couldn't be resolved automatically,
        # look at already-resolved wells from the SAME LEASE/FIELD and copy the
        # missing values when all neighbors agree.  E.g. if HUCKLEBERRY 1, 3, 4
        # are all at Township 25N / Range 15E, infer that HUCKLEBERRY 2 is too.
        #
        # This is fundamentally different from Pass 3 (RDS uniqueness): here we
        # use the actual dataset as ground truth rather than the full PLSS grid.
        # Works for bounds_invalid AND previously-skipped (no_plss, no_section).

        lease_index = _build_lease_index(results, all_rows)
        log.debug("Pass 4 lease index: %d leases", len(lease_index))

        n_pass4_resolved = 0

        # 4a: bounds_invalid records still in results -------------------------
        bi_indices_p4 = [
            i for i, t in enumerate(results)
            if t and t[0].get("resolution_source") == "bounds_invalid"
        ]

        # 4b: skipped records (results[i] is None) that have a dot but
        #     missing PLSS fields — may now be inferrable from neighbors ------
        def _is_enrichable_skip(row: dict) -> bool:
            """True if this skipped row has a dot but is missing PLSS fields."""
            dr   = str(row.get("dot_row",   "") or "").strip()
            dc   = str(row.get("dot_col",   "") or "").strip()
            conf = str(row.get("dot_confidence", "0") or "0").strip()
            if not dr or not dc or conf in ("", "0", "0.0"):
                return False  # no dot — can't enrich
            return True

        skipped_indices_p4 = [
            i for i, t in enumerate(results)
            if t is None and _is_enrichable_skip(all_rows[i])
        ]

        p4_total = len(bi_indices_p4) + len(skipped_indices_p4)
        if p4_total:
            _print(
                f"  Pass 4: {len(bi_indices_p4):,} bounds_invalid + "
                f"{len(skipped_indices_p4):,} skipped "
                f"-> lease-neighbor inference..."
            )

            for idx in bi_indices_p4:
                orig_row = all_rows[idx]
                stem     = orig_row.get("pdf_stem", "")

                # Determine which field is missing
                township = norm_township(orig_row.get("township", "") or
                                         orig_row.get("location_township", ""))
                rng_raw  = norm_range(   orig_row.get("range",    "") or
                                         orig_row.get("location_range",    ""))
                twp_num, ns = parse_township(township)
                rng_num, ew = parse_range(rng_raw)

                need_twp = (twp_num is None)
                need_rng = (rng_num is None)
                if not need_twp and not need_rng:
                    continue   # bounds_invalid for other reason (too_high)

                county   = norm_county(orig_row.get("county_name", ""))
                twp_inf, rng_inf, _ = _infer_from_lease(
                    stem, lease_index, need_twp, need_rng, county=county
                )
                if (need_twp and not twp_inf) or (need_rng and not rng_inf):
                    continue   # no consensus from neighbors

                # Patch the row with inferred values and re-process
                patched = dict(orig_row)
                if twp_inf:
                    patched["location_township"] = twp_inf
                    patched["township"]          = twp_inf
                if rng_inf:
                    patched["location_range"] = rng_inf
                    patched["range"]          = rng_inf

                # Re-run the standard row processor with patched data
                p4_coord, p4_fail, p4_log = _process_row(
                    patched, resolver, parse_township, parse_range
                )
                if p4_coord is not None and p4_coord.get("resolved_lat") != "":
                    # Tag with pass-4 provenance
                    existing_flags = p4_coord.get("flags", "")
                    inferred_tags  = []
                    if twp_inf: inferred_tags.append(f"p4_twp_inferred:{twp_inf}")
                    if rng_inf: inferred_tags.append(f"p4_rng_inferred:{rng_inf}")
                    tag_str = "; ".join(inferred_tags)
                    p4_coord["flags"] = (tag_str + "; " + existing_flags).strip("; ")
                    src = p4_coord.get("resolution_source", "")
                    if src not in ("bounds_invalid", "rds_miss", "parse_failed", ""):
                        p4_coord["resolution_source"] = f"p4_{src}"
                    results[idx] = (p4_coord, p4_fail, p4_log, orig_row)
                    n_pass4_resolved += 1

            for idx in skipped_indices_p4:
                orig_row = all_rows[idx]
                stem     = orig_row.get("pdf_stem", "")

                section  = norm_section( orig_row.get("location_section",  ""))
                township = norm_township(orig_row.get("location_township", ""))
                rng_raw  = norm_range(   orig_row.get("location_range",    ""))
                twp_num, _ = parse_township(township)
                rng_num, _ = parse_range(rng_raw)

                need_twp  = (twp_num is None and not township)
                need_rng  = (rng_num is None and not rng_raw)
                need_sect = (not section)

                if not (need_twp or need_rng or need_sect):
                    continue  # should already be in results somehow

                county   = norm_county(orig_row.get("county_name", ""))
                twp_inf, rng_inf, sect_inf = _infer_from_lease(
                    stem, lease_index, need_twp, need_rng, need_sect, county=county
                )

                # Must have all required inferences to proceed
                if need_sect and not sect_inf:
                    continue
                if need_twp and not twp_inf:
                    continue
                if need_rng and not rng_inf:
                    continue

                patched = dict(orig_row)
                if sect_inf:
                    patched["location_section"] = sect_inf
                    patched["section"]          = sect_inf
                if twp_inf:
                    patched["location_township"] = twp_inf
                    patched["township"]          = twp_inf
                if rng_inf:
                    patched["location_range"] = rng_inf
                    patched["range"]          = rng_inf

                p4_coord, p4_fail, p4_log = _process_row(
                    patched, resolver, parse_township, parse_range
                )
                if p4_coord is not None and p4_coord.get("resolved_lat") != "":
                    existing_flags = p4_coord.get("flags", "")
                    inferred_tags  = []
                    if sect_inf: inferred_tags.append(f"p4_sect_inferred:{sect_inf}")
                    if twp_inf:  inferred_tags.append(f"p4_twp_inferred:{twp_inf}")
                    if rng_inf:  inferred_tags.append(f"p4_rng_inferred:{rng_inf}")
                    tag_str = "; ".join(inferred_tags)
                    p4_coord["flags"] = (tag_str + "; " + existing_flags).strip("; ")
                    src = p4_coord.get("resolution_source", "")
                    if src not in ("bounds_invalid", "rds_miss", "parse_failed", ""):
                        p4_coord["resolution_source"] = f"p4_{src}"
                    results[idx] = (p4_coord, p4_fail, p4_log, orig_row)
                    n_pass4_resolved += 1

            _print(
                f"  Pass 4 resolved: +{n_pass4_resolved:,}  "
                f"(lease-neighbor inference)"
            )

        # ── Pass 5: Section centroid for PLSS-complete no-dot records ────────
        # Two cases are eligible:
        #   (a) centroid_candidate=true  — G records (grid not found, dot skipped)
        #       that were added to success_csv by run_coord_enrichment --include-centroid
        #   (b) F records — dot stage ran but position is empty (dot_row/col blank);
        #       they are in success_csv (dot_found=true) but _process_row() skipped
        #       them because dot_row/dot_col was absent.  Pass 5 catches these too.
        # Both cases use the same _process_row_pass5_centroid() handler.
        def _is_centroid_eligible(row: dict) -> bool:
            if row.get("centroid_candidate") == "true":
                return True
            # F-category: passed through _normalise_row (dot_found=true) but
            # dot_row or dot_col is empty — dot model returned no position.
            if (str(row.get("dot_found", "")).lower() == "true"
                    and not str(row.get("dot_row", "") or "").strip()
                    and not str(row.get("dot_col", "") or "").strip()):
                return True
            return False

        centroid_indices = [
            i for i, t in enumerate(results)
            if t is None and _is_centroid_eligible(all_rows[i])
        ]
        n_pass5_resolved = 0
        if centroid_indices:
            _print(
                f"  Pass 5: {len(centroid_indices):,} centroid candidates "
                f"-> section centroid approximation..."
            )
            for idx in centroid_indices:
                orig_row = all_rows[idx]
                p5_coord, p5_fail, p5_log = _process_row_pass5_centroid(
                    orig_row, resolver, parse_township, parse_range
                )
                if p5_coord is not None and p5_coord.get("resolved_lat") != "":
                    results[idx] = (p5_coord, p5_fail, p5_log, orig_row)
                    n_pass5_resolved += 1
            _print(
                f"  Pass 5 resolved: +{n_pass5_resolved:,}  "
                f"(approx section centroid, +/-0.5 mi)"
            )

        # ── Write combined results ────────────────────────────────────────────
        with (
            output_csv.open("w",   newline="", encoding="utf-8") as fout,
            failures_csv.open("w", newline="", encoding="utf-8") as ffail,
            log_csv.open("w",      newline="", encoding="utf-8") as flog,
        ):
            writer = csv.DictWriter(fout,  fieldnames=_COORD_FIELDS,
                                    extrasaction="ignore")
            wfail  = csv.DictWriter(ffail, fieldnames=_FAILURE_FIELDS,
                                    extrasaction="ignore")
            wlog   = csv.DictWriter(flog,  fieldnames=_LOG_FIELDS,
                                    extrasaction="ignore")
            writer.writeheader()
            wfail.writeheader()
            wlog.writeheader()

            for entry in results:
                if entry is None:
                    n_skipped += 1
                    continue
                coord_row, fail_row, log_row, orig_row = entry
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
                    _write_coord_audit(ps, coord_row, orig_row)

    except Exception as exc:
        _print(f"  coordinate enrichment error: {exc}")
        log.error("enrich_with_coordinates: %s", exc, exc_info=True)
    finally:
        # Log final cache hit-rate before closing
        cs = resolver.cache_stats()
        if cs["hits"] + cs["misses"] > 0:
            _print(
                f"  cache hit-rate: {cs['hit_rate']*100:.1f}%  "
                f"({cs['hits']:,} hits / {cs['misses']:,} live queries)"
            )
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

    # Canonical normalisation: handles spacing, caps, T/R prefix, float artefacts,
    # junk county values ("Not detected." -> ""), malformed quadrants -> ""
    section      = norm_section( row.get("section",     ""))
    township     = norm_township(row.get("township",    ""))
    rng_raw      = norm_range(   row.get("range",       ""))
    county       = norm_county(  row.get("county_name", ""))
    dot_row      = str(row.get("dot_row",    "") or "").strip()
    dot_col      = str(row.get("dot_col",    "") or "").strip()
    x_norm_s     = str(row.get("dot_x_norm", "") or "").strip()
    y_norm_s     = str(row.get("dot_y_norm", "") or "").strip()
    # Quadrant labels from two independent sources (normalised to canonical form)
    ocr_quad_pdf = norm_quadrant(row.get("location_quadrant_pdf", ""))
    ocr_quad_db  = norm_quadrant(row.get("location_quadrant_db",  ""))
    unet_nw      = norm_quadrant(row.get("dot_nw",                ""))

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
# Pass-2 per-row processor
# ---------------------------------------------------------------------------

def _process_row_pass2(row: dict, resolver, parse_township, parse_range):
    """
    Pass-2 variant of _process_row.

    Delegates to resolver.resolve_exhaustive() instead of resolver.resolve().
    Called only for records whose Pass-1 resolution_source was 'rds_miss'.

    Differences from Pass 1
    -----------------------
    - Tries ALL valid NS×EW direction combinations (most-probable first)
    - Also searches section ±2 (Pass 1 only does ±1)
    - Source codes have 'p2_' prefix so map/logs can distinguish the pass
    """
    if str(row.get("dot_found", "")).lower() != "true":
        return None, None, None

    # Canonical normalisation (same rules as _process_row)
    section      = norm_section( row.get("section",     ""))
    township     = norm_township(row.get("township",    ""))
    rng_raw      = norm_range(   row.get("range",       ""))
    county       = norm_county(  row.get("county_name", ""))
    dot_row      = str(row.get("dot_row",    "") or "").strip()
    dot_col      = str(row.get("dot_col",    "") or "").strip()
    x_norm_s     = str(row.get("dot_x_norm", "") or "").strip()
    y_norm_s     = str(row.get("dot_y_norm", "") or "").strip()
    ocr_quad_pdf = norm_quadrant(row.get("location_quadrant_pdf", ""))
    ocr_quad_db  = norm_quadrant(row.get("location_quadrant_db",  ""))
    unet_nw      = norm_quadrant(row.get("dot_nw",                ""))

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

    quad_source, quad_agreement = _quadrant_meta(ocr_quad_db, unet_nw)

    result = resolver.resolve_exhaustive(
        section=section,
        township_num=twp_num, ns=ns,
        range_num=rng_num,    ew=ew,
        county=county,
        dot_row=dot_row, dot_col=dot_col,
        x_norm=x_norm, y_norm=y_norm,
        quadrant_label=ocr_quad_db or None,
        unet_nw=unet_nw or None,
    )

    effective_quad = (ocr_quad_db or unet_nw or
                      _row_col_to_label_safe(dot_row, dot_col))

    lat_out = round(result["lat"], 7) if result["lat"] is not None else ""
    lon_out = round(result["lon"], 7) if result["lon"] is not None else ""
    flags   = "; ".join(result.get("flags", []))

    sec_minx   = result.get("sec_minx") or ""
    sec_miny   = result.get("sec_miny") or ""
    sec_maxx   = result.get("sec_maxx") or ""
    sec_maxy   = result.get("sec_maxy") or ""
    cell_rel_x = result.get("rel_x") if result.get("rel_x") is not None else ""
    cell_rel_y = result.get("rel_y") if result.get("rel_y") is not None else ""

    corners = result.get("cell_corners") or {}
    tl = corners.get("tl") or ("", "")
    tr = corners.get("tr") or ("", "")
    bl = corners.get("bl") or ("", "")
    br = corners.get("br") or ("", "")

    coord_row = {
        "pdf_stem":         row.get("pdf_stem",    ""),
        "pdf_path":         row.get("pdf_path",    ""),
        "collection":       row.get("collection",  ""),
        "year":             row.get("year",        ""),
        "month":            row.get("month",       ""),
        "model_tier":       row.get("model_tier",  ""),
        "decade":           row.get("decade",      ""),
        "section":          section,
        "township":         township,
        "range":            rng_raw,
        "county_name":      county,
        "ocr_quadrant_pdf": ocr_quad_pdf,
        "ocr_quadrant_db":  ocr_quad_db,
        "unet_nw":          unet_nw,
        "effective_quad":   effective_quad,
        "quadrant_source":  quad_source,
        "quadrant_agreement": quad_agreement,
        "dot_row":          dot_row,
        "dot_col":          dot_col,
        "dot_nw":           unet_nw,
        "dot_confidence":   row.get("dot_confidence", ""),
        "dot_x_norm":       x_norm_s,
        "dot_y_norm":       y_norm_s,
        "resolved_lat":     lat_out,
        "resolved_lon":     lon_out,
        "resolution_source": result["source"],
        "cell_minx": sec_minx,  "cell_miny": sec_miny,
        "cell_maxx": sec_maxx,  "cell_maxy": sec_maxy,
        "cell_tl_lon": tl[0],   "cell_tl_lat": tl[1],
        "cell_tr_lon": tr[0],   "cell_tr_lat": tr[1],
        "cell_bl_lon": bl[0],   "cell_bl_lat": bl[1],
        "cell_br_lon": br[0],   "cell_br_lat": br[1],
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
# Pass-3 per-row processor
# ---------------------------------------------------------------------------

def _process_row_pass3(row: dict, resolver, parse_township, parse_range):
    """
    Pass-3: For bounds_invalid records where exactly one of township_num /
    range_num is None, infer the missing value from RDS and re-resolve.

    Only called on rows whose Pass-1 resolution_source was 'bounds_invalid'.
    Delegates to resolver.resolve_missing_field() which queries the RDS for
    a unique match of the missing PLSS field given the county + known field.

    Returns (coord_dict, failure_dict, log_dict) on success, or
    (None, None, None) to skip (ambiguous, no match, or not applicable).
    """
    if str(row.get("dot_found", "")).lower() != "true":
        return None, None, None

    # Canonical normalisation (same rules as _process_row)
    section      = norm_section( row.get("section",     ""))
    township     = norm_township(row.get("township",    ""))
    rng_raw      = norm_range(   row.get("range",       ""))
    county       = norm_county(  row.get("county_name", ""))
    dot_row      = str(row.get("dot_row",    "") or "").strip()
    dot_col      = str(row.get("dot_col",    "") or "").strip()
    x_norm_s     = str(row.get("dot_x_norm", "") or "").strip()
    y_norm_s     = str(row.get("dot_y_norm", "") or "").strip()
    ocr_quad_pdf = norm_quadrant(row.get("location_quadrant_pdf", ""))
    ocr_quad_db  = norm_quadrant(row.get("location_quadrant_db",  ""))
    unet_nw      = norm_quadrant(row.get("dot_nw",                ""))

    if not (section and dot_row and dot_col):
        return None, None, None

    twp_num, ns = parse_township(township)
    rng_num, ew = parse_range(rng_raw)

    # Pass 3 only targets: exactly one of the two numbers is None
    if twp_num is None and rng_num is None:
        return None, None, None   # both missing — can't infer
    if twp_num is not None and rng_num is not None:
        return None, None, None   # both present — not a missing-field case

    try:
        x_norm = float(x_norm_s) if x_norm_s else None
    except ValueError:
        x_norm = None
    try:
        y_norm = float(y_norm_s) if y_norm_s else None
    except ValueError:
        y_norm = None

    quad_source, quad_agreement = _quadrant_meta(ocr_quad_db, unet_nw)

    result = resolver.resolve_missing_field(
        section=section,
        township_num=twp_num, ns=ns,
        range_num=rng_num,    ew=ew,
        county=county,
        dot_row=dot_row, dot_col=dot_col,
        x_norm=x_norm, y_norm=y_norm,
        quadrant_label=ocr_quad_db or None,
        unet_nw=unet_nw or None,
    )

    # Only accept a genuine coordinate (not ambiguous / no-match)
    if result is None or result.get("lat") is None:
        return None, None, None

    effective_quad = (ocr_quad_db or unet_nw or
                      _row_col_to_label_safe(dot_row, dot_col))

    lat_out = round(result["lat"], 7) if result["lat"] is not None else ""
    lon_out = round(result["lon"], 7) if result["lon"] is not None else ""
    flags   = "; ".join(result.get("flags", []))

    sec_minx   = result.get("sec_minx") or ""
    sec_miny   = result.get("sec_miny") or ""
    sec_maxx   = result.get("sec_maxx") or ""
    sec_maxy   = result.get("sec_maxy") or ""
    cell_rel_x = result.get("rel_x") if result.get("rel_x") is not None else ""
    cell_rel_y = result.get("rel_y") if result.get("rel_y") is not None else ""

    corners = result.get("cell_corners") or {}
    tl = corners.get("tl") or ("", "")
    tr = corners.get("tr") or ("", "")
    bl = corners.get("bl") or ("", "")
    br = corners.get("br") or ("", "")

    coord_row = {
        "pdf_stem":         row.get("pdf_stem",    ""),
        "pdf_path":         row.get("pdf_path",    ""),
        "collection":       row.get("collection",  ""),
        "year":             row.get("year",        ""),
        "month":            row.get("month",       ""),
        "model_tier":       row.get("model_tier",  ""),
        "decade":           row.get("decade",      ""),
        "section":          section,
        "township":         township,
        "range":            rng_raw,
        "county_name":      county,
        "ocr_quadrant_pdf": ocr_quad_pdf,
        "ocr_quadrant_db":  ocr_quad_db,
        "unet_nw":          unet_nw,
        "effective_quad":   effective_quad,
        "quadrant_source":  quad_source,
        "quadrant_agreement": quad_agreement,
        "dot_row":          dot_row,
        "dot_col":          dot_col,
        "dot_nw":           unet_nw,
        "dot_confidence":   row.get("dot_confidence", ""),
        "dot_x_norm":       x_norm_s,
        "dot_y_norm":       y_norm_s,
        "resolved_lat":     lat_out,
        "resolved_lon":     lon_out,
        "resolution_source": result["source"],
        "cell_minx": sec_minx,  "cell_miny": sec_miny,
        "cell_maxx": sec_maxx,  "cell_maxy": sec_maxy,
        "cell_tl_lon": tl[0],   "cell_tl_lat": tl[1],
        "cell_tr_lon": tr[0],   "cell_tr_lat": tr[1],
        "cell_bl_lon": bl[0],   "cell_bl_lat": bl[1],
        "cell_br_lon": br[0],   "cell_br_lat": br[1],
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
# Pass-5 per-row processor: section centroid
# ---------------------------------------------------------------------------

def _process_row_pass5_centroid(row: dict, resolver, parse_township, parse_range):
    """
    Pass-5: Return the geographic centre of the PLSS section as an approximate
    well location.  Called only for rows with centroid_candidate=true
    (dot stage found nothing or grid image unavailable, but PLSS is complete).

    No dot position required — uses only section/township/range/county.
    Precision is approximately ½ mile.

    Returns (coord_dict, failure_dict, log_dict) or (None, None, None) to skip.
    """
    section  = norm_section( row.get("section",     ""))
    township = norm_township(row.get("township",    ""))
    rng_raw  = norm_range(   row.get("range",       ""))
    county   = norm_county(  row.get("county_name", ""))

    if not section:
        return None, None, None
    if not (township or rng_raw):
        return None, None, None

    twp_num, ns = parse_township(township)
    rng_num, ew = parse_range(rng_raw)

    if twp_num is None or rng_num is None:
        return None, None, None   # need both for centroid

    result = resolver.resolve_section_centroid(
        section=section,
        township_num=twp_num, ns=ns,
        range_num=rng_num,    ew=ew,
        county=county,
    )

    if result is None or result.get("lat") is None:
        return None, None, None

    lat_out = round(result["lat"], 7)
    lon_out = round(result["lon"], 7)
    flags   = "; ".join(result.get("flags", []))

    sec_minx = result.get("sec_minx") or ""
    sec_miny = result.get("sec_miny") or ""
    sec_maxx = result.get("sec_maxx") or ""
    sec_maxy = result.get("sec_maxy") or ""

    coord_row = {
        "pdf_stem":       row.get("pdf_stem",   ""),
        "pdf_path":       row.get("pdf_path",   ""),
        "collection":     row.get("collection", ""),
        "year":           row.get("year",       ""),
        "month":          row.get("month",      ""),
        "model_tier":     row.get("model_tier", ""),
        "decade":         row.get("decade",     ""),
        "section":        section,
        "township":       township,
        "range":          rng_raw,
        "county_name":    county,
        "ocr_quadrant_pdf": "",
        "ocr_quadrant_db":  "",
        "unet_nw":          "",
        "effective_quad":   "",
        "quadrant_source":  "none",
        "quadrant_agreement": "none",
        "dot_row":        "",
        "dot_col":        "",
        "dot_nw":         "",
        "dot_confidence": "",
        "dot_x_norm":     "",
        "dot_y_norm":     "",
        "resolved_lat":   lat_out,
        "resolved_lon":   lon_out,
        "resolution_source": result["source"],
        "cell_minx": sec_minx, "cell_miny": sec_miny,
        "cell_maxx": sec_maxx, "cell_maxy": sec_maxy,
        "cell_tl_lon": "", "cell_tl_lat": "",
        "cell_tr_lon": "", "cell_tr_lat": "",
        "cell_bl_lon": "", "cell_bl_lat": "",
        "cell_br_lon": "", "cell_br_lat": "",
        "cell_rel_x":  0.5,
        "cell_rel_y":  0.5,
        "flags": flags,
    }

    fail_row = {k: coord_row.get(k, "") for k in _FAILURE_FIELDS}

    log_row = {
        "pdf_stem":    row.get("pdf_stem", ""),
        "section":     section,
        "township":    township,
        "range":       rng_raw,
        "county_name": county,
        "ocr_quadrant_db": "",
        "unet_nw":         "",
        "effective_quad":  "",
        "dot_row":     "",
        "dot_col":     "",
        "dot_x_norm":  "",
        "dot_y_norm":  "",
        "resolved_lat":  lat_out,
        "resolved_lon":  lon_out,
        "resolution_source": result["source"],
        "cell_minx": sec_minx, "cell_miny": sec_miny,
        "cell_maxx": sec_maxx, "cell_maxy": sec_maxy,
        "cell_rel_x": 0.5,
        "cell_rel_y": 0.5,
        "flags": flags,
    }

    return coord_row, fail_row, log_row


# ---------------------------------------------------------------------------
# Pass-4 helpers: resolved-neighbor (lease) inference
# ---------------------------------------------------------------------------

import re as _re

def _extract_lease_name(pdf_stem: str) -> str:
    """
    Extract the lease/well-family name from a pdf_stem.

    pdf_stem format:  <ID>_<WELL_NAME> <WELL_NUM>_<RECORD_ID>
    Examples:
      "10503518_HUCKLEBERRY 2_1613736"       -> "HUCKLEBERRY"
      "35147763600000_R M WOLFE 4_1380763"   -> "R M WOLFE"
      "x_SECURITY 93_1613731"                -> "SECURITY"
      "X_LEDBETTER AND TERRELL 56_1679383"   -> "LEDBETTER AND TERRELL"

    Returns upper-case lease name or "" if pattern doesn't match.
    """
    m = _re.match(r'^[^_]+_(.+)_\d+$', pdf_stem)
    if not m:
        return ""
    name = m.group(1).strip()
    # Strip trailing well number (e.g. " 56" -> "")
    name = _re.sub(r'\s+\d+$', '', name).strip()
    return name.upper()


def _build_lease_index(results: list, all_rows: list) -> dict:
    """
    Build index: (lease_name, county) -> {'township': set, 'range': set, 'section': set}
    from all currently RESOLVED records in this enrichment run.

    Keyed by (lease_name, county) to prevent cross-county contamination — wells
    with the same lease name in different counties won't pollute each other's
    PLSS data.  E.g. HUCKLEBERRY in Nowata County and HUCKLEBERRY in Okmulgee
    County are tracked independently.

    Only records with non-empty resolved_lat are included.
    Only the normalised values (township/range/section from coord_row) are used.
    """
    from collections import defaultdict
    index: dict[tuple, dict[str, set]] = defaultdict(
        lambda: {"township": set(), "range": set(), "section": set()}
    )

    for i, entry in enumerate(results):
        if entry is None:
            continue
        coord_row = entry[0]
        if not coord_row.get("resolved_lat"):
            continue

        stem   = coord_row.get("pdf_stem", "")
        lease  = _extract_lease_name(stem)
        if not lease:
            continue

        county = coord_row.get("county_name", "").strip()
        key    = (lease, county)

        twp  = coord_row.get("township", "").strip()
        rng  = coord_row.get("range",    "").strip()
        sect = coord_row.get("section",  "").strip()

        if twp:  index[key]["township"].add(twp)
        if rng:  index[key]["range"].add(rng)
        if sect: index[key]["section"].add(sect)

    return dict(index)


def _infer_from_lease(stem: str, lease_index: dict,
                      need_township: bool, need_range: bool,
                      need_section: bool = False,
                      county: str = "",
                      ) -> tuple[str | None, str | None, str | None]:
    """
    Look up inferred (township, range, section) for a record by lease name + county.

    Uses (lease_name, county) as the composite key so that wells from different
    counties that share a lease name cannot cross-contaminate PLSS values.

    Returns the inferred value only when ALL resolved neighbors agree
    (singleton set). Returns None for a field if there is no consensus.
    """
    lease = _extract_lease_name(stem)
    key   = (lease, county)
    if not lease or key not in lease_index:
        return None, None, None

    entry = lease_index[key]

    twp_inf  = None
    rng_inf  = None
    sect_inf = None

    if need_township:
        twp_set = entry.get("township", set())
        if len(twp_set) == 1:
            twp_inf = next(iter(twp_set))

    if need_range:
        rng_set = entry.get("range", set())
        if len(rng_set) == 1:
            rng_inf = next(iter(rng_set))

    if need_section:
        sect_set = entry.get("section", set())
        if len(sect_set) == 1:
            sect_inf = next(iter(sect_set))

    return twp_inf, rng_inf, sect_inf


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

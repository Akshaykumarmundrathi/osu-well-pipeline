"""
Per-record, per-stage processing tracker backed by a CSV file.

Schema (one row per PDF):
  pdf_stem | pdf_path | collection | year | month |
  grid_status | grid_confidence | grid_page | grid_method |
  location_status | location_confidence | location_section | location_township | location_range |
  county_status | county_confidence | county_name | county_score |
  last_updated

Status values: pending | done | failed | skipped

Saves are batched (SAVE_INTERVAL updates) to avoid O(n^2) I/O on large runs.
Call force_save() at month/year boundaries and on process exit.
"""

import csv
import time
from datetime import datetime, timezone
from pathlib import Path

from config import ALL_STAGES

PENDING = "pending"
DONE    = "done"
FAILED  = "failed"
SKIPPED = "skipped"

SAVE_INTERVAL = 25  # rewrite CSV after this many mark_done/mark_failed calls

_FIELDNAMES = [
    # Identity / traceability — enough to reconstruct the source path on any system
    "pdf_stem", "pdf_path", "zip_path", "internal_path",
    "collection", "collection_num", "year", "month",
    "model_tier", "decade",
    # latlong (first stage)
    "latlong_status", "latlong_confidence", "latlong_error_type",
    "latlong_lat", "latlong_lon", "latlong_well_type", "latlong_page",
    # latlong audit — how it was extracted
    "latlong_method", "latlong_form_type",
    # Location: header block fields (Form 1002A / Locate Well)
    "header_county", "header_section", "header_township", "header_range",
    "header_quad_raw", "header_quad_type", "header_quad_db",
    "header_quad_row", "header_quad_col",
    "header_feet", "header_rel_x", "header_rel_y",
    # grid
    "grid_status",     "grid_confidence",  "grid_error_type",
    "grid_page",       "grid_method",      "grid_image_path",
    # location
    "location_status", "location_confidence", "location_error_type",
    "location_section", "location_township", "location_range",
    "location_quadrant_pdf", "location_quadrant_db",
    "location_quadrant_row", "location_quadrant_col",
    "location_quadrant_confidence",
    # county
    "county_status",   "county_confidence", "county_error_type",
    "county_name",     "county_score",
    # dot (U-Net well-spot detection on grid image)
    "dot_status",      "dot_confidence",    "dot_error_type",
    "dot_row",         "dot_col",           "dot_nw",
    "dot_x_norm",      "dot_y_norm",
    # end-to-end coordinate derivation audit
    "coord_derivation",        # latlong_direct|dot_interpolation|not_resolved
    "coord_latlong_source",    # labeled_decimal|dms|unlabeled_decimal|form_1002a|""
    "coord_section_source",    # header_block|ocr_extracted|not_found
    "coord_township_source",   # header_block|ocr_extracted|inferred|not_found
    "coord_range_source",      # header_block|ocr_extracted|inferred|not_found
    "coord_county_used",       # which county string was used for RDS resolution
    "coord_dot_source",        # unet|ocr_quadrant|both|not_found
    "last_updated",
]

_EMPTY_ROW = {f: "" for f in _FIELDNAMES}


def _now() -> str:
    """UTC timestamp string ('YYYY-MM-DDTHH:MM:SS')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _classify_error(error: str) -> str:
    """Heuristically derive an error_type slug from a free-text error string."""
    if not error:
        return "unknown"
    e = error.lower()
    if "503" in e or "unavailable" in e or "socket" in e or "handshaker" in e:
        return "api_error"
    if "keyword_not_found" in e:
        return "keyword_not_found"
    if "no_match" in e:
        return "no_match"
    if "not_detected" in e or "no grid" in e:
        return "not_detected"
    if "not_found" in e:
        return "not_found"
    if "invalid_crop" in e:
        return "invalid_crop"
    return "exception"


class ProcessingStatus:
    """In-memory mirror of processing_status.csv with batched writes."""

    def __init__(self, csv_path: Path):
        """Load existing CSV (if any) into self._rows keyed by pdf_stem."""
        self.csv_path = csv_path
        self._rows: dict[str, dict] = {}
        self._pending = 0
        self._load()

    # -- I/O ------------------------------------------------------------------

    def _load(self):
        """Read existing rows, fill missing columns from new schema with ''."""
        if not self.csv_path.exists():
            return
        with self.csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                full = dict(_EMPTY_ROW)
                full.update(row)
                self._rows[full["pdf_stem"]] = full

    def save(self):
        """
        Rewrite the entire CSV from current in-memory rows.

        Writes to a sibling .tmp file first, then renames over the
        original — atomic on Windows/POSIX. Ctrl-C during the write
        is deferred until after the rename so the on-disk file is
        never half-written.
        """
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.csv_path.with_suffix(self.csv_path.suffix + ".tmp")
        pending_interrupt = None
        try:
            with tmp.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_FIELDNAMES,
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerows(self._rows.values())
        except KeyboardInterrupt as exc:
            # Finish writing before re-raising.
            pending_interrupt = exc
        # On Windows another process (e.g. Excel) may hold a read lock on the
        # CSV briefly. Retry the atomic rename for up to ~5 seconds before
        # giving up — long enough to outlast transient locks without hanging.
        for attempt in range(10):
            try:
                tmp.replace(self.csv_path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.5)
        if pending_interrupt is not None:
            raise pending_interrupt

    def force_save(self):
        """Flush pending updates to disk; call at boundaries and process exit."""
        self.save()
        self._pending = 0

    # -- Record management ----------------------------------------------------

    def init_record(self, pdf_stem: str, pdf_path: str,
                    collection: str = "", year: str = "", month: str = "",
                    zip_path: str = "", internal_path: str = "",
                    collection_num: int = 0,
                    model_tier: str = "", decade: str = ""):
        """Create a PENDING row for `pdf_stem` if it doesn't already exist."""
        if pdf_stem in self._rows:
            return
        row = dict(_EMPTY_ROW)
        row.update({
            "pdf_stem":      pdf_stem,
            "pdf_path":      pdf_path,
            "zip_path":      zip_path,
            "internal_path": internal_path,
            "collection":    collection,
            "collection_num": str(collection_num),
            "year":          year,
            "month":         month,
            "model_tier":    model_tier,
            "decade":        decade,
            "latlong_status":  PENDING,
            "grid_status":     PENDING,
            "location_status": PENDING,
            "county_status":   PENDING,
            "dot_status":      PENDING,
            "last_updated": _now(),
        })
        self._rows[pdf_stem] = row

    def mark_done(self, pdf_stem: str, stage: str, result: dict):
        """
        Mark stage done and store structured fields extracted from result dict.
          grid     -> confidence, page, method
          location -> confidence, section, township, range
          county   -> confidence, name, fuzzy_score
        """
        confidence = str(result.get("confidence", 0))
        extra: dict = {}
        if stage == "latlong":
            extra = {
                "latlong_lat":        str(result.get("lat", "")),
                "latlong_lon":        str(result.get("lon", "")),
                "latlong_well_type":  result.get("well_type", ""),
                "latlong_page":       str(result.get("page", "")),
                "latlong_method":     result.get("latlong_method", ""),
                "latlong_form_type":  result.get("latlong_form_type", ""),
                "header_county":      result.get("header_county", ""),
                "header_section":     result.get("header_section", ""),
                "header_township":    result.get("header_township", ""),
                "header_range":       result.get("header_range", ""),
                "header_quad_raw":    result.get("header_quad_raw", ""),
                "header_quad_type":   result.get("header_quad_type", ""),
                "header_quad_db":     result.get("header_quad_db", ""),
                "header_quad_row":    result.get("header_quad_row", ""),
                "header_quad_col":    result.get("header_quad_col", ""),
                "header_feet":        result.get("header_feet", ""),
                "header_rel_x":       result.get("header_rel_x", ""),
                "header_rel_y":       result.get("header_rel_y", ""),
            }
        elif stage == "grid":
            extra = {
                "grid_page":       str(result.get("page", "")),
                "grid_method":     result.get("method", ""),
                "grid_image_path": result.get("image_path", ""),
            }
        elif stage == "location":
            extra = {
                "location_section":              result.get("section", ""),
                "location_township":             result.get("township", ""),
                "location_range":                result.get("range", ""),
                "location_quadrant_pdf":         result.get("quadrant_pdf", ""),
                "location_quadrant_db":          result.get("quadrant_db", ""),
                "location_quadrant_row":         result.get("quadrant_row", ""),
                "location_quadrant_col":         result.get("quadrant_col", ""),
                "location_quadrant_confidence":  str(result.get("quadrant_confidence", "")),
            }
        elif stage == "county":
            extra = {
                "county_name":  result.get("name", ""),
                "county_score": str(result.get("fuzzy_score", 0)),
            }
        elif stage == "dot":
            extra = {
                "dot_row":    str(result.get("row", "")),
                "dot_col":    str(result.get("col", "")),
                "dot_nw":     result.get("nw", ""),
                "dot_x_norm": str(result.get("x_norm", "")),
                "dot_y_norm": str(result.get("y_norm", "")),
            }
        self._update(pdf_stem, stage, DONE, confidence, extra)

    def mark_failed(self, pdf_stem: str, stage: str, error: str = "",
                    error_type: str = ""):
        """
        Mark stage FAILED and record the *type* of failure so the retry
        dispatcher can pick an appropriate strategy. Common error_type values:
          - 'api_error'         transient Vision/Gemini failure
          - 'keyword_not_found' anchor keyword absent on scanned pages
          - 'no_match'          deterministic model returned no candidate
          - 'not_detected'      heuristic detector found nothing
          - 'invalid_crop'      bbox computation produced empty region
          - 'exception'         unhandled exception (text in error)
        """
        et = error_type or _classify_error(error)
        self._update(pdf_stem, stage, FAILED, "",
                     {f"{stage}_error_type": et})

    def mark_skipped(self, pdf_stem: str, stage: str):
        """Mark stage SKIPPED (e.g. grid/location when lat/lon was found)."""
        self._update(pdf_stem, stage, SKIPPED, "", {})

    def is_done(self, pdf_stem: str, stage: str) -> bool:
        """True iff the given stage is in the DONE state for this record."""
        return self._rows.get(pdf_stem, {}).get(f"{stage}_status") == DONE

    def is_done_or_skipped(self, pdf_stem: str, stage: str) -> bool:
        """True iff the stage is DONE or SKIPPED (both are terminal non-error states)."""
        return self._rows.get(pdf_stem, {}).get(f"{stage}_status") in (DONE, SKIPPED)

    def get_status(self, pdf_stem: str, stage: str) -> str:
        """Return the raw status string for a (stem, stage) pair."""
        return self._rows.get(pdf_stem, {}).get(f"{stage}_status", PENDING)

    def get_error_type(self, pdf_stem: str, stage: str) -> str:
        """Return the recorded error_type for a failed stage, or '' if none."""
        return self._rows.get(pdf_stem, {}).get(f"{stage}_error_type", "")

    def mark_coord_audit(self, pdf_stem: str, derivation: str,
                         latlong_source: str = "",
                         section_source: str = "",
                         township_source: str = "",
                         range_source: str = "",
                         county_used: str = "",
                         dot_source: str = ""):
        """
        Store end-to-end coordinate derivation provenance.

        derivation:
          'latlong_direct'      — lat/lon extracted directly from PDF
          'dot_interpolation'   — derived via grid + dot + PLSS RDS
          'form_1002a_latlong'  — labeled coordinates on Form 1002A
          'not_resolved'        — coordinate could not be determined
        """
        self._update(pdf_stem, "coord", "", "", {
            "coord_derivation":      derivation,
            "coord_latlong_source":  latlong_source,
            "coord_section_source":  section_source,
            "coord_township_source": township_source,
            "coord_range_source":    range_source,
            "coord_county_used":     county_used,
            "coord_dot_source":      dot_source,
        })

    def latlong_detected(self, pdf_stem: str) -> bool:
        """True iff both lat and lon were stored — used to skip grid/location."""
        row = self._rows.get(pdf_stem, {})
        return bool(row.get("latlong_lat")) and bool(row.get("latlong_lon"))

    def all_done(self, pdf_stem: str) -> bool:
        """True iff every stage in ALL_STAGES is DONE for this record."""
        return all(self.is_done(pdf_stem, s) for s in ALL_STAGES)

    def failed_in(self, stems: list[str], stages: tuple) -> list[str]:
        """Subset of `stems` that have at least one FAILED stage in `stages`."""
        return [
            s for s in stems
            if any(self.get_status(s, st) == FAILED for st in stages)
        ]

    def counts(self) -> dict:
        """Per-stage tally: {stage: {done, failed, pending, skipped: int}}."""
        totals = {s: {DONE: 0, FAILED: 0, PENDING: 0, SKIPPED: 0} for s in ALL_STAGES}
        for row in self._rows.values():
            for s in ALL_STAGES:
                st = row.get(f"{s}_status", PENDING)
                totals[s][st] = totals[s].get(st, 0) + 1
        return totals

    # -- Internal -------------------------------------------------------------

    def _update(self, pdf_stem: str, stage: str,
                status: str, confidence: str, extra: dict):
        """
        Write the stage's status + confidence + any stage-specific extras
        to the row, bump last_updated, and flush to disk every
        SAVE_INTERVAL changes.
        """
        if pdf_stem not in self._rows:
            row = dict(_EMPTY_ROW)
            row["pdf_stem"] = pdf_stem
            self._rows[pdf_stem] = row
        row = self._rows[pdf_stem]
        row[f"{stage}_status"]     = status
        row[f"{stage}_confidence"] = confidence
        if extra:
            row.update(extra)
        row["last_updated"] = _now()

        self._pending += 1
        if self._pending >= SAVE_INTERVAL:
            self.save()
            self._pending = 0

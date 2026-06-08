"""
Per-record, per-stage processing tracker backed by a CSV file.

Schema (49 columns, one row per PDF):

  Identity      : pdf_stem, pdf_path, zip_path, collection, collection_num, year, month, model_tier
  Lat/Lon stage : latlong_status, latlong_confidence, latlong_error_type, latlong_lat, latlong_lon, latlong_page
  Grid stage    : grid_status, grid_confidence, grid_error_type, grid_page, grid_method, grid_image_path,
                  grid_form_type, grid_str_zone, grid_county_format_hint,
                  grid_str_strategy_hint, grid_anchor_phrase
  Location stage: location_status, location_confidence, location_error_type,
                  location_section, location_township, location_range,
                  location_quadrant_pdf, location_quadrant_db
  County stage  : county_status, county_confidence, county_error_type, county_name, county_score
  Dot stage     : dot_status, dot_confidence, dot_error_type,
                  dot_row, dot_col, dot_nw, dot_x_norm, dot_y_norm
  Coord audit   : coord_derivation, coord_latlong_source
  Housekeeping  : last_updated

Status values: pending | done | failed | skipped

Saves are batched (SAVE_INTERVAL updates) to avoid O(n²) I/O on large runs.
Call force_save() at month/year boundaries and on process exit.
"""

import csv
import threading
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
    # Identity / traceability
    "pdf_stem", "pdf_path", "zip_path",
    "collection", "collection_num", "year", "month", "model_tier",
    # latlong stage
    "latlong_status", "latlong_confidence", "latlong_error_type",
    "latlong_lat", "latlong_lon", "latlong_page",
    # grid stage
    "grid_status",     "grid_confidence",  "grid_error_type",
    "grid_page",       "grid_method",      "grid_image_path",
    # grid form-type classifier outputs (populated after grid detection)
    "grid_form_type",  "grid_str_zone",    "grid_county_format_hint",
    "grid_str_strategy_hint",              "grid_anchor_phrase",
    # location stage (Section / Township / Range)
    "location_status", "location_confidence", "location_error_type",
    "location_section", "location_township", "location_range",
    "location_quadrant_pdf", "location_quadrant_db",
    # county stage
    "county_status",   "county_confidence", "county_error_type",
    "county_name",     "county_score",
    # dot stage (U-Net well-spot on grid image)
    "dot_status",      "dot_confidence",    "dot_error_type",
    "dot_row",         "dot_col",           "dot_nw",
    "dot_x_norm",      "dot_y_norm",
    # coordinate derivation summary
    "coord_derivation",      # latlong_direct|dot_interpolation|not_resolved
    "coord_latlong_source",  # labeled_decimal|dms|unlabeled_decimal|""
    # housekeeping
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
    # API / network errors (Vision API + Gemini)
    if any(x in e for x in (
        "503", "unavailable", "socket", "handshaker",
        "deadline", "timeout", "permission", "unauthenticated",
        "credentials", "api_error", "quota", "resource_exhausted",
        "429", "401", "403",
    )):
        return "api_error"
    # Dot-specific
    if "model_load_failed" in e or "unet" in e or "checkpoint" in e:
        return "model_load_failed"
    if "grid_image_not_found" in e:
        return "grid_image_not_found"
    # Named sentinel strings returned directly as error values
    if "keyword_not_found" in e:
        return "keyword_not_found"
    if "no_match" in e:
        return "no_match"
    if "not_detected" in e or "no grid" in e or "no_dot_found" in e:
        return "not_detected"
    if "not_found" in e:
        return "not_found"
    if "invalid_crop" in e:
        return "invalid_crop"
    # "no_change" is the sentinel returned by _retry_record when _retry_one_stage
    # gives back None (no retry strategy).  It is NOT a real error type; callers
    # should guard against it before calling mark_failed, but handle it gracefully
    # here too so it never surfaces as "exception" in the CSV.
    if e == "no_change":
        return "not_detected"
    return "exception"


class ProcessingStatus:
    """In-memory mirror of processing_status.csv with batched writes."""

    def __init__(self, csv_path: Path):
        """Load existing CSV (if any) into self._rows keyed by pdf_stem."""
        self.csv_path = csv_path
        self._rows: dict[str, dict] = {}
        self._pending = 0
        # RLock so that force_save() → save() and _update() → _save_locked()
        # can re-enter without deadlock, and concurrent workers don't corrupt
        # the shared .tmp file.
        self._lock = threading.RLock()
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

        Thread-safe: acquires _lock before writing so concurrent workers
        can't both write to the same .tmp file and race on the rename.
        """
        with self._lock:
            self._save_locked()

    def _save_locked(self):
        """
        Write CSV to a sibling .new file then rename — MUST be called with
        self._lock held.

        Uses .new (not .tmp) extension: Windows Defender quarantines large
        .tmp files immediately after creation, causing the rename to fail.

        Write-then-rename is atomic on Windows/POSIX. Ctrl-C during the write
        is deferred until after the rename so the on-disk file is never
        half-written.
        """
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        # NOTE: Do NOT use ".tmp" extension — Windows Defender/CFA quarantines
        # large .tmp files immediately after creation, causing the rename to fail.
        # Use ".new" instead (plain text, no special-case treatment by AV).
        tmp = self.csv_path.with_suffix(self.csv_path.suffix + ".new")
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
        except Exception as exc:
            # Any other write-side failure: log it and re-raise so the caller
            # gets a clear traceback rather than a confusing FileNotFoundError
            # from the rename step (tmp may not exist if open() itself failed).
            import logging as _logging
            _logging.getLogger(__name__).error(
                "_save_locked write FAILED (%s: %s); tmp=%s exists=%s",
                type(exc).__name__, exc, tmp, tmp.exists()
            )
            raise
        # On Windows another process (e.g. Excel) may hold a read lock on the
        # CSV briefly. Retry the atomic rename for up to ~5 seconds before
        # giving up — long enough to outlast transient locks without hanging.
        for attempt in range(10):
            try:
                tmp.replace(self.csv_path)
                break
            except (PermissionError, FileNotFoundError) as exc:
                if attempt == 9:
                    raise
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "_save_locked rename attempt %d failed (%s): tmp=%s exists=%s",
                    attempt, exc, tmp, tmp.exists()
                )
                time.sleep(0.5)
        if pending_interrupt is not None:
            raise pending_interrupt

    def force_save(self):
        """Flush pending updates to disk; call at boundaries and process exit."""
        with self._lock:
            self._save_locked()
            self._pending = 0

    # -- Record management ----------------------------------------------------

    def init_record(self, pdf_stem: str, pdf_path: str,
                    collection: str = "", year: str = "", month: str = "",
                    zip_path: str = "", collection_num: int = 0,
                    model_tier: str = ""):
        """Create a PENDING row for `pdf_stem` if it doesn't already exist."""
        with self._lock:
            self._init_record_locked(pdf_stem, pdf_path, collection, year, month,
                                     zip_path, collection_num, model_tier)

    def _init_record_locked(self, pdf_stem: str, pdf_path: str,
                            collection: str, year: str, month: str,
                            zip_path: str, collection_num: int, model_tier: str):
        """Inner implementation — must be called with self._lock held."""
        if pdf_stem in self._rows:
            return
        row = dict(_EMPTY_ROW)
        row.update({
            "pdf_stem":       pdf_stem,
            "pdf_path":       pdf_path,
            "zip_path":       zip_path,
            "collection":     collection,
            "collection_num": str(collection_num),
            "year":           year,
            "month":          month,
            "model_tier":     model_tier,
            "latlong_status":  PENDING,
            "grid_status":     PENDING,
            "location_status": PENDING,
            "county_status":   PENDING,
            "dot_status":      PENDING,
            "last_updated":    _now(),
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
                "latlong_lat":  str(result.get("lat", "")),
                "latlong_lon":  str(result.get("lon", "")),
                "latlong_page": str(result.get("page", "")),
            }
        elif stage == "grid":
            extra = {
                "grid_page":               str(result.get("page", "")),
                "grid_method":             result.get("method", ""),
                "grid_image_path":         result.get("image_path", ""),
                # Form-type classifier outputs — consumed on resume to
                # reconstruct hints for location + county dispatching.
                "grid_form_type":          str(result.get("form_type", "") or ""),
                "grid_str_zone":           str(result.get("str_zone", "") or ""),
                "grid_county_format_hint": str(result.get("county_format_hint", "") or ""),
                "grid_str_strategy_hint":  str(result.get("str_strategy_hint", "") or ""),
                "grid_anchor_phrase":      str(result.get("anchor_phrase", "") or ""),
            }
        elif stage == "location":
            extra = {
                "location_section":      result.get("section", ""),
                "location_township":     result.get("township", ""),
                "location_range":        result.get("range", ""),
                "location_quadrant_pdf": result.get("quadrant_pdf", ""),
                "location_quadrant_db":  result.get("quadrant_db", ""),
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
                         latlong_source: str = "", **_ignored):
        """
        Store end-to-end coordinate derivation provenance.

        derivation:
          'latlong_direct'      — lat/lon extracted directly from PDF
          'dot_interpolation'   — derived via grid + dot + PLSS RDS
          'form_1002a_latlong'  — labeled coordinates on Form 1002A
          'not_resolved'        — coordinate could not be determined

        Extra kwargs are accepted and silently ignored for backward compatibility
        with callers that still pass section_source / county_used / etc.
        """
        self._update(pdf_stem, "coord", "", "", {
            "coord_derivation":     derivation,
            "coord_latlong_source": latlong_source,
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

        Thread-safe: holds _lock for the entire in-memory mutation and the
        conditional disk flush so concurrent workers don't interleave dict
        writes or race on the .tmp rename.
        """
        with self._lock:
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
                self._save_locked()   # already holding _lock — RLock allows re-entry
                self._pending = 0

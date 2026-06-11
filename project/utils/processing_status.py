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
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from config import ALL_STAGES

log = logging.getLogger(__name__)

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
    # gemini_disabled: Gemini was bypassed (GEMINI_DISABLED=1) and Vision OCR
    # alone could not identify the county — outcome is the same as no_match.
    if "gemini_disabled" in e or "gemini disabled" in e:
        return "no_match"
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


# ---------------------------------------------------------------------------
# Cross-process advisory lock — used by _save_locked so that multiprocessing
# workers don't simultaneously overwrite processing_status.csv.
#
# Uses os.O_CREAT | os.O_EXCL which is atomic on NTFS and POSIX.
# Stale locks (from crashed workers) are detected via PID liveness check and
# automatically stolen so a single crash never wedges the whole run.
# ---------------------------------------------------------------------------

def _proc_lock_acquire(lock_path: Path, timeout: float = 30.0) -> bool:
    """
    Exclusively create `lock_path` as a cross-process advisory lock.

    Returns True when the lock is held; False if `timeout` seconds elapse
    without acquiring it.  Stale locks from dead PIDs are stolen automatically.
    """
    deadline = time.monotonic() + timeout
    delay    = 0.05   # start at 50 ms, double each retry up to 500 ms cap
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()}\n".encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            # Check whether the lock-holding PID is still alive.
            try:
                pid = int(lock_path.read_text().strip())
                if pid != os.getpid():
                    try:
                        os.kill(pid, 0)   # signal 0 = liveness probe only
                    except ProcessLookupError:
                        # Lock-holder is dead — steal the lock.
                        try:
                            lock_path.unlink(missing_ok=True)
                            log.warning(
                                "Stole stale processing_status lock from dead PID %d", pid
                            )
                        except Exception:
                            pass
                        continue   # retry O_EXCL open immediately
                    except PermissionError:
                        pass       # PID exists — wait normally
            except Exception:
                pass               # unreadable lock file — wait and retry
            time.sleep(min(delay, 0.5))
            delay = min(delay * 2, 0.5)
        except OSError:
            time.sleep(0.1)
    return False


def _proc_lock_release(lock_path: Path):
    """Release the cross-process advisory lock."""
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


class ProcessingStatus:
    """In-memory mirror of processing_status.csv with batched writes."""

    def __init__(self, csv_path: Path, stems_filter: set[str] | None = None):
        """Load existing CSV (if any) into self._rows keyed by pdf_stem.

        ``stems_filter`` — when given, only rows whose pdf_stem is in the set
        are loaded into memory.  SAFE because _save_locked() merge-writes
        against the on-disk file (never self._rows wholesale), so unloaded
        rows are preserved verbatim on every save.  Scopes a run to its
        index: the full file is 514K rows (~13s load); a 3,700-stem run
        needs only its own rows (<0.5s).  Callers must ensure every stem
        they will query is in the filter — get_status returns '' (pending)
        for unloaded stems.
        """
        self.csv_path  = csv_path
        self._stems_filter = stems_filter

        # ── Sharded writes (cross-RUN contention fix) ─────────────────────
        # Concurrent runs against one 130MB file starve each other: every
        # save merge-rewrites the FULL file holding one advisory lock for
        # 10-20s.  With STATUS_SHARD_SUFFIX set (one per run), THIS run's
        # saves go to processing_status.<suffix>.csv — small file, its own
        # lock, zero cross-run contention.  Loads always read the base file
        # PLUS every shard (shards override base; newest shard wins) so all
        # runs see each other's completed work.  consolidate_status.py folds
        # shards back into the base when no runs are active.
        _suffix = os.environ.get("STATUS_SHARD_SUFFIX", "").strip()
        self._base_path = csv_path
        if _suffix:
            self.csv_path = csv_path.with_name(
                csv_path.stem + f".{_suffix}" + csv_path.suffix)
        self._lock_path = self.csv_path.with_suffix(".csv.lock")
        self._rows: dict[str, dict] = {}
        # Track which stems *this process* has modified — used by _save_locked
        # to merge-write safely when multiple worker processes share the file.
        self._dirty: set[str] = set()
        self._pending = 0
        # RLock so that force_save() → save() and _update() → _save_locked()
        # can re-enter without deadlock, and concurrent workers don't corrupt
        # the shared .tmp file.
        self._lock = threading.RLock()
        self._load()

    # -- I/O ------------------------------------------------------------------

    def _load(self):
        """Read base + all shard files (shards override base, newest last)."""
        sources = []
        if self._base_path.exists():
            sources.append(self._base_path)
        # Other runs' shards + our own from a previous crash.
        shards = sorted(
            (p for p in self._base_path.parent.glob(
                self._base_path.stem + ".*" + self._base_path.suffix)
             if p != self._base_path and not p.name.endswith(".new")),
            key=lambda p: p.stat().st_mtime)
        sources.extend(shards)
        for src in sources:
            self._load_one(src)

    def _load_one(self, path: Path):
        if not path.exists():
            return
        _orig, self.csv_path = self.csv_path, path
        try:
            self._load_file()
        finally:
            self.csv_path = _orig

    def _load_file(self):
        """Read existing rows, fill missing columns from new schema with ''."""
        if not self.csv_path.exists():
            return
        # Guard against corrupt rows with fields > 1 MB (produced by the old
        # multi-process race condition where two workers interleaved writes to
        # the same .csv.new file).  Python's default limit is 128 KB; raise it
        # so we can read past pre-existing corruption, but drop any row whose
        # largest field exceeds 10 KB (no legitimate field is that long).
        csv.field_size_limit(2_000_000)
        bad_rows = 0
        with self.csv_path.open(newline="", encoding="utf-8",
                                 errors="replace") as f:
            if self._stems_filter is not None:
                # Fast path: csv.reader + first-column membership test, dict
                # built only for matching rows (DictReader on every row costs
                # ~3x more than raw reader on the 514K-row master file).
                reader = csv.reader(f)
                header = next(reader, None) or []
                try:
                    stem_i = header.index("pdf_stem")
                except ValueError:
                    stem_i = 0
                for vals in reader:
                    if len(vals) <= stem_i or vals[stem_i] not in self._stems_filter:
                        continue
                    if any(len(v) > 10_000 for v in vals):
                        bad_rows += 1
                        continue
                    full = dict(_EMPTY_ROW)
                    full.update(zip(header, vals))
                    self._rows[full["pdf_stem"]] = full
            else:
                for row in csv.DictReader(f):
                    if any(len(v) > 10_000 for v in row.values()):
                        bad_rows += 1
                        continue      # drop corrupt row — will be reprocessed
                    full = dict(_EMPTY_ROW)
                    full.update(row)
                    self._rows[full["pdf_stem"]] = full
        if bad_rows:
            log.warning(
                "_load: dropped %d corrupt row(s) from %s "
                "(fields > 10 KB — likely caused by a previous "
                "multi-process write race; rows will be reprocessed).",
                bad_rows, self.csv_path,
            )

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

        Multi-process safety (read-merge-write):
          1. Acquire cross-process advisory lock (_lock_path sentinel file).
          2. Re-read the current on-disk CSV.
          3. Overlay *only* the rows this process has modified (_dirty set).
             Rows modified by other workers since our last save are preserved.
          4. Write merged state to .new, rename atomically.
          5. Release the advisory lock.

        Uses .new (not .tmp) extension: Windows Defender quarantines large
        .tmp files immediately after creation, causing the rename to fail.
        """
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.csv_path.with_suffix(self.csv_path.suffix + ".new")

        # --- Acquire cross-process lock ---------------------------------------
        if not _proc_lock_acquire(self._lock_path):
            log.error(
                "_save_locked: could not acquire cross-process lock %s "
                "within 30 s — skipping this save cycle (data not lost: "
                "will retry on next SAVE_INTERVAL flush or force_save).",
                self._lock_path,
            )
            return

        try:
            # --- Re-read on-disk state so other workers' recent saves are kept.
            # merged starts as the full disk state; we then overlay only the rows
            # *this* process has modified.  Rows we never touched stay as-is.
            merged: dict[str, dict] = {}
            if self.csv_path.exists():
                try:
                    with self.csv_path.open(newline="", encoding="utf-8") as f:
                        for row in csv.DictReader(f):
                            full = dict(_EMPTY_ROW)
                            full.update(row)
                            merged[full["pdf_stem"]] = full
                except Exception as exc:
                    log.warning(
                        "_save_locked: re-read of disk CSV failed (%s) — "
                        "writing in-memory state only (some cross-worker "
                        "updates may be lost this cycle).", exc
                    )
                    merged = {}

            # Overlay this process's dirty rows onto the merged disk state.
            for stem in self._dirty:
                if stem in self._rows:
                    merged[stem] = self._rows[stem]

            # Sync our in-memory state to the merged view so future saves
            # start from a coherent baseline.
            self._rows = merged

            # --- Write merged state ------------------------------------------
            pending_interrupt = None
            try:
                with tmp.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=_FIELDNAMES,
                                            extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(merged.values())
            except KeyboardInterrupt as exc:
                pending_interrupt = exc
            except Exception as exc:
                log.error(
                    "_save_locked write FAILED (%s: %s); tmp=%s exists=%s",
                    type(exc).__name__, exc, tmp, tmp.exists()
                )
                raise

            # On Windows another process (e.g. Excel) may hold a read lock on
            # the CSV briefly. Retry the atomic rename for up to ~5 seconds.
            for attempt in range(10):
                try:
                    tmp.replace(self.csv_path)
                    break
                except (PermissionError, FileNotFoundError) as exc:
                    if attempt == 9:
                        raise
                    log.warning(
                        "_save_locked rename attempt %d failed (%s): "
                        "tmp=%s exists=%s",
                        attempt, exc, tmp, tmp.exists()
                    )
                    time.sleep(0.5)

            if pending_interrupt is not None:
                raise pending_interrupt

        finally:
            _proc_lock_release(self._lock_path)

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
        self._dirty.add(pdf_stem)

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
            self._dirty.add(pdf_stem)   # mark as modified by this process

            self._pending += 1
            if self._pending >= SAVE_INTERVAL:
                self._save_locked()   # already holding _lock — RLock allows re-entry
                self._pending = 0

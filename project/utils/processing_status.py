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
from datetime import datetime, timezone
from pathlib import Path

from config import ALL_STAGES

PENDING = "pending"
DONE    = "done"
FAILED  = "failed"
SKIPPED = "skipped"

SAVE_INTERVAL = 25  # rewrite CSV after this many mark_done/mark_failed calls

_FIELDNAMES = [
    "pdf_stem", "pdf_path", "collection", "year", "month",
    "grid_status",     "grid_confidence",     "grid_page",    "grid_method",
    "location_status", "location_confidence",
    "location_section", "location_township",  "location_range",
    "county_status",   "county_confidence",   "county_name",  "county_score",
    "last_updated",
]

_EMPTY_ROW = {f: "" for f in _FIELDNAMES}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class ProcessingStatus:
    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self._rows: dict[str, dict] = {}
        self._pending = 0
        self._load()

    # -- I/O ------------------------------------------------------------------

    def _load(self):
        if not self.csv_path.exists():
            return
        with self.csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                full = dict(_EMPTY_ROW)
                full.update(row)
                self._rows[full["pdf_stem"]] = full

    def save(self):
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self._rows.values())

    def force_save(self):
        """Explicitly flush — call at month/year boundaries and process exit."""
        self.save()
        self._pending = 0

    # -- Record management ----------------------------------------------------

    def init_record(self, pdf_stem: str, pdf_path: str,
                    collection: str = "", year: str = "", month: str = ""):
        if pdf_stem in self._rows:
            return
        row = dict(_EMPTY_ROW)
        row.update({
            "pdf_stem": pdf_stem, "pdf_path": pdf_path,
            "collection": collection, "year": year, "month": month,
            "grid_status":     PENDING,
            "location_status": PENDING,
            "county_status":   PENDING,
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
        if stage == "grid":
            extra = {
                "grid_page":   str(result.get("page", "")),
                "grid_method": result.get("method", ""),
            }
        elif stage == "location":
            extra = {
                "location_section":  result.get("section", ""),
                "location_township": result.get("township", ""),
                "location_range":    result.get("range", ""),
            }
        elif stage == "county":
            extra = {
                "county_name":  result.get("name", ""),
                "county_score": str(result.get("fuzzy_score", 0)),
            }
        self._update(pdf_stem, stage, DONE, confidence, extra)

    def mark_failed(self, pdf_stem: str, stage: str, error: str = ""):
        # Error details are written to failed_records.csv and the per-PDF log.
        # Status field alone is enough for resume/retry logic.
        self._update(pdf_stem, stage, FAILED, "", {})

    def mark_skipped(self, pdf_stem: str, stage: str):
        self._update(pdf_stem, stage, SKIPPED, "", {})

    def is_done(self, pdf_stem: str, stage: str) -> bool:
        return self._rows.get(pdf_stem, {}).get(f"{stage}_status") == DONE

    def get_status(self, pdf_stem: str, stage: str) -> str:
        return self._rows.get(pdf_stem, {}).get(f"{stage}_status", PENDING)

    def all_done(self, pdf_stem: str) -> bool:
        return all(self.is_done(pdf_stem, s) for s in ALL_STAGES)

    def failed_in(self, stems: list[str], stages: tuple) -> list[str]:
        """Return stems that have at least one FAILED stage in `stages`."""
        return [
            s for s in stems
            if any(self.get_status(s, st) == FAILED for st in stages)
        ]

    def counts(self) -> dict:
        totals = {s: {DONE: 0, FAILED: 0, PENDING: 0, SKIPPED: 0} for s in ALL_STAGES}
        for row in self._rows.values():
            for s in ALL_STAGES:
                st = row.get(f"{s}_status", PENDING)
                totals[s][st] = totals[s].get(st, 0) + 1
        return totals

    # -- Internal -------------------------------------------------------------

    def _update(self, pdf_stem: str, stage: str,
                status: str, confidence: str, extra: dict):
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

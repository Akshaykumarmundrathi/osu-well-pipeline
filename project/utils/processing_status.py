"""
Per-record, per-stage processing tracker backed by a CSV file.

Schema (one row per PDF):
  pdf_stem | pdf_path | collection | year | month |
  grid_status | grid_confidence | grid_detail |
  location_status | location_confidence | location_detail |
  county_status | county_confidence | county_detail |
  last_updated

Status values: pending | done | failed | skipped
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

from config import ALL_STAGES

PENDING = "pending"
DONE    = "done"
FAILED  = "failed"
SKIPPED = "skipped"

_FIELDNAMES = [
    "pdf_stem", "pdf_path", "collection", "year", "month",
    "grid_status",     "grid_confidence",     "grid_detail",
    "location_status", "location_confidence", "location_detail",
    "county_status",   "county_confidence",   "county_detail",
    "last_updated",
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class ProcessingStatus:
    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self._rows: dict[str, dict] = {}
        self._load()

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _load(self):
        if not self.csv_path.exists():
            return
        with self.csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                self._rows[row["pdf_stem"]] = row

    def save(self):
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
            writer.writeheader()
            writer.writerows(self._rows.values())

    # ── Record management ────────────────────────────────────────────────────

    def init_record(self, pdf_stem: str, pdf_path: str,
                    collection: str = "", year: str = "", month: str = ""):
        if pdf_stem in self._rows:
            return
        self._rows[pdf_stem] = {
            "pdf_stem": pdf_stem, "pdf_path": pdf_path,
            "collection": collection, "year": year, "month": month,
            "grid_status":     PENDING, "grid_confidence":     "", "grid_detail":     "",
            "location_status": PENDING, "location_confidence": "", "location_detail": "",
            "county_status":   PENDING, "county_confidence":   "", "county_detail":   "",
            "last_updated": _now(),
        }

    def mark_done(self, pdf_stem: str, stage: str,
                  confidence: int = 0, detail: str = ""):
        self._update(pdf_stem, stage, DONE, str(confidence), detail)

    def mark_failed(self, pdf_stem: str, stage: str, error: str = ""):
        self._update(pdf_stem, stage, FAILED, "", error[:120])

    def mark_skipped(self, pdf_stem: str, stage: str):
        self._update(pdf_stem, stage, SKIPPED, "", "")

    def is_done(self, pdf_stem: str, stage: str) -> bool:
        row = self._rows.get(pdf_stem, {})
        return row.get(f"{stage}_status") == DONE

    def get_status(self, pdf_stem: str, stage: str) -> str:
        row = self._rows.get(pdf_stem, {})
        return row.get(f"{stage}_status", PENDING)

    def all_done(self, pdf_stem: str) -> bool:
        return all(self.is_done(pdf_stem, s) for s in ALL_STAGES)

    def counts(self) -> dict:
        totals = {s: {DONE: 0, FAILED: 0, PENDING: 0, SKIPPED: 0} for s in ALL_STAGES}
        for row in self._rows.values():
            for s in ALL_STAGES:
                st = row.get(f"{s}_status", PENDING)
                totals[s][st] = totals[s].get(st, 0) + 1
        return totals

    # ── Internal ─────────────────────────────────────────────────────────────

    def _update(self, pdf_stem: str, stage: str,
                status: str, confidence: str, detail: str):
        if pdf_stem not in self._rows:
            self._rows[pdf_stem] = {f: "" for f in _FIELDNAMES}
            self._rows[pdf_stem]["pdf_stem"] = pdf_stem
        row = self._rows[pdf_stem]
        row[f"{stage}_status"]     = status
        row[f"{stage}_confidence"] = confidence
        row[f"{stage}_detail"]     = detail
        row["last_updated"]        = _now()
        self.save()

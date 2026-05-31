"""
safeguards.py — Proactive pipeline safety guards.

Checks run BEFORE each PDF and BEFORE each year-batch to stop problems before
they become crashes or data corruption:

  DiskGuard          — aborts if D: drive free space < threshold
  APIRateLimiter     — enforces per-minute call budgets for Vision + Gemini
  MemoryGuard        — warns / GC-collects if process RSS is excessive
  SafetyCoordinator  — single entry-point used by run_pipeline()

Usage (automatic via run_pipeline):
    coordinator = SafetyCoordinator()
    coordinator.check_before_batch("1911", n_records=397)
    for record in records:
        coordinator.check_before_record()   # may sleep for rate-limit
        process_record(...)
        coordinator.record_api_call("vision")
        coordinator.record_api_call("gemini")

Standalone check:
    python safeguards.py
"""

from __future__ import annotations

import gc
import logging
import shutil
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger("safeguards")

# ---------------------------------------------------------------------------
# Thresholds — edit these to match your environment / API tier
# ---------------------------------------------------------------------------

# Disk — derived from OUTPUT_ROOT env var so the correct drive is monitored
# regardless of where outputs are stored (D:\, C:\, E:\, etc.)
import os as _os
import sys as _sys2
_default_out = r"D:\project_outputs_local" if _sys2.platform == "win32" else "/tmp/output"
del _sys2
_output_root_str = _os.environ.get("OUTPUT_ROOT", _default_out)
_output_drive    = Path(_output_root_str).anchor   # e.g. "D:\\" or "/"
DISK_PATH        = Path(_output_drive) if _output_drive else Path("/tmp")
del _os, _output_root_str, _output_drive

DISK_WARN_GB      = 10.0   # log a warning below this
DISK_ABORT_GB     = 3.0    # raise DiskSpaceError below this

# Vision API (Google Cloud Vision)
# Free tier:  1800 req/min.  Paid: higher, but 1800 is a safe default.
VISION_RPM_LIMIT  = 1200   # 2/3 of quota cap — leaves headroom
VISION_RPB_LIMIT  = 10_000 # per-batch (per-year) hard ceiling; 0 = no limit

# Gemini Flash (paid tier: 1000 RPM; free tier: 10 RPM)
# Set to 50 if using paid, 8 if on free tier.
GEMINI_RPM_LIMIT  = 50     # adjust to your API tier
GEMINI_RPB_LIMIT  = 0      # no per-batch cap by default

# Memory
MEMORY_WARN_MB    = 2_000  # warn above this RSS
MEMORY_GC_MB      = 3_500  # force GC above this RSS
MEMORY_ABORT_MB   = 6_000  # raise MemoryError above this RSS (0 = disable)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DiskSpaceError(RuntimeError):
    """Raised when D: drive free space falls below DISK_ABORT_GB."""

class APIBudgetError(RuntimeError):
    """Raised when per-batch API call budget is exhausted."""


# ---------------------------------------------------------------------------
# Disk guard
# ---------------------------------------------------------------------------

class DiskGuard:
    """Checks available disk space on a given path."""

    def __init__(self, path: Path = DISK_PATH,
                 warn_gb: float = DISK_WARN_GB,
                 abort_gb: float = DISK_ABORT_GB):
        self.path     = path
        self.warn_gb  = warn_gb
        self.abort_gb = abort_gb

    def free_gb(self) -> float:
        try:
            usage = shutil.disk_usage(self.path)
            return usage.free / (1024 ** 3)
        except Exception as exc:
            logger.warning("DiskGuard: could not check %s: %s", self.path, exc)
            return float("inf")   # assume OK if we can't check

    def check(self) -> float:
        """
        Returns free GB.  Logs a warning if < warn_gb.
        Raises DiskSpaceError if < abort_gb.
        """
        free = self.free_gb()
        if free < self.abort_gb:
            msg = (
                f"DISK SPACE CRITICAL: {free:.1f} GB free on {self.path} "
                f"(abort threshold: {self.abort_gb:.1f} GB). "
                f"Pipeline halted to prevent output corruption."
            )
            logger.error(msg)
            raise DiskSpaceError(msg)
        if free < self.warn_gb:
            logger.warning(
                "DISK SPACE LOW: %.1f GB free on %s (warn threshold: %.1f GB)",
                free, self.path, self.warn_gb,
            )
        return free


# ---------------------------------------------------------------------------
# API rate limiter  (sliding-window token bucket)
# ---------------------------------------------------------------------------

class _SlidingWindowLimiter:
    """
    Sliding 60-second window rate limiter.
    call() blocks until the call is within the allowed rate.
    """

    def __init__(self, rpm: int, name: str):
        self.rpm      = rpm
        self.name     = name
        self._window  = deque()   # timestamps of recent calls (monotonic)
        self._lock    = threading.Lock()

    def acquire(self):
        """Block until a call slot is available, then consume it."""
        if self.rpm <= 0:
            return   # unlimited
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - 60.0
                # Evict calls older than 60 s
                while self._window and self._window[0] < cutoff:
                    self._window.popleft()

                if len(self._window) < self.rpm:
                    self._window.append(now)
                    return   # slot acquired

                # Must wait until the oldest call leaves the window
                oldest = self._window[0]
                wait   = (oldest + 60.0) - now + 0.05  # small buffer

            # Sleep outside the lock
            logger.debug(
                "Rate limit: %s  (%d/%d rpm used) — sleeping %.1fs",
                self.name, len(self._window), self.rpm, wait,
            )
            time.sleep(max(wait, 0.1))

    def current_rpm(self) -> int:
        """Approximate calls in the last 60 s (for status logging)."""
        with self._lock:
            now    = time.monotonic()
            cutoff = now - 60.0
            return sum(1 for t in self._window if t >= cutoff)


class APIRateLimiter:
    """
    Manages per-API rate limiters for Vision and Gemini.

    Usage:
        limiter = APIRateLimiter()
        limiter.acquire("vision")    # blocks if needed
        limiter.acquire("gemini")
        limiter.record("vision")     # just counts calls (no blocking)
    """

    def __init__(
        self,
        vision_rpm:  int = VISION_RPM_LIMIT,
        gemini_rpm:  int = GEMINI_RPM_LIMIT,
        vision_rpb:  int = VISION_RPB_LIMIT,
        gemini_rpb:  int = GEMINI_RPB_LIMIT,
    ):
        self._limiters = {
            "vision": _SlidingWindowLimiter(vision_rpm, "vision"),
            "gemini": _SlidingWindowLimiter(gemini_rpm, "gemini"),
        }
        self._rpb  = {"vision": vision_rpb, "gemini": gemini_rpb}
        self._batch: dict[str, int] = {"vision": 0, "gemini": 0}
        self._total: dict[str, int] = {"vision": 0, "gemini": 0}
        self._lock = threading.Lock()

    def acquire(self, api: str):
        """
        Must be called BEFORE making an API call.
        Blocks until the rate-limit window allows the call.
        Raises APIBudgetError if the per-batch cap is exceeded.
        """
        lim = self._limiters.get(api)
        if lim is None:
            return
        rpb = self._rpb.get(api, 0)
        if rpb > 0:
            with self._lock:
                if self._batch.get(api, 0) >= rpb:
                    raise APIBudgetError(
                        f"{api} per-batch budget exhausted ({rpb} calls). "
                        f"Increase VISION_RPB_LIMIT / GEMINI_RPB_LIMIT in safeguards.py "
                        f"or split into smaller batches."
                    )
        lim.acquire()
        with self._lock:
            self._batch[api]  = self._batch.get(api, 0) + 1
            self._total[api]  = self._total.get(api, 0) + 1

    def reset_batch_counts(self):
        """Call at the start of each year-batch to reset per-batch counters."""
        with self._lock:
            self._batch = {k: 0 for k in self._batch}

    def status(self) -> dict:
        return {
            api: {
                "batch_calls":  self._batch.get(api, 0),
                "total_calls":  self._total.get(api, 0),
                "current_rpm":  self._limiters[api].current_rpm(),
                "rpm_limit":    self._limiters[api].rpm,
            }
            for api in self._limiters
        }

    def log_status(self):
        for api, s in self.status().items():
            logger.info(
                "API[%s]  batch=%d  total=%d  current_rpm=%d/%d",
                api, s["batch_calls"], s["total_calls"],
                s["current_rpm"], s["rpm_limit"],
            )


# ---------------------------------------------------------------------------
# Memory guard
# ---------------------------------------------------------------------------

class MemoryGuard:
    """Monitors process RSS memory and takes action when thresholds are crossed."""

    def __init__(
        self,
        warn_mb:  float = MEMORY_WARN_MB,
        gc_mb:    float = MEMORY_GC_MB,
        abort_mb: float = MEMORY_ABORT_MB,
    ):
        self.warn_mb  = warn_mb
        self.gc_mb    = gc_mb
        self.abort_mb = abort_mb
        self._psutil_ok: bool | None = None

    def _rss_mb(self) -> float:
        if self._psutil_ok is False:
            return 0.0
        try:
            import psutil
            import os
            self._psutil_ok = True
            return psutil.Process(os.getpid()).memory_info().rss / (1024 ** 2)
        except ImportError:
            if self._psutil_ok is None:
                logger.debug("psutil not installed — memory guard disabled")
            self._psutil_ok = False
            return 0.0
        except Exception as exc:
            logger.debug("MemoryGuard._rss_mb: %s", exc)
            return 0.0

    def check(self) -> float:
        """Returns current RSS in MB; may GC or raise MemoryError."""
        rss = self._rss_mb()
        if rss <= 0:
            return rss

        if self.abort_mb > 0 and rss > self.abort_mb:
            msg = (
                f"MEMORY CRITICAL: RSS={rss:.0f} MB exceeds abort threshold "
                f"{self.abort_mb:.0f} MB. Halting to prevent OOM crash."
            )
            logger.error(msg)
            raise MemoryError(msg)

        if rss > self.gc_mb:
            logger.warning(
                "MEMORY HIGH: RSS=%.0f MB > gc threshold %.0f MB — forcing GC",
                rss, self.gc_mb,
            )
            gc.collect()

        elif rss > self.warn_mb:
            logger.warning(
                "MEMORY WARNING: RSS=%.0f MB (warn threshold: %.0f MB)",
                rss, self.warn_mb,
            )

        return rss


# ---------------------------------------------------------------------------
# Safety coordinator
# ---------------------------------------------------------------------------

class SafetyCoordinator:
    """
    Single entry-point for all safeguards.
    Create one instance per pipeline run and wire it into run_pipeline().

    Example
    -------
        safety = SafetyCoordinator()
        for year in years:
            safety.check_before_batch(year, n_records)
            for record in records:
                safety.check_before_record()
                process_record(...)
                safety.record_api_call("vision")
                safety.record_api_call("gemini")
    """

    def __init__(
        self,
        disk_path:   Path  = DISK_PATH,
        vision_rpm:  int   = VISION_RPM_LIMIT,
        gemini_rpm:  int   = GEMINI_RPM_LIMIT,
    ):
        self.disk    = DiskGuard(path=disk_path)
        self.api     = APIRateLimiter(vision_rpm=vision_rpm, gemini_rpm=gemini_rpm)
        self.memory  = MemoryGuard()
        self._record_count = 0
        self._last_disk_check = 0.0   # monotonic time of last disk check

    # -- Batch-level checks (once per year) ----------------------------------

    def check_before_batch(self, year: str | int, n_records: int):
        """
        Run full checks before starting a new year-batch.
        Logs disk space, resets per-batch API counters.
        """
        self.api.reset_batch_counts()
        free_gb = self.disk.check()    # raises DiskSpaceError if critical
        logger.info(
            "Batch start  year=%s  records=%d  disk_free=%.1f GB",
            year, n_records, free_gb,
        )
        self.memory.check()
        self._last_disk_check = time.monotonic()

    # -- Per-record checks (once per PDF) ------------------------------------

    def check_before_record(self):
        """
        Lightweight per-record check.
        - Checks disk every 200 records (or ~3 min at 1 s/PDF).
        - Checks memory every record.
        - Does NOT rate-limit here — callers use acquire_api() instead.
        """
        self._record_count += 1

        # Disk: re-check every 200 records (~every ~3 min at 1 s/pdf)
        if self._record_count % 200 == 0:
            elapsed = time.monotonic() - self._last_disk_check
            if elapsed >= 60:   # at least 60 s since last check
                self.disk.check()
                self._last_disk_check = time.monotonic()

        # Memory: every record
        self.memory.check()

    # -- API rate-limiting wrappers ------------------------------------------

    def acquire_vision(self):
        """Call before every Vision API request. Blocks if rate-limited."""
        self.api.acquire("vision")

    def acquire_gemini(self):
        """Call before every Gemini API request. Blocks if rate-limited."""
        self.api.acquire("gemini")

    # -- Summary logging ------------------------------------------------------

    def log_api_status(self):
        self.api.log_status()
        rss = self.memory._rss_mb()
        if rss > 0:
            logger.info("Memory RSS: %.0f MB", rss)


# ---------------------------------------------------------------------------
# Standalone check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    safety = SafetyCoordinator()
    print("Running safeguard checks...")
    try:
        free = safety.disk.check()
        print(f"  Disk  : {free:.1f} GB free on {DISK_PATH}  [OK]")
    except DiskSpaceError as e:
        print(f"  Disk  : CRITICAL — {e}")

    rss = safety.memory._rss_mb()
    if rss > 0:
        print(f"  Memory: {rss:.0f} MB RSS  [OK]")
    else:
        print("  Memory: psutil not available — install with: pip install psutil")

    print(f"\nAPI rate limits configured:")
    print(f"  Vision: {VISION_RPM_LIMIT} RPM  (per-batch cap: {VISION_RPB_LIMIT or 'unlimited'})")
    print(f"  Gemini: {GEMINI_RPM_LIMIT} RPM  (per-batch cap: {GEMINI_RPB_LIMIT or 'unlimited'})")
    print(f"\nThresholds:")
    print(f"  Disk abort : {DISK_ABORT_GB} GB free")
    print(f"  Disk warn  : {DISK_WARN_GB} GB free")
    print(f"  Mem warn   : {MEMORY_WARN_MB} MB RSS")
    print(f"  Mem GC     : {MEMORY_GC_MB} MB RSS")
    print(f"  Mem abort  : {MEMORY_ABORT_MB} MB RSS  (0=disabled)")

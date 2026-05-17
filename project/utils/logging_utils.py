"""
Logging helpers.

Console (stdout):
  - Pipeline-level messages (main.py) use plain print() — no logger needed.
  - Per-PDF file logs (DEBUG+) go to logs/{stem}.log for diagnostics.
  - Only ERRORs from stage functions appear on console (via pdf_logger).

File handler keeps full detail: timestamp, level, logger name.
"""

import logging
import sys
from pathlib import Path

_FILE_FMT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

# Console: just the message, no decoration
_CONSOLE_FMT = logging.Formatter("%(message)s")


def get_logger(name: str) -> logging.Logger:
    """
    Pipeline-level logger (main.py). INFO+ to stdout, no timestamps.
    In practice, most pipeline output now uses print() directly — this
    is kept for compatibility with scan_dataset and other helpers.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        sh = logging.StreamHandler(stream=sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(_CONSOLE_FMT)
        logger.addHandler(sh)
    return logger


def get_pdf_logger(pdf_stem: str, log_path: Path) -> logging.Logger:
    """
    Per-PDF logger:
      - File: DEBUG+ with full timestamp/level (for post-run diagnostics).
      - Console: ERROR only — stage warnings/info are shown via main.py's
        clean print() output instead.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    name   = f"pdf.{pdf_stem}"
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # Console: errors only, clean format
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setLevel(logging.ERROR)
    sh.setFormatter(_CONSOLE_FMT)
    logger.addHandler(sh)

    # File: full detail
    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_FILE_FMT)
    logger.addHandler(fh)

    return logger

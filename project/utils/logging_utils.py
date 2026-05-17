import logging
from pathlib import Path

_FMT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def get_logger(name: str) -> logging.Logger:
    """Console-only module logger. Call with __name__."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        sh = logging.StreamHandler(stream=__import__("sys").stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(_FMT)
        logger.addHandler(sh)
    return logger


def get_pdf_logger(pdf_stem: str, log_path: Path) -> logging.Logger:
    """
    Logger that writes DEBUG+ to a per-PDF file and INFO+ to console.
    Creates a uniquely named logger so it doesn't pollute the root namespace.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    name = f"pdf.{pdf_stem}"
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    sh = logging.StreamHandler(stream=__import__("sys").stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(_FMT)
    logger.addHandler(sh)

    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_FMT)
    logger.addHandler(fh)

    return logger

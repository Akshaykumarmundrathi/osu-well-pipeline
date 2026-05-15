import logging
import os


def get_logger(name: str, log_dir: str = "logs") -> logging.Logger:
    """
    Returns a named logger that writes to stdout at INFO level.
    One logger per module — call with __name__ at the top of each file.

    Usage:
        from utils.logging_utils import get_logger
        logger = get_logger(__name__)
        logger.info("Processing file: %s", pdf_path)
    """
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)

    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)

    return logger

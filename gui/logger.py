"""Logging setup: simple console + verbose rotating file."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent
_LOG_FILE = _LOG_DIR / "ncmdump.log"
_MAX_BYTES = 512 * 1024  # 512 KB per file
_BACKUP_COUNT = 2

_fmt_console = logging.Formatter("%(message)s")
_fmt_file = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)


def setup() -> logging.Logger:
    logger = logging.getLogger("ncmdump")
    logger.setLevel(logging.DEBUG)

    # Console handler – simple one-liners
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(_fmt_console)
        logger.addHandler(ch)

    # File handler – verbose, rotating
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        fh = RotatingFileHandler(
            str(_LOG_FILE), maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_fmt_file)
        logger.addHandler(fh)

    return logger


log = setup()

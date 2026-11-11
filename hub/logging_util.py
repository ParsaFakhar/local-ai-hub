"""Logging setup.

CRITICAL: in native-messaging mode ``stdout`` is the wire to Firefox.
Every log record goes to a rotating file and to ``stderr`` only.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from . import paths

_CONFIGURED = False
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def setup_logging(config) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("local_ai_hub")
    if _CONFIGURED:
        return logger

    level = _LEVELS.get(str(config.get("log.level", "info")).lower(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    logger.addHandler(stderr_handler)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            paths.log_dir() / str(config.get("log.file", "hub.log")),
            maxBytes=int(config.get("log.max_bytes", 1_048_576)),
            backupCount=int(config.get("log.backups", 3)),
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:  # read-only home, etc.
        pass

    _CONFIGURED = True
    return logger


def get_logger(name: str = "core") -> logging.Logger:
    return logging.getLogger(f"local_ai_hub.{name}")

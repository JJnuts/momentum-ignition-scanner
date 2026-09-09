"""Console + rotating-file logging."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"


def setup_logging(log_dir: Path, level: str = "INFO", filename: str = "scanner.log") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / filename

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Avoid duplicate handlers when called twice (tests, selftest).
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    fileh = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    # Quiet noisy libraries.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    return path

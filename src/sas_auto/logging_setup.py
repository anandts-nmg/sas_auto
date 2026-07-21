"""Consistent console and UTF-8 file logging."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path


def configure_logging(project_root: Path, command: str, verbose: bool = False) -> tuple[logging.Logger, Path]:
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"{stamp}_{command}.log"
    logger = logging.getLogger("sas_auto")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger, log_path

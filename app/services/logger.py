"""
Structured Logging System for Local Color Service V0.1.1
Handles console, file logging (data/logs/service.log), and system log tracking.
"""

import sys
import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Dict, Any, Optional

from app.config import settings

LOG_DIR = settings.DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "service.log"

# Memory log ring buffer for quick API queries
LOG_RING_BUFFER: List[Dict[str, Any]] = []
MAX_BUFFER_SIZE = 500


class RingBufferHandler(logging.Handler):
    """Stores logs in memory buffer for REST API log queries."""
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            entry = {
                "timestamp": record.asctime if hasattr(record, "asctime") else "",
                "level": record.levelname,
                "module": record.module,
                "message": record.getMessage(),
                "formatted": msg
            }
            LOG_RING_BUFFER.append(entry)
            if len(LOG_RING_BUFFER) > MAX_BUFFER_SIZE:
                LOG_RING_BUFFER.pop(0)
        except Exception:
            self.handleError(record)


def setup_logger(name: str = "local_color") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger  # Already initialized

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s:%(module)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Rotating File Handler (Max 10MB per file, max 5 backup files)
    file_handler = RotatingFileHandler(
        str(LOG_FILE),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Memory Ring Buffer Handler
    ring_handler = RingBufferHandler()
    ring_handler.setLevel(logging.INFO)
    ring_handler.setFormatter(formatter)
    logger.addHandler(ring_handler)

    return logger


logger = setup_logger("local_color")


def get_recent_logs(level: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieves recent logs from the in-memory ring buffer."""
    filtered = LOG_RING_BUFFER
    if level:
        level_upper = level.upper()
        filtered = [l for l in filtered if l["level"] == level_upper]
    return filtered[-limit:]

"""Application-only rotating logs and trace locations without exception payloads."""

from __future__ import annotations

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from thuauth.config import LoggingConfig
from thuauth.errors import ConfigError


def configure_logging(config: LoggingConfig, *, debug: bool = False) -> None:
    logger = logging.getLogger("thuauth")
    close_logging()
    logger.setLevel(logging.DEBUG if debug else config.level)
    logger.propagate = False
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if config.file:
        try:
            path = Path(config.file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(path, maxBytes=1048576, backupCount=3, encoding="utf-8")
            )
        except (OSError, ValueError, RuntimeError):
            for handler in handlers:
                handler.close()
            raise ConfigError("$.logging.file", "无法创建日志文件，请检查路径和权限") from None
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def close_logging() -> None:
    logger = logging.getLogger("thuauth")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()


def safe_traceback(error: BaseException) -> str:
    """Report exception types and code locations, excluding messages, source and locals."""
    parts = [type(error).__name__]
    for frame in traceback.extract_tb(error.__traceback__):
        parts.append(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}")
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            parts.append(safe_traceback(child))
    return " | ".join(parts)

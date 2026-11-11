"""Structured, secret-redacting logging utilities for console and file artifacts."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .security import redact_secrets

IST = ZoneInfo("Asia/Kolkata")


class SecretRedactionFilter(logging.Filter):
    """Logging filter that scrubs credentials from all formatted messages."""

    def __init__(self, extra_secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in extra_secrets if s and s.strip())

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        record.msg = redact_secrets(rendered, self._secrets)
        record.args = None
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text, self._secrets)
        return True


class ISTFormatter(logging.Formatter):
    """Formatter emitting timestamps in India Standard Time (Asia/Kolkata)."""

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        extra_secrets: Iterable[str] = (),
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._secrets = tuple(s for s in extra_secrets if s and s.strip())

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=IST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return redact_secrets(formatted, self._secrets)


def close_logger_handlers(logger: logging.Logger) -> None:
    """Close and detach all handlers attached to a logger."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.flush()
        finally:
            handler.close()


def configure_logger(
    log_dir: Path,
    *,
    run_date: date | None = None,
    level: int = logging.INFO,
    extra_secrets: Iterable[str] = (),
    name: str = "github_auto_push",
) -> tuple[logging.Logger, Path]:
    """Configure console + daily file logging with mandatory secret redaction."""
    effective_date = run_date or datetime.now(IST).date()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"heartbeat-{effective_date:%Y-%m-%d}.log"

    logger = logging.getLogger(name)
    close_logger_handlers(logger)
    logger.setLevel(level)
    logger.propagate = False

    formatter = ISTFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        extra_secrets=extra_secrets,
    )
    redaction_filter = SecretRedactionFilter(extra_secrets=extra_secrets)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redaction_filter)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction_filter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger, log_path

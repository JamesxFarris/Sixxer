"""Structured logging setup using structlog and rich.

Call ``setup_logging()`` once at application startup to configure every
logger in the process.  Afterwards, use ``get_logger(__name__)`` in each
module to obtain a bound structlog logger that writes human-readable
output to the terminal (via rich) and machine-readable JSON lines to a
rotating log file.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog
from rich.console import Console
from rich.logging import RichHandler

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_LOGGING_CONFIGURED: bool = False

# Default log directory relative to the project root.  The caller may
# override by setting the SIXXER_LOG_DIR environment variable.
_DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "data" / "logs"
_LOG_FILE_NAME = "sixxer.log"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5


def _ensure_log_dir(log_dir: Path) -> Path:
    """Create the log directory if it does not already exist."""
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structlog, stdlib logging, and all handlers.

    This function is idempotent -- calling it more than once is a safe
    no-op so that library code can defensively invoke it without worry.

    Parameters
    ----------
    log_level:
        Root log level as an uppercase string (``DEBUG``, ``INFO``, etc.).
    """
    global _LOGGING_CONFIGURED  # noqa: PLW0603
    if _LOGGING_CONFIGURED:
        return

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    log_dir = Path(os.environ.get("SIXXER_LOG_DIR", str(_DEFAULT_LOG_DIR)))
    _ensure_log_dir(log_dir)
    log_file = log_dir / _LOG_FILE_NAME

    # -- stdlib root logger --------------------------------------------------
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove any pre-existing handlers to avoid duplicate output when
    # setup_logging is called after basicConfig or similar.
    root_logger.handlers.clear()

    # 1) Rich console handler (human-friendly)
    console = Console(stderr=True, width=140)
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=True,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        markup=True,
        log_time_format="[%Y-%m-%d %H:%M:%S]",
    )
    rich_handler.setLevel(numeric_level)
    root_logger.addHandler(rich_handler)

    # 2) Rotating file handler (JSON lines)
    file_handler = RotatingFileHandler(
        filename=str(log_file),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    # The file formatter is intentionally minimal because structlog's
    # JSONRenderer produces the final string that stdlib will emit.
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(file_handler)

    # -- structlog configuration ---------------------------------------------
    # Shared pre-chain processors used by *both* structlog-native loggers and
    # stdlib loggers that are wrapped by structlog.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.CallsiteParameterAdder(
            parameters=[
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            ],
        ),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    # For the file handler we want JSON; for the console handler we let Rich
    # do the formatting.  We achieve this by routing structlog events through
    # stdlib and attaching the JSON renderer only to the file handler's
    # formatter.  The trick is to use ``ProcessorFormatter`` on the file
    # handler so that it renders JSON, while the Rich handler receives the
    # event dict and renders it natively.

    # Replace the plain formatter on the file handler with a structlog
    # ProcessorFormatter that emits JSON.
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=False)
            if sys.stderr.isatty()
            else structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        ),
    )

    # Replace the rich handler formatter as well so foreign (non-structlog)
    # log records still go through the shared processors.
    rich_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=True),
            foreign_pre_chain=shared_processors,
        ),
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    _LOGGING_CONFIGURED = True


def get_logger(name: str, **initial_binds: Any) -> structlog.stdlib.BoundLogger:
    """Return a *bound* structlog logger for *name*.

    Parameters
    ----------
    name:
        Typically ``__name__`` of the calling module.
    **initial_binds:
        Any key-value pairs to permanently bind to this logger instance
        (e.g. ``component="browser"``).
    """
    if not _LOGGING_CONFIGURED:
        setup_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if initial_binds:
        logger = logger.bind(**initial_binds)
    return logger

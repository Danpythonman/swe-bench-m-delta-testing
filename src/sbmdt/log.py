"""Logging setup helpers, including asyncio task-aware log formatting."""

import asyncio
import logging
import sys
from pathlib import Path

__all__ = [
    'setup_logging',
    'setup_logging_for_asyncio',
]


def setup_logging(log_file: Path | None = None, level: int = logging.DEBUG):
    """Configure the root logger with a console handler and optional file
    handler.

    Args:
        log_file: If provided, log records are also written to this file.
        level: Logging level applied to the logger and its handlers.
    """
    logger = logging.getLogger()  # root logger
    logger.setLevel(level)

    formatter = logging.Formatter(
        fmt='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (only if a path is provided)
    if log_file is not None:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


class TaskContextFilter(logging.Filter):
    """Log filter that annotates records with the current asyncio task name."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Set ``record.taskName`` to the current asyncio task's name, or
        'main'."""
        try:
            task = asyncio.current_task()
            record.taskName = task.get_name() if task else 'main'
        except RuntimeError:
            record.taskName = 'main'
        return True


def setup_logging_for_asyncio(log: logging.Logger) -> None:
    """Add asyncio task context (thread and task name) to a logger's format.

    Attaches a `TaskContextFilter` to `log` and rewrites each existing
    handler's format string to include thread name, thread id, and task name
    ahead of the log message.
    """
    log.addFilter(TaskContextFilter())
    for handler in log.handlers:
        formatter = handler.formatter
        if formatter is None:
            continue
        fmt = formatter._fmt
        if fmt is None:
            continue
        new_format = fmt.replace(
            '%(message)s',
            '[%(threadName)s:%(thread)d:%(taskName)s] %(message)s',
        )
        formatter._fmt = new_format
        formatter._style._fmt = new_format

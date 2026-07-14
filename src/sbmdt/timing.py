"""Decorators for timing function execution and logging the result."""

import functools
import logging
import time
from typing import Callable


__all__ = ['log_duration']


def log_duration[**P, R](
    logger: logging.Logger,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Build a decorator that logs how long the wrapped function took to run.

    Args:
        logger: Logger used to emit the elapsed-time message at INFO level.

    Returns:
        A decorator that wraps a function, timing each call and logging its
        duration (in seconds, to two decimal places) after it returns or
        raises.
    """
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                logger.info(f'{func.__name__} finished in {elapsed:.2f}s')
        return wrapper
    return decorator

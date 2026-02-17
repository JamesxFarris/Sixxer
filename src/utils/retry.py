"""Exponential-backoff retry decorator for both sync and async callables.

Usage::

    from src.utils.retry import retry

    @retry(max_attempts=5, base_delay=2.0, exceptions=(TimeoutError, ConnectionError))
    async def flaky_request():
        ...

    @retry()
    def also_works_sync():
        ...
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import random
import time
from typing import Any, Callable, ParamSpec, TypeVar

import structlog

P = ParamSpec("P")
T = TypeVar("T")

log = structlog.stdlib.get_logger(__name__)


def _compute_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """Return the back-off duration for the given *attempt* (0-indexed).

    Formula: ``min(base_delay * 2^attempt + jitter, max_delay)``
    where *jitter* is uniform in ``[0, base_delay]``.
    """
    exp = base_delay * (2 ** attempt)
    jitter = random.uniform(0, base_delay)
    return min(exp + jitter, max_delay)


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator factory that retries the wrapped function on failure.

    Parameters
    ----------
    max_attempts:
        Total number of attempts (including the first call).  Must be >= 1.
    base_delay:
        Initial delay in seconds before the first retry.
    max_delay:
        Upper cap on the computed delay.
    exceptions:
        Tuple of exception types that trigger a retry.  Any exception
        *not* in this tuple propagates immediately.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                last_exc: BaseException | None = None
                for attempt in range(max_attempts):
                    try:
                        return await func(*args, **kwargs)  # type: ignore[misc]
                    except exceptions as exc:
                        last_exc = exc
                        if attempt + 1 == max_attempts:
                            log.error(
                                "retry.exhausted",
                                func=func.__qualname__,
                                attempt=attempt + 1,
                                max_attempts=max_attempts,
                                error=str(exc),
                                error_type=type(exc).__name__,
                            )
                            raise
                        delay = _compute_delay(attempt, base_delay, max_delay)
                        log.warning(
                            "retry.attempt",
                            func=func.__qualname__,
                            attempt=attempt + 1,
                            max_attempts=max_attempts,
                            delay=round(delay, 2),
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                        await asyncio.sleep(delay)

                # Unreachable in practice, but keeps mypy happy.
                assert last_exc is not None  # noqa: S101
                raise last_exc

            return async_wrapper  # type: ignore[return-value]

        else:

            @functools.wraps(func)
            def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                last_exc: BaseException | None = None
                for attempt in range(max_attempts):
                    try:
                        return func(*args, **kwargs)
                    except exceptions as exc:
                        last_exc = exc
                        if attempt + 1 == max_attempts:
                            log.error(
                                "retry.exhausted",
                                func=func.__qualname__,
                                attempt=attempt + 1,
                                max_attempts=max_attempts,
                                error=str(exc),
                                error_type=type(exc).__name__,
                            )
                            raise
                        delay = _compute_delay(attempt, base_delay, max_delay)
                        log.warning(
                            "retry.attempt",
                            func=func.__qualname__,
                            attempt=attempt + 1,
                            max_attempts=max_attempts,
                            delay=round(delay, 2),
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                        time.sleep(delay)

                # Unreachable in practice, but keeps mypy happy.
                assert last_exc is not None  # noqa: S101
                raise last_exc

            return sync_wrapper  # type: ignore[return-value]

    return decorator

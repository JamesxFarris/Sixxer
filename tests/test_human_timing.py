"""Tests for the human-like timing functions in ``src.utils.human_timing``.

Each function is called 100 times to verify that results consistently fall
within the documented bounds.  Because the functions use Gaussian sampling
with hard clipping, every single sample must respect the [min, max] range.
"""

from __future__ import annotations

import pytest

from src.utils.human_timing import (
    between_actions,
    human_delay,
    page_load_wait,
    poll_interval,
    reading_delay,
    typing_delay,
)

ITERATIONS = 100


# =========================================================================
# human_delay
# =========================================================================


class TestHumanDelay:
    """human_delay(min_sec, max_sec) with default range [0.5, 2.0]."""

    def test_returns_float(self) -> None:
        assert isinstance(human_delay(), float)

    def test_within_default_bounds(self) -> None:
        for _ in range(ITERATIONS):
            val = human_delay()
            assert 0.5 <= val <= 2.0

    def test_within_custom_bounds(self) -> None:
        for _ in range(ITERATIONS):
            val = human_delay(min_sec=1.0, max_sec=5.0)
            assert 1.0 <= val <= 5.0

    def test_tight_range(self) -> None:
        """When min == max the function should return exactly that value."""
        for _ in range(ITERATIONS):
            val = human_delay(min_sec=3.0, max_sec=3.0)
            assert val == pytest.approx(3.0)

    def test_results_vary(self) -> None:
        """Over 100 calls, we should see at least two distinct values."""
        values = {round(human_delay(), 6) for _ in range(ITERATIONS)}
        assert len(values) > 1


# =========================================================================
# typing_delay
# =========================================================================


class TestTypingDelay:
    """typing_delay() with range [0.03, 0.20]."""

    def test_returns_float(self) -> None:
        assert isinstance(typing_delay(), float)

    def test_within_bounds(self) -> None:
        for _ in range(ITERATIONS):
            val = typing_delay()
            assert 0.03 <= val <= 0.20

    def test_results_vary(self) -> None:
        values = {round(typing_delay(), 6) for _ in range(ITERATIONS)}
        assert len(values) > 1


# =========================================================================
# reading_delay
# =========================================================================


class TestReadingDelay:
    """reading_delay(text_length) scales with text length and has min 0.5s."""

    def test_returns_float(self) -> None:
        assert isinstance(reading_delay(100), float)

    def test_minimum_bound(self) -> None:
        """Even for very short texts the delay should be >= 0.5."""
        for _ in range(ITERATIONS):
            val = reading_delay(1)
            assert val >= 0.5

    def test_scales_with_length(self) -> None:
        """A 5000-character text should have a higher average delay than 50."""
        short_avg = sum(reading_delay(50) for _ in range(ITERATIONS)) / ITERATIONS
        long_avg = sum(reading_delay(5000) for _ in range(ITERATIONS)) / ITERATIONS
        assert long_avg > short_avg

    def test_reasonable_upper_bound(self) -> None:
        """For a 500-character text (~100 words) the delay should be under 60s."""
        for _ in range(ITERATIONS):
            val = reading_delay(500)
            assert val < 60.0

    def test_zero_length(self) -> None:
        """Zero-length should still return >= 0.5."""
        for _ in range(ITERATIONS):
            val = reading_delay(0)
            assert val >= 0.5


# =========================================================================
# page_load_wait
# =========================================================================


class TestPageLoadWait:
    """page_load_wait() with range [1.0, 4.0]."""

    def test_returns_float(self) -> None:
        assert isinstance(page_load_wait(), float)

    def test_within_bounds(self) -> None:
        for _ in range(ITERATIONS):
            val = page_load_wait()
            assert 1.0 <= val <= 4.0

    def test_results_vary(self) -> None:
        values = {round(page_load_wait(), 6) for _ in range(ITERATIONS)}
        assert len(values) > 1


# =========================================================================
# between_actions
# =========================================================================


class TestBetweenActions:
    """between_actions() with range [0.5, 3.0]."""

    def test_returns_float(self) -> None:
        assert isinstance(between_actions(), float)

    def test_within_bounds(self) -> None:
        for _ in range(ITERATIONS):
            val = between_actions()
            assert 0.5 <= val <= 3.0


# =========================================================================
# poll_interval
# =========================================================================


class TestPollInterval:
    """poll_interval(min_minutes, max_minutes) returns seconds."""

    def test_returns_float(self) -> None:
        assert isinstance(poll_interval(), float)

    def test_default_bounds_in_seconds(self) -> None:
        """Default range is 3-5 minutes, i.e. [180, 300] seconds."""
        for _ in range(ITERATIONS):
            val = poll_interval()
            assert 180.0 <= val <= 300.0

    def test_custom_bounds_in_seconds(self) -> None:
        """poll_interval(1, 2) should return between 60 and 120 seconds."""
        for _ in range(ITERATIONS):
            val = poll_interval(min_minutes=1, max_minutes=2)
            assert 60.0 <= val <= 120.0

    def test_results_are_in_seconds_not_minutes(self) -> None:
        """Ensure the result is much larger than the minute input values."""
        val = poll_interval(min_minutes=3, max_minutes=5)
        assert val >= 60.0  # at least 1 minute worth of seconds

    def test_results_vary(self) -> None:
        values = {round(poll_interval(), 2) for _ in range(ITERATIONS)}
        assert len(values) > 1

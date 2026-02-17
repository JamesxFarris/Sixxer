"""Tests for the retry decorator (``src.utils.retry``)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.utils.retry import retry


# =========================================================================
# Sync retry
# =========================================================================


class TestSyncRetry:
    """Verify the retry decorator on synchronous functions."""

    @patch("src.utils.retry.time.sleep", return_value=None)
    def test_succeeds_first_attempt(self, mock_sleep: MagicMock) -> None:
        call_count = 0

        @retry(max_attempts=3)
        def succeed():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = succeed()
        assert result == "ok"
        assert call_count == 1
        mock_sleep.assert_not_called()

    @patch("src.utils.retry.time.sleep", return_value=None)
    def test_retries_then_succeeds(self, mock_sleep: MagicMock) -> None:
        call_count = 0

        @retry(max_attempts=3, base_delay=0.1)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("transient")
            return "recovered"

        result = flaky()
        assert result == "recovered"
        assert call_count == 3
        assert mock_sleep.call_count == 2  # slept before attempt 2 and 3

    @patch("src.utils.retry.time.sleep", return_value=None)
    def test_exhausts_retries(self, mock_sleep: MagicMock) -> None:
        @retry(max_attempts=2, base_delay=0.1)
        def always_fails():
            raise ValueError("permanent")

        with pytest.raises(ValueError, match="permanent"):
            always_fails()

        # 2 attempts, 1 sleep between them
        assert mock_sleep.call_count == 1

    @patch("src.utils.retry.time.sleep", return_value=None)
    def test_only_specified_exceptions_trigger_retry(
        self, mock_sleep: MagicMock
    ) -> None:
        """Exceptions not in the `exceptions` tuple should propagate immediately."""
        call_count = 0

        @retry(max_attempts=3, exceptions=(ConnectionError,))
        def type_error_raiser():
            nonlocal call_count
            call_count += 1
            raise TypeError("not retryable")

        with pytest.raises(TypeError, match="not retryable"):
            type_error_raiser()

        assert call_count == 1  # no retry
        mock_sleep.assert_not_called()

    @patch("src.utils.retry.time.sleep", return_value=None)
    def test_returns_correct_value(self, mock_sleep: MagicMock) -> None:
        @retry(max_attempts=2)
        def returns_value():
            return 42

        assert returns_value() == 42

    @patch("src.utils.retry.time.sleep", return_value=None)
    def test_preserves_function_name(self, mock_sleep: MagicMock) -> None:
        @retry()
        def my_function():
            return True

        assert my_function.__name__ == "my_function"

    def test_max_attempts_less_than_one_raises(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be >= 1"):
            @retry(max_attempts=0)
            def invalid():
                pass


# =========================================================================
# Async retry
# =========================================================================


class TestAsyncRetry:
    """Verify the retry decorator on async functions."""

    @patch("src.utils.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_async_succeeds_first_attempt(
        self, mock_sleep: AsyncMock
    ) -> None:
        call_count = 0

        @retry(max_attempts=3)
        async def succeed():
            nonlocal call_count
            call_count += 1
            return "async_ok"

        result = await succeed()
        assert result == "async_ok"
        assert call_count == 1
        mock_sleep.assert_not_called()

    @patch("src.utils.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_async_retries_then_succeeds(
        self, mock_sleep: AsyncMock
    ) -> None:
        call_count = 0

        @retry(max_attempts=4, base_delay=0.1)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("transient")
            return "recovered"

        result = await flaky()
        assert result == "recovered"
        assert call_count == 3
        assert mock_sleep.call_count == 2

    @patch("src.utils.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_async_exhausts_retries(self, mock_sleep: AsyncMock) -> None:
        @retry(max_attempts=2, base_delay=0.1)
        async def always_fails():
            raise TimeoutError("timed out")

        with pytest.raises(TimeoutError, match="timed out"):
            await always_fails()

        assert mock_sleep.call_count == 1

    @patch("src.utils.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_async_only_specified_exceptions_trigger_retry(
        self, mock_sleep: AsyncMock
    ) -> None:
        call_count = 0

        @retry(max_attempts=3, exceptions=(ConnectionError,))
        async def wrong_exception():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("not retryable")

        with pytest.raises(RuntimeError, match="not retryable"):
            await wrong_exception()

        assert call_count == 1
        mock_sleep.assert_not_called()

    @patch("src.utils.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_async_preserves_function_name(
        self, mock_sleep: AsyncMock
    ) -> None:
        @retry()
        async def my_async_function():
            return True

        assert my_async_function.__name__ == "my_async_function"

    @patch("src.utils.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_async_backoff_delay_increases(
        self, mock_sleep: AsyncMock
    ) -> None:
        """Verify that successive delays grow (exponential backoff)."""
        call_count = 0

        @retry(max_attempts=4, base_delay=1.0, max_delay=100.0)
        async def fails_three_times():
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                raise ConnectionError("retry me")
            return "done"

        result = await fails_three_times()
        assert result == "done"
        assert mock_sleep.call_count == 3

        # Extract the delay arguments from each sleep call
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        # Each delay should be non-negative
        for d in delays:
            assert d >= 0.0
        # The second delay should generally be larger than the first due to
        # exponential growth (base_delay * 2^attempt + jitter)
        # With base_delay=1.0: attempt 0 -> ~1+jitter, attempt 1 -> ~2+jitter
        # We check that at least the last delay is larger than the first
        assert delays[-1] > delays[0]

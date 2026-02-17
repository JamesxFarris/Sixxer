"""Anthropic Claude API client with cost tracking and budget enforcement.

Wraps the ``anthropic.AsyncAnthropic`` SDK to provide token accounting, daily
cost caps, and structured-JSON convenience methods.  Every API call is logged
to the ``api_costs`` database table for auditing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import anthropic

from src.models.database import Database
from src.models.schemas import ApiCost
from src.utils.logger import get_logger
from src.utils.retry import retry

log = get_logger(__name__, component="ai_client")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BudgetExceededError(Exception):
    """Raised when the daily API spend cap has been reached."""


# ---------------------------------------------------------------------------
# Pricing table (USD per million tokens)
# ---------------------------------------------------------------------------

_MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-haiku-3-5-20241022": {"input": 0.80, "output": 4.0},
}

# Fallback pricing used when the exact model slug is not in the table.
_DEFAULT_PRICING: dict[str, float] = {"input": 3.0, "output": 15.0}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class AIClient:
    """Async wrapper around the Anthropic Messages API.

    Parameters
    ----------
    api_key:
        Anthropic API key.
    model:
        Default model identifier for completions.
    db:
        Optional ``Database`` instance used to persist API cost records.
        When ``None``, cost tracking is performed in-memory only.
    daily_cap:
        Maximum allowed daily spend in USD.  Calls that would exceed the
        cap raise ``BudgetExceededError``.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-5-20250929",
        db: Database | None = None,
        daily_cap: float = 5.0,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model
        self._db = db
        self._daily_cap = daily_cap
        self._daily_cost_accumulator: float = 0.0
        log.info(
            "ai_client.init",
            model=model,
            daily_cap=daily_cap,
            db_enabled=db is not None,
        )

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def daily_cost(self) -> float:
        """Return the running in-memory total of today's API spend."""
        return self._daily_cost_accumulator

    # ------------------------------------------------------------------
    # Budget enforcement
    # ------------------------------------------------------------------

    async def get_daily_cost(self) -> float:
        """Query the database for today's total API cost.

        Falls back to the in-memory accumulator when no DB is available.
        """
        if self._db is None:
            return self._daily_cost_accumulator

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = await self._db.fetch_one(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total "
            "FROM api_costs WHERE timestamp LIKE ? || '%'",
            (today_str,),
        )
        return float(row["total"]) if row else 0.0

    async def check_budget(self) -> None:
        """Raise ``BudgetExceededError`` if the daily cap has been reached."""
        cost = await self.get_daily_cost()
        if cost >= self._daily_cap:
            log.warning(
                "ai_client.budget_exceeded",
                daily_cost=round(cost, 6),
                daily_cap=self._daily_cap,
            )
            raise BudgetExceededError(
                f"Daily API budget of ${self._daily_cap:.2f} exceeded "
                f"(current spend: ${cost:.4f})"
            )

    # ------------------------------------------------------------------
    # Cost calculation
    # ------------------------------------------------------------------

    def _calculate_cost(
        self, input_tokens: int, output_tokens: int, model: str
    ) -> float:
        """Return the estimated USD cost for a single API call."""
        pricing = _MODEL_PRICING.get(model, _DEFAULT_PRICING)
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        return input_cost + output_cost

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _log_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        cost: float,
        model: str,
        purpose: str,
    ) -> None:
        """Persist an API cost record to the database."""
        self._daily_cost_accumulator += cost

        record = ApiCost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            purpose=purpose,
        )

        log.info(
            "ai_client.api_call",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(cost, 6),
            purpose=purpose,
            daily_total=round(self._daily_cost_accumulator, 6),
        )

        if self._db is not None:
            try:
                await self._db.execute(
                    "INSERT INTO api_costs "
                    "(model, input_tokens, output_tokens, cost_usd, purpose, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record.model,
                        record.input_tokens,
                        record.output_tokens,
                        record.cost_usd,
                        record.purpose,
                        record.timestamp.isoformat(),
                    ),
                )
            except Exception:
                log.exception("ai_client.log_cost_failed")

    # ------------------------------------------------------------------
    # Completion methods
    # ------------------------------------------------------------------

    @retry(max_attempts=3, base_delay=2.0, max_delay=30.0, exceptions=(anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError))
    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        purpose: str = "completion",
    ) -> str:
        """Send a message to the Claude API and return the text response.

        Parameters
        ----------
        system:
            System prompt providing context and instructions.
        user:
            User message (the actual request).
        max_tokens:
            Maximum tokens in the response.
        temperature:
            Sampling temperature (0.0 for deterministic, up to 1.0).
        purpose:
            Human-readable label stored in the cost log.

        Returns
        -------
        str
            The assistant's text response.

        Raises
        ------
        BudgetExceededError
            If the daily spending cap has already been reached.
        """
        await self.check_budget()

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

        # Extract text from the response content blocks.
        text_parts: list[str] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
        result = "\n".join(text_parts)

        # Track cost.
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = self._calculate_cost(input_tokens, output_tokens, self._model)
        await self._log_cost(input_tokens, output_tokens, cost, self._model, purpose)

        return result

    async def complete_json(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        purpose: str = "json_completion",
    ) -> dict[str, Any]:
        """Send a message to Claude and parse the response as JSON.

        Uses ``temperature=0`` for deterministic structured output.  The
        response is stripped of optional markdown code fences before parsing.

        Returns
        -------
        dict
            The parsed JSON object.

        Raises
        ------
        json.JSONDecodeError
            If the model response is not valid JSON.
        BudgetExceededError
            If the daily spending cap has already been reached.
        """
        raw = await self.complete(
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=0.0,
            purpose=purpose,
        )

        # Strip optional markdown code fences that models sometimes emit.
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            # Remove opening fence (with optional language tag) and closing fence.
            lines = cleaned.split("\n")
            # Drop the first line (```json or ```) and the last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            cleaned = "\n".join(lines).strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            log.error(
                "ai_client.json_parse_failed",
                raw_response=raw[:500],
            )
            raise

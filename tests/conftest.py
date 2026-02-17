"""Shared pytest fixtures for the Sixxer test suite.

Provides reusable fixtures for the in-memory database, application settings,
and the deliverable file manager.  Every fixture is designed to run without
network access or external services.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so that ``import src.*`` resolves
# correctly regardless of how pytest is invoked.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.models.database import Database
from src.models.schemas import GigType, OrderStatus
from src.utils.file_handler import DeliverableManager


# ---------------------------------------------------------------------------
# Database fixture (in-memory, migrated, with a seed order)
# ---------------------------------------------------------------------------

@pytest.fixture
async def db() -> AsyncGenerator[Database, None]:
    """Yield a connected, migrated in-memory Database and close it after use."""
    database = Database(db_path=":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def db_with_order(db: Database) -> tuple[Database, str]:
    """Yield a Database that already contains a single test order.

    Returns a tuple of (db, order_id) so tests can reference the seed row.
    """
    order_id = "test-order-001"
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO orders "
        "(id, fiverr_order_id, gig_type, status, requirements, "
        " buyer_username, price, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            order_id,
            "FO-12345",
            GigType.WRITING.value,
            OrderStatus.NEW.value,
            json.dumps(["Write a 500-word blog post about Python"]),
            "testbuyer",
            25.0,
            now,
            now,
        ),
    )
    return db, order_id


# ---------------------------------------------------------------------------
# Settings fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch):
    """Return a Settings instance backed by test-only environment variables.

    Uses monkeypatch so the real environment is never modified.
    """
    monkeypatch.setenv("FIVERR_USERNAME", "test_user")
    monkeypatch.setenv("FIVERR_PASSWORD", "test_pass")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-not-real")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-3-5-20241022")
    monkeypatch.setenv("DAILY_COST_CAP_USD", "1.0")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DB_PATH", ":memory:")

    from config.settings import Settings

    return Settings()


# ---------------------------------------------------------------------------
# File-manager fixture (uses pytest tmp_path for isolation)
# ---------------------------------------------------------------------------

@pytest.fixture
def file_manager(tmp_path: Path) -> DeliverableManager:
    """Return a DeliverableManager rooted in a temporary directory."""
    return DeliverableManager(base_dir=str(tmp_path / "deliverables"))

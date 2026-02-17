"""Async SQLite database layer using aiosqlite.

Provides connection management, automatic schema migrations, and convenience
helpers for common query patterns.  The ``Database`` class is intended to be
used as a long-lived singleton and supports the async context-manager protocol.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from src.utils.logger import get_logger

log = get_logger(__name__, component="database")


class Database:
    """Thin async wrapper around an aiosqlite connection.

    Parameters
    ----------
    db_path:
        File-system path to the SQLite database.  Parent directories are
        created automatically if they do not exist.
    """

    def __init__(self, db_path: str = "data/sixxer.db") -> None:
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the database connection, enable WAL mode, and run migrations."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()
        log.info("database_connected", path=str(self._db_path))
        await self.migrate()

    async def close(self) -> None:
        """Close the database connection gracefully."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            log.info("database_closed")

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    async def migrate(self) -> None:
        """Create all application tables if they do not already exist."""
        assert self._conn is not None, "Database is not connected"

        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id               TEXT    PRIMARY KEY,
                fiverr_order_id  TEXT    UNIQUE NOT NULL,
                gig_type         TEXT    NOT NULL,
                status           TEXT    NOT NULL DEFAULT 'new',
                requirements     TEXT    NOT NULL DEFAULT '[]',
                buyer_username   TEXT    NOT NULL,
                price            REAL    NOT NULL,
                created_at       TEXT    NOT NULL,
                updated_at       TEXT    NOT NULL,
                delivered_at     TEXT,
                deliverable_paths TEXT   NOT NULL DEFAULT '[]',
                revision_count   INTEGER NOT NULL DEFAULT 0,
                notes            TEXT    NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id  TEXT    NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                direction TEXT    NOT NULL CHECK(direction IN ('sent', 'received')),
                content   TEXT    NOT NULL,
                timestamp TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_costs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                model         TEXT    NOT NULL,
                input_tokens  INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd      REAL    NOT NULL,
                purpose       TEXT    NOT NULL,
                timestamp     TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gigs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                fiverr_gig_id TEXT,
                gig_type      TEXT    NOT NULL,
                title         TEXT    NOT NULL,
                status        TEXT    NOT NULL DEFAULT 'active',
                created_at    TEXT    NOT NULL
            );
            """
        )
        await self._conn.commit()
        log.info("database_migrated")

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    @property
    def conn(self) -> aiosqlite.Connection:
        """Return the raw connection, raising if not connected."""
        if self._conn is None:
            raise RuntimeError("Database is not connected. Call connect() first.")
        return self._conn

    async def execute(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> aiosqlite.Cursor:
        """Execute a SQL statement and commit.

        Parameters
        ----------
        sql:
            SQL query string with ``?`` placeholders.
        params:
            Positional bind parameters.

        Returns
        -------
        aiosqlite.Cursor
            The cursor after execution (useful for ``lastrowid``, etc.).
        """
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor

    async def fetch_one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        """Fetch a single row as a dictionary.

        JSON-encoded columns (``requirements``, ``deliverable_paths``) are
        automatically deserialised.

        Returns ``None`` when the query matches no rows.
        """
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    async def fetch_all(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        """Fetch all matching rows, each returned as a dictionary."""
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """Convert an ``aiosqlite.Row`` to a plain ``dict``.

        Fields that are known to hold JSON payloads are automatically parsed.
        """
        data: dict[str, Any] = dict(row)
        for json_field in ("requirements", "deliverable_paths"):
            if json_field in data and isinstance(data[json_field], str):
                try:
                    data[json_field] = json.loads(data[json_field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return data

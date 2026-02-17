"""Lightweight HTTP health-check server for Railway.

Railway expects a listening HTTP port to confirm the service is alive.
This module runs an ``asyncio``-based HTTP server that exposes:

- ``GET /health``            -- JSON payload with uptime, cycle count, daily
  API cost, and order counts by status.
- ``GET /``                  -- Redirect to ``/health``.
- ``GET /debug/screenshot``  -- PNG screenshot of the current browser page.
- ``GET /debug/html``        -- Raw HTML of the current browser page.
- ``GET /debug/url``         -- JSON with the current page URL.

Usage (standalone)::

    python -m scripts.health_check

Normally started automatically by ``main.py`` as a background task.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.browser.engine import BrowserEngine
    from src.models.database import Database
    from src.orchestrator.scheduler import Scheduler

_start_time = time.monotonic()


class HealthCheckServer:
    """Minimal async HTTP server for Railway health checks.

    Parameters
    ----------
    port:
        TCP port to listen on.  Railway sets ``$PORT`` automatically.
    scheduler:
        Optional scheduler reference for live cycle-count reporting.
    db:
        Optional database reference for querying order stats and API cost.
    """

    def __init__(
        self,
        port: int = 8080,
        scheduler: Scheduler | None = None,
        db: Database | None = None,
        engine: BrowserEngine | None = None,
    ) -> None:
        self._port = port
        self._scheduler = scheduler
        self._db = db
        self._engine = engine
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        """Start listening on the configured port."""
        self._server = await asyncio.start_server(
            self._handle_connection, "0.0.0.0", self._port
        )
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        print(f"  Health check server listening on {addrs}")

    async def stop(self) -> None:
        """Shut down the server gracefully."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            request_text = request_line.decode("utf-8", errors="replace").strip()

            # Parse path from "GET /health HTTP/1.1"
            parts = request_text.split()
            path = parts[1] if len(parts) >= 2 else "/"

            # Drain remaining headers (we don't need them)
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            if path in ("/health", "/"):
                body = await self._build_health_payload()
                response = self._json_response(HTTPStatus.OK, body)
            elif path == "/debug/screenshot":
                response = await self._handle_debug_screenshot()
            elif path == "/debug/html":
                response = await self._handle_debug_html()
            elif path == "/debug/url":
                response = await self._handle_debug_url()
            else:
                response = self._json_response(
                    HTTPStatus.NOT_FOUND, {"error": "not found"}
                )

            writer.write(response)
            await writer.drain()
        except Exception:
            pass  # Don't crash on malformed requests
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Health payload
    # ------------------------------------------------------------------

    async def _build_health_payload(self) -> dict[str, Any]:
        uptime_seconds = time.monotonic() - _start_time
        hours, remainder = divmod(int(uptime_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)

        payload: dict[str, Any] = {
            "status": "ok",
            "uptime": f"{hours}h {minutes}m {seconds}s",
            "uptime_seconds": round(uptime_seconds, 1),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Scheduler metrics
        if self._scheduler is not None:
            payload["cycle_count"] = self._scheduler._cycle_count

        # Database metrics
        if self._db is not None:
            try:
                payload["orders"] = await self._get_order_counts()
                payload["daily_api_cost_usd"] = await self._get_daily_cost()
            except Exception:
                payload["orders"] = "unavailable"
                payload["daily_api_cost_usd"] = "unavailable"

        return payload

    async def _get_order_counts(self) -> dict[str, int]:
        assert self._db is not None
        rows = await self._db.fetch_all(
            "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status"
        )
        return {row["status"]: row["cnt"] for row in rows}

    async def _get_daily_cost(self) -> float:
        assert self._db is not None
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = await self._db.fetch_one(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total "
            "FROM api_costs WHERE timestamp LIKE ? || '%'",
            (today_str,),
        )
        return round(float(row["total"]), 4) if row else 0.0

    # ------------------------------------------------------------------
    # Debug endpoints
    # ------------------------------------------------------------------

    async def _handle_debug_screenshot(self) -> bytes:
        """Take a screenshot of the current browser page and return as PNG."""
        if self._engine is None:
            return self._json_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "browser engine not available"},
            )
        try:
            page = await self._engine.get_page()
            png_bytes = await page.screenshot(full_page=True)
            return self._binary_response(
                HTTPStatus.OK, png_bytes, "image/png"
            )
        except Exception as exc:
            return self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"screenshot failed: {exc}"},
            )

    async def _handle_debug_html(self) -> bytes:
        """Return the raw HTML of the current browser page."""
        if self._engine is None:
            return self._json_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "browser engine not available"},
            )
        try:
            page = await self._engine.get_page()
            html = await page.content()
            html_bytes = html.encode("utf-8")
            return self._binary_response(
                HTTPStatus.OK, html_bytes, "text/html; charset=utf-8"
            )
        except Exception as exc:
            return self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"html dump failed: {exc}"},
            )

    async def _handle_debug_url(self) -> bytes:
        """Return the current page URL as JSON."""
        if self._engine is None:
            return self._json_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "browser engine not available"},
            )
        try:
            page = await self._engine.get_page()
            return self._json_response(
                HTTPStatus.OK,
                {"url": page.url, "title": await page.title()},
            )
        except Exception as exc:
            return self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"url check failed: {exc}"},
            )

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _json_response(status: HTTPStatus, body: dict[str, Any]) -> bytes:
        body_bytes = json.dumps(body, indent=2).encode("utf-8")
        return (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8") + body_bytes

    @staticmethod
    def _binary_response(
        status: HTTPStatus, body: bytes, content_type: str
    ) -> bytes:
        return (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8") + body


async def _standalone_main() -> None:
    """Run the health check server standalone (for testing)."""
    port = int(os.environ.get("PORT", "8080"))
    server = HealthCheckServer(port=port)
    await server.start()
    print(f"  Running standalone on port {port}. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(_standalone_main())

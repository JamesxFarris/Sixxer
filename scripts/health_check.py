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
import random
import time
from datetime import datetime, timezone
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from src.browser.engine import BrowserEngine
    from src.browser.selectors import SelectorStore
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
        selectors: SelectorStore | None = None,
        debug_token: str | None = None,
    ) -> None:
        self._port = port
        self._scheduler = scheduler
        self._db = db
        self._engine = engine
        self._selectors = selectors
        self._debug_token = debug_token
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

            # Parse path and query string from "GET /health?token=abc HTTP/1.1"
            parts = request_text.split()
            raw_path = parts[1] if len(parts) >= 2 else "/"
            parsed = urlparse(raw_path)
            path = parsed.path
            params = parse_qs(parsed.query)

            # Drain remaining headers (we don't need them)
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            # Auth check for /debug/* endpoints
            if path.startswith("/debug/") and not self._check_debug_auth(params):
                response = self._json_response(
                    HTTPStatus.FORBIDDEN,
                    {"error": "invalid or missing token"},
                )
            elif path in ("/health", "/"):
                body = await self._build_health_payload()
                response = self._json_response(HTTPStatus.OK, body)
            elif path == "/debug/screenshot":
                response = await self._handle_debug_screenshot()
            elif path == "/debug/html":
                response = await self._handle_debug_html()
            elif path == "/debug/url":
                response = await self._handle_debug_url()
            elif path == "/debug/status":
                response = await self._handle_debug_status()
            elif path == "/debug/solve-px":
                response = await self._handle_debug_solve_px()
            elif path == "/debug/click":
                response = await self._handle_debug_click(params)
            elif path == "/debug/hold":
                response = await self._handle_debug_hold(params)
            elif path == "/debug/navigate":
                response = await self._handle_debug_navigate(params)
            elif path == "/debug/dom":
                response = await self._handle_debug_dom(params)
            elif path == "/debug/selectors-probe":
                response = await self._handle_debug_selectors_probe()
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
            payload["captcha_paused"] = getattr(
                self._scheduler, "_captcha_paused", False
            )

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
    # Auth check
    # ------------------------------------------------------------------

    def _check_debug_auth(self, params: dict[str, list[str]]) -> bool:
        """Return True if the request is authorized for debug endpoints."""
        if not self._debug_token:
            return True  # No token configured = open access
        token = params.get("token", [""])[0]
        return token == self._debug_token

    # ------------------------------------------------------------------
    # New debug endpoints
    # ------------------------------------------------------------------

    async def _handle_debug_status(self) -> bytes:
        """Return current URL, title, PerimeterX detection status, scheduler state."""
        if self._engine is None:
            return self._json_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "browser engine not available"},
            )
        try:
            page = await self._engine.get_page()
            url = page.url
            title = await page.title()

            # PerimeterX detection
            px_detected = False
            try:
                el = await page.wait_for_selector("#px-captcha", timeout=1_000)
                if el is not None:
                    px_detected = True
            except Exception:
                pass
            if not px_detected:
                try:
                    if title and "human" in title.lower():
                        px_detected = True
                except Exception:
                    pass

            payload: dict[str, Any] = {
                "url": url,
                "title": title,
                "perimeterx_detected": px_detected,
                "captcha_paused": getattr(
                    self._scheduler, "_captcha_paused", False
                ) if self._scheduler else None,
                "cycle_count": self._scheduler._cycle_count if self._scheduler else None,
            }
            return self._json_response(HTTPStatus.OK, payload)
        except Exception as exc:
            return self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"status check failed: {exc}"},
            )

    async def _handle_debug_solve_px(self) -> bytes:
        """Attempt automated PerimeterX solve: find #px-captcha, press-and-hold."""
        if self._engine is None:
            return self._json_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "browser engine not available"},
            )
        try:
            page = await self._engine.get_page()

            # Find #px-captcha element
            try:
                el = await page.wait_for_selector("#px-captcha", timeout=5_000)
            except Exception:
                el = None

            if el is None:
                return self._json_response(HTTPStatus.OK, {
                    "success": False,
                    "reason": "no #px-captcha element found on page",
                    "url": page.url,
                    "title": await page.title(),
                })

            # Get element bounding box for the press-and-hold
            box = await el.bounding_box()
            if box is None:
                return self._json_response(HTTPStatus.OK, {
                    "success": False,
                    "reason": "#px-captcha found but has no bounding box (hidden?)",
                })

            # Calculate center of the element with slight randomization
            cx = box["x"] + box["width"] / 2 + random.uniform(-5, 5)
            cy = box["y"] + box["height"] / 2 + random.uniform(-5, 5)

            # Move mouse to element naturally
            await page.mouse.move(cx, cy, steps=random.randint(15, 30))
            await asyncio.sleep(random.uniform(0.3, 0.8))

            # Press and hold for 8-12 seconds (PerimeterX requirement)
            hold_duration = random.uniform(8.0, 12.0)
            await page.mouse.down()
            await asyncio.sleep(hold_duration)
            await page.mouse.up()

            # Wait for page to potentially navigate
            await asyncio.sleep(3.0)

            # Check result
            new_url = page.url
            new_title = await page.title()
            still_blocked = False
            try:
                px_el = await page.wait_for_selector("#px-captcha", timeout=2_000)
                if px_el is not None:
                    still_blocked = True
            except Exception:
                pass

            return self._json_response(HTTPStatus.OK, {
                "success": not still_blocked,
                "hold_duration_seconds": round(hold_duration, 1),
                "click_position": {"x": round(cx), "y": round(cy)},
                "url_after": new_url,
                "title_after": new_title,
                "still_blocked": still_blocked,
            })

        except Exception as exc:
            return self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"solve-px failed: {exc}"},
            )

    async def _handle_debug_click(self, params: dict[str, list[str]]) -> bytes:
        """Click at given x,y coordinates."""
        if self._engine is None:
            return self._json_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "browser engine not available"},
            )
        try:
            x = float(params.get("x", ["0"])[0])
            y = float(params.get("y", ["0"])[0])
        except ValueError:
            return self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "x and y must be numbers"},
            )
        try:
            page = await self._engine.get_page()
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await asyncio.sleep(random.uniform(0.1, 0.3))
            await page.mouse.click(x, y)
            return self._json_response(HTTPStatus.OK, {
                "clicked": {"x": x, "y": y},
                "url": page.url,
            })
        except Exception as exc:
            return self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"click failed: {exc}"},
            )

    async def _handle_debug_hold(self, params: dict[str, list[str]]) -> bytes:
        """Press-and-hold at given x,y coordinates for a duration."""
        if self._engine is None:
            return self._json_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "browser engine not available"},
            )
        try:
            x = float(params.get("x", ["0"])[0])
            y = float(params.get("y", ["0"])[0])
            duration = float(params.get("duration", ["10"])[0])
            duration = min(duration, 30.0)  # Cap at 30 seconds
        except ValueError:
            return self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "x, y, and duration must be numbers"},
            )
        try:
            page = await self._engine.get_page()
            await page.mouse.move(x, y, steps=random.randint(10, 25))
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await page.mouse.down()
            await asyncio.sleep(duration)
            await page.mouse.up()
            await asyncio.sleep(1.0)
            return self._json_response(HTTPStatus.OK, {
                "held": {"x": x, "y": y, "duration": duration},
                "url_after": page.url,
                "title_after": await page.title(),
            })
        except Exception as exc:
            return self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"hold failed: {exc}"},
            )

    async def _handle_debug_navigate(self, params: dict[str, list[str]]) -> bytes:
        """Navigate the browser to a given URL."""
        if self._engine is None:
            return self._json_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "browser engine not available"},
            )
        url = params.get("url", [""])[0]
        if not url:
            return self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "url parameter is required"},
            )
        try:
            await self._engine.navigate(url)
            await asyncio.sleep(2.0)
            page = await self._engine.get_page()
            return self._json_response(HTTPStatus.OK, {
                "navigated_to": url,
                "current_url": page.url,
                "title": await page.title(),
            })
        except Exception as exc:
            return self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"navigation failed: {exc}"},
            )

    async def _handle_debug_dom(self, params: dict[str, list[str]]) -> bytes:
        """Query DOM elements by CSS selector."""
        if self._engine is None:
            return self._json_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "browser engine not available"},
            )
        selector = params.get("selector", [""])[0]
        if not selector:
            return self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "selector parameter is required"},
            )
        try:
            page = await self._engine.get_page()
            elements = await page.query_selector_all(selector)
            results = []
            for el in elements[:50]:  # Limit to 50 elements
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                text = (await el.text_content() or "").strip()[:200]
                attrs = await el.evaluate(
                    """el => {
                        const obj = {};
                        for (const attr of el.attributes) {
                            obj[attr.name] = attr.value.substring(0, 100);
                        }
                        return obj;
                    }"""
                )
                box = await el.bounding_box()
                results.append({
                    "tag": tag,
                    "text": text,
                    "attributes": attrs,
                    "visible": box is not None,
                    "bounding_box": box,
                })
            return self._json_response(HTTPStatus.OK, {
                "selector": selector,
                "count": len(elements),
                "elements": results,
            })
        except Exception as exc:
            return self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"DOM query failed: {exc}"},
            )

    async def _handle_debug_selectors_probe(self) -> bytes:
        """Test all configured selectors against the current page."""
        if self._engine is None or self._selectors is None:
            return self._json_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "browser engine or selectors not available"},
            )
        try:
            page = await self._engine.get_page()
            results: dict[str, dict[str, Any]] = {}

            for page_key, elements in self._selectors._data.items():
                results[page_key] = {}
                if not isinstance(elements, dict):
                    continue
                for element_key, entry in elements.items():
                    all_sels = self._selectors.get_all(page_key, element_key)
                    matched = []
                    for sel in all_sels:
                        try:
                            el = await page.wait_for_selector(sel, timeout=500)
                            if el is not None:
                                matched.append(sel)
                        except Exception:
                            pass
                    results[page_key][element_key] = {
                        "candidates": all_sels,
                        "matched": matched,
                        "found": len(matched) > 0,
                    }

            return self._json_response(HTTPStatus.OK, {
                "url": page.url,
                "title": await page.title(),
                "probe_results": results,
            })
        except Exception as exc:
            return self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"selectors probe failed: {exc}"},
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

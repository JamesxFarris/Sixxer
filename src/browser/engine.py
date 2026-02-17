"""Playwright-based browser engine with persistent context and stealth.

The ``BrowserEngine`` class is the single entry point for all browser
interaction in Sixxer.  It wraps Playwright's persistent Chromium context
with human-like timing, stealth patches, and convenience helpers for
common DOM operations.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Self

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright_stealth import stealth_async

from src.utils.human_timing import (
    between_actions,
    human_delay,
    page_load_wait,
    typing_delay,
)
from src.utils.logger import get_logger

log = get_logger(__name__, component="browser_engine")

_SCREENSHOTS_DIR = Path("data/screenshots")

# Realistic browser fingerprint constants
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_VIEWPORT = {"width": 1920, "height": 1080}
_LOCALE = "en-US"
_TIMEZONE = "America/New_York"


class BrowserEngine:
    """Persistent Chromium browser with stealth patches and human-like timing.

    Parameters
    ----------
    data_dir:
        Directory that stores the persistent browser profile (cookies,
        localStorage, etc.).  Re-using the same directory across runs
        preserves login sessions.
    headless:
        Whether to run the browser without a visible window.
    """

    def __init__(
        self,
        data_dir: str = "data/browser_data",
        headless: bool = False,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._headless = headless
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch the browser with a persistent profile and stealth patches."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        _SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._data_dir),
            headless=self._headless,
            viewport=_VIEWPORT,
            user_agent=_USER_AGENT,
            locale=_LOCALE,
            timezone_id=_TIMEZONE,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
            ],
            ignore_default_args=["--enable-automation"],
            java_script_enabled=True,
            accept_downloads=True,
        )

        # Grab the first page (Chromium opens one automatically) or create one.
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        # Apply stealth patches to every page created by this context.
        await stealth_async(self._page)
        self._context.on(
            "page",
            lambda p: asyncio.ensure_future(stealth_async(p)),
        )

        log.info(
            "browser_started",
            headless=self._headless,
            data_dir=str(self._data_dir),
        )

    async def stop(self) -> None:
        """Close the browser and release Playwright resources."""
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                log.warning("browser_context_close_error", exc_info=True)
            self._context = None
            self._page = None

        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

        log.info("browser_stopped")

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Page access
    # ------------------------------------------------------------------

    async def get_page(self) -> Page:
        """Return the current page, creating a new one if it was closed."""
        if self._context is None:
            raise RuntimeError("BrowserEngine has not been started.")

        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
            await stealth_async(self._page)
            log.debug("new_page_created")

        return self._page

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(
        self, url: str, wait_until: str = "domcontentloaded"
    ) -> None:
        """Navigate to *url* and wait, then pause like a human."""
        page = await self.get_page()
        log.info("navigating", url=url)
        await page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
        await asyncio.sleep(page_load_wait())

    # ------------------------------------------------------------------
    # Interaction helpers
    # ------------------------------------------------------------------

    async def click(self, selector: str) -> None:
        """Click an element with human-like delays.

        Waits briefly before and after the click to emulate natural rhythm.
        """
        page = await self.get_page()
        await asyncio.sleep(between_actions())

        element = await page.wait_for_selector(selector, timeout=10_000)
        if element is None:
            log.warning("click_element_not_found", selector=selector)
            return

        box = await element.bounding_box()
        if box is not None:
            # Move mouse to element vicinity first
            await page.mouse.move(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
                steps=5,
            )
            await asyncio.sleep(human_delay(0.05, 0.2))

        await element.click()
        await asyncio.sleep(between_actions())
        log.debug("clicked", selector=selector)

    async def type_text(
        self,
        selector: str,
        text: str,
        clear_first: bool = True,
    ) -> None:
        """Type *text* character-by-character with human-like timing.

        Parameters
        ----------
        selector:
            CSS selector for the target input / textarea.
        text:
            The string to type.
        clear_first:
            If ``True``, select-all and delete existing content before typing.
        """
        page = await self.get_page()
        await page.wait_for_selector(selector, timeout=10_000)

        if clear_first:
            await page.click(selector, click_count=3)  # triple-click = select all
            await asyncio.sleep(human_delay(0.1, 0.3))
            await page.keyboard.press("Backspace")
            await asyncio.sleep(human_delay(0.1, 0.3))

        await page.click(selector)
        for char in text:
            await page.keyboard.type(char)
            await asyncio.sleep(typing_delay())

        log.debug("typed_text", selector=selector, length=len(text))

    async def wait_for(self, selector: str, timeout: int = 30_000) -> bool:
        """Wait for *selector* to appear in the DOM.

        Returns ``True`` if the element appears before *timeout* (ms),
        ``False`` otherwise.
        """
        page = await self.get_page()
        try:
            await page.wait_for_selector(selector, timeout=timeout)
            return True
        except Exception:
            log.debug("wait_for_timeout", selector=selector, timeout_ms=timeout)
            return False

    async def get_text(self, selector: str) -> str | None:
        """Return the ``textContent`` of the first element matching *selector*.

        Returns ``None`` if the element is not found.
        """
        page = await self.get_page()
        try:
            element = await page.wait_for_selector(selector, timeout=5_000)
            if element is None:
                return None
            return await element.text_content()
        except Exception:
            return None

    async def get_texts(self, selector: str) -> list[str]:
        """Return ``textContent`` of **all** elements matching *selector*."""
        page = await self.get_page()
        elements = await page.query_selector_all(selector)
        texts: list[str] = []
        for el in elements:
            content = await el.text_content()
            if content is not None:
                texts.append(content.strip())
        return texts

    async def screenshot(self, name: str = "debug") -> Path:
        """Capture a full-page screenshot and return its path.

        Screenshots are saved under ``data/screenshots/`` with a
        UTC-timestamped filename for easy debugging.
        """
        page = await self.get_page()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = _SCREENSHOTS_DIR / f"{name}_{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info("screenshot_saved", path=str(path))
        return path

    async def upload_file(self, selector: str, file_path: str) -> None:
        """Upload a file through a ``<input type="file">`` element.

        If the input is hidden (common with styled upload buttons), the
        method uses Playwright's ``set_input_files`` which bypasses the
        need for a visible click-to-upload interaction.
        """
        page = await self.get_page()
        resolved = Path(file_path).resolve()
        if not resolved.is_file():
            log.error("upload_file_not_found", path=str(resolved))
            raise FileNotFoundError(f"Upload target not found: {resolved}")

        await page.set_input_files(selector, str(resolved))
        await asyncio.sleep(between_actions())
        log.info("file_uploaded", selector=selector, path=str(resolved))

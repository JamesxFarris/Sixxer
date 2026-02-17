"""Page navigation helpers for Fiverr.

The ``Navigator`` class centralises all URL routing and page-readiness
logic so that higher-level modules never hard-code Fiverr URLs or worry
about popup dismissal timing.
"""

from __future__ import annotations

import asyncio

from src.browser.engine import BrowserEngine
from src.browser.selectors import SelectorStore
from src.utils.human_timing import between_actions, human_delay, page_load_wait
from src.utils.logger import get_logger

log = get_logger(__name__, component="navigator")

_BASE = "https://www.fiverr.com"

_URLS = {
    "dashboard": f"{_BASE}/seller_dashboard",
    "inbox": f"{_BASE}/inbox",
    "orders": f"{_BASE}/manage_orders",
    "gig_create": f"{_BASE}/gigs/new",
    "my_gigs": f"{_BASE}/users/{{username}}/manage_gigs",
    "order_page": f"{_BASE}/manage_orders/{{order_id}}",
}


class Navigator:
    """Thin wrapper over ``BrowserEngine`` that knows Fiverr's URL map.

    Parameters
    ----------
    engine:
        An already-started ``BrowserEngine`` instance.
    selectors:
        The project-wide ``SelectorStore`` used for popup dismissal.
    """

    def __init__(self, engine: BrowserEngine, selectors: SelectorStore) -> None:
        self._engine = engine
        self._selectors = selectors

    # ------------------------------------------------------------------
    # Core navigation targets
    # ------------------------------------------------------------------

    async def goto_dashboard(self) -> None:
        """Navigate to the seller dashboard and wait for readiness."""
        log.info("navigating_to_dashboard")
        await self._engine.navigate(_URLS["dashboard"])
        await self.wait_for_page_ready()

    async def goto_inbox(self) -> None:
        """Navigate to the Fiverr inbox."""
        log.info("navigating_to_inbox")
        await self._engine.navigate(_URLS["inbox"])
        await self.wait_for_page_ready()

    async def goto_orders(self) -> None:
        """Navigate to the manage-orders page."""
        log.info("navigating_to_orders")
        await self._engine.navigate(_URLS["orders"])
        await self.wait_for_page_ready()

    async def goto_gig_creation(self) -> None:
        """Navigate to the gig creation page."""
        log.info("navigating_to_gig_creation")
        await self._engine.navigate(_URLS["gig_create"])
        await self.wait_for_page_ready()

    async def goto_order_page(self, order_id: str) -> None:
        """Navigate to a specific order's detail page.

        Parameters
        ----------
        order_id:
            The Fiverr order identifier (e.g. ``"FO12345678A1B2"``).
        """
        url = _URLS["order_page"].format(order_id=order_id)
        log.info("navigating_to_order", order_id=order_id, url=url)
        await self._engine.navigate(url)
        await self.wait_for_page_ready()

    # ------------------------------------------------------------------
    # Popup / banner dismissal
    # ------------------------------------------------------------------

    async def dismiss_popups(self) -> None:
        """Attempt to close popups, cookie banners, and notification modals.

        Each selector group is tried independently; errors are caught and
        silently logged so that missing popups never break the flow.
        """
        page = await self._engine.get_page()

        # Map of selector-store (page, element) pairs for things we want to close
        dismissal_targets: list[tuple[str, str]] = [
            ("common", "cookie_banner_close"),
            ("common", "popup_close"),
            ("common", "notification_dismiss"),
        ]

        for yaml_page, yaml_element in dismissal_targets:
            try:
                candidates = self._selectors.get_all(yaml_page, yaml_element)
            except KeyError:
                continue

            for selector in candidates:
                try:
                    element = await page.wait_for_selector(selector, timeout=1_500)
                    if element is not None:
                        await asyncio.sleep(human_delay(0.2, 0.6))
                        await element.click()
                        log.info(
                            "popup_dismissed",
                            target=f"{yaml_page}.{yaml_element}",
                            selector=selector,
                        )
                        await asyncio.sleep(human_delay(0.3, 0.8))
                        break  # One successful dismiss per target group is enough
                except Exception:
                    # Element not present or click failed -- perfectly fine.
                    continue

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------

    async def wait_for_page_ready(self) -> None:
        """Wait for the page to become interactive, then dismiss popups.

        The method waits for the ``load`` event (with a generous timeout),
        pauses briefly like a human would, and then sweeps for dismissible
        overlays.
        """
        page = await self._engine.get_page()

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            log.warning("page_load_timeout", url=page.url)

        # Additional settle time for JS frameworks to hydrate
        await asyncio.sleep(page_load_wait())

        # Try to get the page into a clean state
        await self.dismiss_popups()

        # Short human-like pause after everything settles
        await asyncio.sleep(between_actions())
        log.debug("page_ready", url=page.url)

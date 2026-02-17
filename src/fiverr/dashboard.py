"""Dashboard scraping for Fiverr seller metrics.

The ``DashboardScraper`` navigates to the seller dashboard and extracts
key performance indicators such as active-order count, total earnings,
response rate, and unread-message status.
"""

from __future__ import annotations

from src.browser.engine import BrowserEngine
from src.browser.selectors import SelectorStore
from src.fiverr.navigation import Navigator
from src.utils.logger import get_logger

log = get_logger(__name__, component="dashboard")


class DashboardScraper:
    """Extracts seller metrics from the Fiverr dashboard.

    Parameters
    ----------
    engine:
        An already-started ``BrowserEngine``.
    selectors:
        The project-wide ``SelectorStore``.
    navigator:
        A configured ``Navigator`` instance.
    """

    def __init__(
        self,
        engine: BrowserEngine,
        selectors: SelectorStore,
        navigator: Navigator,
    ) -> None:
        self._engine = engine
        self._selectors = selectors
        self._navigator = navigator

    # ------------------------------------------------------------------
    # Primary scrape
    # ------------------------------------------------------------------

    async def scrape(self) -> dict[str, str | int | bool]:
        """Navigate to the dashboard and return key seller metrics.

        Returns
        -------
        dict
            Keys: ``active_orders`` (int), ``earnings`` (str),
            ``response_rate`` (str), ``has_new_messages`` (bool).
            Values default to sensible fallbacks when extraction fails.
        """
        await self._navigator.goto_dashboard()

        metrics: dict[str, str | int | bool] = {
            "active_orders": 0,
            "earnings": "N/A",
            "response_rate": "N/A",
            "has_new_messages": False,
        }

        # -- Active orders --------------------------------------------------
        active_orders_text = await self._extract_with_fallback(
            "dashboard", "active_orders"
        )
        if active_orders_text is not None:
            cleaned = active_orders_text.strip()
            # Extract the numeric portion (e.g. "3 Active" -> 3)
            digits = "".join(ch for ch in cleaned if ch.isdigit())
            if digits:
                metrics["active_orders"] = int(digits)
            else:
                # If the entire text is not numeric, store the raw string
                log.debug("active_orders_non_numeric", raw=cleaned)

        # -- Earnings -------------------------------------------------------
        earnings_text = await self._extract_with_fallback(
            "dashboard", "earnings_total"
        )
        if earnings_text is not None:
            metrics["earnings"] = earnings_text.strip()

        # -- Response rate --------------------------------------------------
        response_text = await self._extract_with_fallback(
            "dashboard", "response_rate"
        )
        if response_text is not None:
            metrics["response_rate"] = response_text.strip()

        # -- New messages ---------------------------------------------------
        badge_text = await self._extract_with_fallback(
            "dashboard", "new_messages_badge"
        )
        if badge_text is not None:
            stripped = badge_text.strip()
            # Any non-empty badge text (including "0") that is not literally "0"
            # indicates new messages.
            metrics["has_new_messages"] = stripped != "" and stripped != "0"

        log.info("dashboard_scraped", metrics=metrics)
        return metrics

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def get_notifications(self) -> list[str]:
        """Return notification texts visible on the dashboard.

        Falls back to an empty list if no notification elements are found.
        """
        await self._navigator.goto_dashboard()

        page = await self._engine.get_page()
        notifications: list[str] = []

        # Try common notification selectors
        notification_selectors = [
            ".notification-text",
            "[data-testid='notification'] .text",
            ".dashboard-notification",
            ".alert-message",
            ".notification-item .content",
        ]

        for selector in notification_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    text = await el.text_content()
                    if text and text.strip():
                        notifications.append(text.strip())
                if notifications:
                    break
            except Exception:
                continue

        log.info("notifications_retrieved", count=len(notifications))
        return notifications

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _extract_with_fallback(
        self, yaml_page: str, yaml_element: str
    ) -> str | None:
        """Try every selector for *yaml_page*/*yaml_element* and return text.

        Returns the ``textContent`` from the first matching selector, or
        ``None`` if none matched.
        """
        page = await self._engine.get_page()

        try:
            candidates = self._selectors.get_all(yaml_page, yaml_element)
        except KeyError:
            log.warning(
                "selector_key_missing",
                page=yaml_page,
                element=yaml_element,
            )
            return None

        for selector in candidates:
            try:
                element = await page.wait_for_selector(selector, timeout=5_000)
                if element is not None:
                    text = await element.text_content()
                    if text is not None:
                        log.debug(
                            "element_extracted",
                            page=yaml_page,
                            element=yaml_element,
                            selector=selector,
                            text=text.strip()[:80],
                        )
                        return text
            except Exception:
                continue

        log.debug(
            "element_not_found",
            page=yaml_page,
            element=yaml_element,
        )
        return None

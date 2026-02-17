"""Order monitoring -- detecting new and changed orders on Fiverr.

The ``OrderMonitor`` polls the manage-orders page, scrapes the order list,
compares results against the local database, and returns only the orders
that are new or have changed status since the last check.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from src.browser.anti_detect import simulate_reading
from src.browser.engine import BrowserEngine
from src.browser.selectors import SelectorStore
from src.fiverr.navigation import Navigator
from src.models.database import Database
from src.models.schemas import GigType
from src.utils.human_timing import between_actions, human_delay, reading_delay
from src.utils.logger import get_logger

log = get_logger(__name__, component="order_monitor")

# Mapping of keywords in gig titles to GigType enum values.
_GIG_TYPE_KEYWORDS: dict[str, GigType] = {
    "write": GigType.WRITING,
    "article": GigType.WRITING,
    "blog": GigType.WRITING,
    "seo": GigType.WRITING,
    "content": GigType.WRITING,
    "python": GigType.CODING,
    "script": GigType.CODING,
    "automation": GigType.CODING,
    "code": GigType.CODING,
    "program": GigType.CODING,
    "data entry": GigType.DATA_ENTRY,
    "spreadsheet": GigType.DATA_ENTRY,
    "excel": GigType.DATA_ENTRY,
    "data": GigType.DATA_ENTRY,
}


def _infer_gig_type(title: str) -> str:
    """Infer the ``GigType`` value from an order/gig *title*.

    Scans the title for keywords and returns the first matching type.
    Defaults to ``writing`` if no keywords match.
    """
    lower = title.lower()
    for keyword, gig_type in _GIG_TYPE_KEYWORDS.items():
        if keyword in lower:
            return gig_type.value
    return GigType.WRITING.value


class OrderMonitor:
    """Detects new and changed orders by scraping the manage-orders page.

    Parameters
    ----------
    engine:
        An already-started ``BrowserEngine``.
    selectors:
        The project-wide ``SelectorStore``.
    navigator:
        A configured ``Navigator`` instance.
    db:
        A connected ``Database`` for order tracking.
    """

    def __init__(
        self,
        engine: BrowserEngine,
        selectors: SelectorStore,
        navigator: Navigator,
        db: Database,
    ) -> None:
        self._engine = engine
        self._selectors = selectors
        self._navigator = navigator
        self._db = db

    # ------------------------------------------------------------------
    # New / changed order detection
    # ------------------------------------------------------------------

    async def check_for_new_orders(self) -> list[dict[str, str]]:
        """Scrape the orders page and return new or status-changed orders.

        Returns
        -------
        list[dict]
            Each dict has keys: ``fiverr_order_id``, ``buyer_username``,
            ``status``, ``gig_type``, ``price``, ``requirements_url``.
        """
        await self._navigator.goto_orders()
        page = await self._engine.get_page()

        # Give the order list time to render
        await asyncio.sleep(between_actions())

        scraped_orders = await self._scrape_order_list(page)
        log.info("orders_scraped", count=len(scraped_orders))

        # Compare with database to find new / changed entries
        new_or_changed: list[dict[str, str]] = []

        for order in scraped_orders:
            fiverr_id = order.get("fiverr_order_id", "")
            if not fiverr_id:
                continue

            existing = await self._db.fetch_one(
                "SELECT fiverr_order_id, status FROM orders "
                "WHERE fiverr_order_id = ?",
                (fiverr_id,),
            )

            if existing is None:
                # Brand-new order -- insert into DB
                now = datetime.now(timezone.utc).isoformat()
                await self._db.execute(
                    "INSERT INTO orders "
                    "(id, fiverr_order_id, gig_type, status, buyer_username, "
                    " price, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'new', ?, ?, ?, ?)",
                    (
                        fiverr_id,
                        fiverr_id,
                        order.get("gig_type", GigType.WRITING.value),
                        order.get("buyer_username", "unknown"),
                        float(order.get("price", "0") or "0"),
                        now,
                        now,
                    ),
                )
                new_or_changed.append(order)
                log.info("new_order_detected", fiverr_order_id=fiverr_id)

            elif existing["status"] != order.get("status", ""):
                # Status changed -- update DB
                now = datetime.now(timezone.utc).isoformat()
                await self._db.execute(
                    "UPDATE orders SET status = ?, updated_at = ? "
                    "WHERE fiverr_order_id = ?",
                    (order.get("status", existing["status"]), now, fiverr_id),
                )
                new_or_changed.append(order)
                log.info(
                    "order_status_changed",
                    fiverr_order_id=fiverr_id,
                    old_status=existing["status"],
                    new_status=order.get("status"),
                )

        log.info("order_check_complete", new_or_changed=len(new_or_changed))
        return new_or_changed

    # ------------------------------------------------------------------
    # Order detail extraction
    # ------------------------------------------------------------------

    async def get_order_details(self, order_id: str) -> dict[str, object]:
        """Navigate to an order page and extract full details.

        Parameters
        ----------
        order_id:
            The Fiverr order identifier.

        Returns
        -------
        dict
            Keys: ``requirements`` (str), ``attached_files`` (list[str]),
            ``buyer_messages`` (list[str]), ``deadline`` (str),
            ``revision_requests`` (list[str]).
        """
        await self._navigator.goto_order_page(order_id)
        page = await self._engine.get_page()

        # Simulate reading the order page
        await simulate_reading(page, duration=reading_delay(800))

        details: dict[str, object] = {
            "requirements": "",
            "attached_files": [],
            "buyer_messages": [],
            "deadline": "",
            "revision_requests": [],
        }

        # -- Requirements ---------------------------------------------------
        req_selector = await self._selectors.find(
            page, "orders", "order_requirements"
        )
        if req_selector is not None:
            el = await page.query_selector(req_selector)
            if el is not None:
                text = await el.text_content()
                details["requirements"] = (text or "").strip()

        # -- Attached files -------------------------------------------------
        file_selectors = [
            ".attachment-name",
            ".file-name",
            "[data-testid='attachment']",
            "a[download]",
            ".order-attachments a",
        ]
        attached: list[str] = []
        for sel in file_selectors:
            elements = await page.query_selector_all(sel)
            for el in elements:
                text = await el.text_content()
                href = await el.get_attribute("href")
                name = (text or href or "").strip()
                if name and name not in attached:
                    attached.append(name)
            if attached:
                break
        details["attached_files"] = attached

        # -- Buyer messages -------------------------------------------------
        msg_selectors = [
            ".message-body",
            ".buyer-message",
            "[data-testid='buyer-message']",
            ".order-message .content",
            ".chat-message p",
        ]
        buyer_msgs: list[str] = []
        for sel in msg_selectors:
            elements = await page.query_selector_all(sel)
            for el in elements:
                text = await el.text_content()
                if text and text.strip():
                    buyer_msgs.append(text.strip())
            if buyer_msgs:
                break
        details["buyer_messages"] = buyer_msgs

        # -- Deadline -------------------------------------------------------
        deadline_selectors = [
            ".delivery-deadline",
            ".due-date",
            "[data-testid='deadline']",
            ".order-deadline",
            "time.deadline",
        ]
        for sel in deadline_selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=3_000)
                if el is not None:
                    text = await el.text_content()
                    dt_attr = await el.get_attribute("datetime")
                    details["deadline"] = (dt_attr or text or "").strip()
                    break
            except Exception:
                continue

        # -- Revision requests ----------------------------------------------
        revision_selectors = [
            ".revision-message",
            ".revision-request-text",
            "[data-testid='revision-message']",
            ".revision-details",
            ".buyer-revision-note",
        ]
        revisions: list[str] = []
        for sel in revision_selectors:
            elements = await page.query_selector_all(sel)
            for el in elements:
                text = await el.text_content()
                if text and text.strip():
                    revisions.append(text.strip())
            if revisions:
                break
        details["revision_requests"] = revisions

        log.info("order_details_extracted", order_id=order_id)
        return details

    # ------------------------------------------------------------------
    # Requirements shortcut
    # ------------------------------------------------------------------

    async def get_order_requirements(self, order_id: str) -> str:
        """Return the buyer-submitted requirements text for an order.

        Parameters
        ----------
        order_id:
            The Fiverr order identifier.

        Returns
        -------
        str
            The requirements text, or an empty string if not found.
        """
        details = await self.get_order_details(order_id)
        requirements = details.get("requirements", "")
        if isinstance(requirements, str):
            return requirements
        return str(requirements)

    # ------------------------------------------------------------------
    # Revision detection
    # ------------------------------------------------------------------

    async def detect_revision_request(
        self, order_id: str
    ) -> dict[str, str] | None:
        """Check whether a revision has been requested on *order_id*.

        Returns
        -------
        dict | None
            ``{"feedback": "<revision text>"}`` if a revision request is
            found, or ``None`` otherwise.
        """
        await self._navigator.goto_order_page(order_id)
        page = await self._engine.get_page()
        await asyncio.sleep(between_actions())

        # Look for revision indicator selectors from the YAML
        try:
            candidates = self._selectors.get_all("delivery", "revision_message")
        except KeyError:
            candidates = []

        # Extend with common fallback selectors
        candidates.extend([
            ".revision-request",
            ".revision-message",
            "[data-testid='revision-request']",
            ".buyer-revision-note",
            ".modification-request",
        ])

        for selector in candidates:
            try:
                el = await page.wait_for_selector(selector, timeout=3_000)
                if el is not None:
                    text = await el.text_content()
                    if text and text.strip():
                        feedback = text.strip()
                        log.info(
                            "revision_request_detected",
                            order_id=order_id,
                            feedback=feedback[:100],
                        )

                        # Update the order status in DB
                        now = datetime.now(timezone.utc).isoformat()
                        await self._db.execute(
                            "UPDATE orders SET status = 'revision_requested', "
                            "revision_count = revision_count + 1, "
                            "updated_at = ? WHERE fiverr_order_id = ?",
                            (now, order_id),
                        )
                        return {"feedback": feedback}
            except Exception:
                continue

        log.debug("no_revision_request", order_id=order_id)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _scrape_order_list(self, page: object) -> list[dict[str, str]]:
        """Extract individual order rows from the manage-orders page."""
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        orders: list[dict[str, str]] = []

        # Find order item elements
        item_selector = await self._selectors.find(
            pw_page, "orders", "order_item"
        )
        if item_selector is None:
            log.warning("order_items_not_found")
            return orders

        elements = await pw_page.query_selector_all(item_selector)

        for el in elements:
            try:
                order: dict[str, str] = {
                    "fiverr_order_id": "",
                    "buyer_username": "",
                    "status": "",
                    "gig_type": "",
                    "price": "",
                    "requirements_url": "",
                }

                # -- Order ID -----------------------------------------------
                id_candidates = self._selectors.get_all("orders", "order_id")
                for sel in id_candidates:
                    id_el = await el.query_selector(sel)
                    if id_el is not None:
                        # Try href first (often contains the order ID)
                        href = await id_el.get_attribute("href") or ""
                        if "/orders/" in href:
                            # Extract the order ID from URL path
                            parts = href.split("/orders/")
                            if len(parts) > 1:
                                raw_id = parts[1].split("/")[0].split("?")[0]
                                order["fiverr_order_id"] = raw_id
                                order["requirements_url"] = (
                                    f"https://www.fiverr.com/manage_orders/{raw_id}"
                                )

                        if not order["fiverr_order_id"]:
                            text = await id_el.text_content()
                            if text and text.strip():
                                # Strip non-alphanumeric prefix like "#"
                                cleaned = text.strip().lstrip("#").strip()
                                order["fiverr_order_id"] = cleaned

                        if order["fiverr_order_id"]:
                            break

                # -- Buyer username -----------------------------------------
                for uname_sel in (".buyer-name", ".username", "a.buyer",
                                  "[data-testid='buyer-name']", ".seller-buyer"):
                    uname_el = await el.query_selector(uname_sel)
                    if uname_el is not None:
                        text = await uname_el.text_content()
                        if text and text.strip():
                            order["buyer_username"] = text.strip()
                            break

                # -- Status -------------------------------------------------
                status_candidates = self._selectors.get_all(
                    "orders", "order_status"
                )
                for sel in status_candidates:
                    status_el = await el.query_selector(sel)
                    if status_el is not None:
                        text = await status_el.text_content()
                        if text and text.strip():
                            order["status"] = text.strip().lower()
                            break

                # -- Price --------------------------------------------------
                for price_sel in (".price", ".order-price", ".amount",
                                  "[data-testid='order-price']", "span.total"):
                    price_el = await el.query_selector(price_sel)
                    if price_el is not None:
                        text = await price_el.text_content()
                        if text:
                            # Strip currency symbols and whitespace
                            cleaned = text.strip().replace("$", "").replace(
                                ",", ""
                            ).strip()
                            order["price"] = cleaned
                            break

                # -- Gig type (inferred from title) -------------------------
                title_text = ""
                for title_sel in (".gig-title", ".order-title", "h3", "h4",
                                  "[data-testid='gig-title']", ".order-desc"):
                    title_el = await el.query_selector(title_sel)
                    if title_el is not None:
                        text = await title_el.text_content()
                        if text and text.strip():
                            title_text = text.strip()
                            break

                order["gig_type"] = _infer_gig_type(title_text)

                # -- Requirements URL fallback ------------------------------
                if not order["requirements_url"] and order["fiverr_order_id"]:
                    order["requirements_url"] = (
                        f"https://www.fiverr.com/manage_orders/"
                        f"{order['fiverr_order_id']}"
                    )

                if order["fiverr_order_id"]:
                    orders.append(order)
                else:
                    log.debug("order_row_missing_id", raw=str(order))

            except Exception:
                log.warning("order_row_parse_error", exc_info=True)
                continue

        return orders

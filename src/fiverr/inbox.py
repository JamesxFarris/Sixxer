"""Fiverr inbox operations -- reading, sending, and tracking messages.

The ``InboxManager`` handles conversation listing, message extraction,
and sending replies both from the inbox view and from within order pages.
All sent messages are recorded in the local database for audit.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from src.browser.anti_detect import simulate_reading
from src.browser.engine import BrowserEngine
from src.browser.selectors import SelectorStore
from src.fiverr.navigation import Navigator
from src.models.database import Database
from src.utils.human_timing import between_actions, human_delay, reading_delay
from src.utils.logger import get_logger

log = get_logger(__name__, component="inbox")


class InboxManager:
    """Read and send messages through the Fiverr inbox.

    Parameters
    ----------
    engine:
        An already-started ``BrowserEngine``.
    selectors:
        The project-wide ``SelectorStore``.
    navigator:
        A configured ``Navigator`` instance.
    db:
        A connected ``Database`` for message logging.
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
    # Conversation listing
    # ------------------------------------------------------------------

    async def get_conversations(self) -> list[dict[str, str | bool]]:
        """List all visible conversations in the inbox.

        Returns
        -------
        list[dict]
            Each dict contains: ``username``, ``last_message_preview``,
            ``unread`` (bool), ``conversation_url``.
        """
        await self._navigator.goto_inbox()
        page = await self._engine.get_page()

        conversations: list[dict[str, str | bool]] = []

        # Locate conversation items
        item_selector = await self._selectors.find(
            page, "inbox", "conversation_item"
        )
        if item_selector is None:
            log.warning("conversation_items_not_found")
            return conversations

        elements = await page.query_selector_all(item_selector)
        log.info("conversations_found", count=len(elements))

        for el in elements:
            try:
                conv: dict[str, str | bool] = {
                    "username": "",
                    "last_message_preview": "",
                    "unread": False,
                    "conversation_url": "",
                }

                # Extract username -- typically in a heading or strong tag
                for name_sel in ("h3", "strong", ".username", ".sender-name",
                                 "[data-testid='username']", "a.username"):
                    name_el = await el.query_selector(name_sel)
                    if name_el is not None:
                        text = await name_el.text_content()
                        if text and text.strip():
                            conv["username"] = text.strip()
                            break

                # Extract preview text
                for preview_sel in (".message-preview", ".last-message",
                                    "p", "span.preview", ".snippet"):
                    preview_el = await el.query_selector(preview_sel)
                    if preview_el is not None:
                        text = await preview_el.text_content()
                        if text and text.strip():
                            conv["last_message_preview"] = text.strip()
                            break

                # Detect unread state -- look for unread indicator classes
                class_attr = await el.get_attribute("class") or ""
                conv["unread"] = (
                    "unread" in class_attr.lower()
                    or "new" in class_attr.lower()
                )
                if not conv["unread"]:
                    badge = await el.query_selector(
                        ".unread-badge, .unread-indicator, .new-message-dot"
                    )
                    conv["unread"] = badge is not None

                # Extract conversation URL from the first anchor tag
                link_el = await el.query_selector("a[href]")
                if link_el is not None:
                    href = await link_el.get_attribute("href") or ""
                    if href:
                        if href.startswith("/"):
                            href = f"https://www.fiverr.com{href}"
                        conv["conversation_url"] = href

                conversations.append(conv)
            except Exception:
                log.warning("conversation_parse_error", exc_info=True)
                continue

        log.info("conversations_listed", total=len(conversations))
        return conversations

    # ------------------------------------------------------------------
    # Reading messages
    # ------------------------------------------------------------------

    async def read_conversation(self, username: str) -> list[dict[str, str]]:
        """Open a conversation by *username* and extract all messages.

        Parameters
        ----------
        username:
            The Fiverr username of the conversation partner.

        Returns
        -------
        list[dict]
            Each dict has keys: ``sender``, ``text``, ``timestamp``.
        """
        await self._navigator.goto_inbox()
        page = await self._engine.get_page()

        # Find and click the conversation for this user
        opened = await self._open_conversation_by_username(page, username)
        if not opened:
            log.warning("conversation_not_found", username=username)
            return []

        # Wait for messages to load
        await asyncio.sleep(page_load_pause())

        # Simulate reading the conversation like a human
        await simulate_reading(page, duration=reading_delay(500))

        # Extract messages
        messages: list[dict[str, str]] = []
        message_containers = await self._find_message_elements(page)

        for container in message_containers:
            try:
                msg: dict[str, str] = {
                    "sender": "",
                    "text": "",
                    "timestamp": "",
                }

                # Determine sender
                for sender_sel in (".sender", ".message-author", ".username",
                                   "[data-testid='message-sender']"):
                    sender_el = await container.query_selector(sender_sel)
                    if sender_el is not None:
                        text = await sender_el.text_content()
                        if text and text.strip():
                            msg["sender"] = text.strip()
                            break

                # If no explicit sender element, infer from class
                if not msg["sender"]:
                    container_class = await container.get_attribute("class") or ""
                    if "sent" in container_class.lower() or "self" in container_class.lower():
                        msg["sender"] = "me"
                    else:
                        msg["sender"] = username

                # Extract message text
                text_selector = await self._selectors.find(
                    page, "inbox", "message_text"
                )
                if text_selector is not None:
                    text_el = await container.query_selector(text_selector)
                else:
                    text_el = None

                if text_el is None:
                    for fallback in ("p", ".text", ".content", "span"):
                        text_el = await container.query_selector(fallback)
                        if text_el is not None:
                            break

                if text_el is not None:
                    text = await text_el.text_content()
                    if text:
                        msg["text"] = text.strip()

                # Extract timestamp
                for ts_sel in (".timestamp", "time", ".message-time",
                               "[data-testid='message-time']", ".date"):
                    ts_el = await container.query_selector(ts_sel)
                    if ts_el is not None:
                        ts_text = await ts_el.text_content()
                        ts_attr = await ts_el.get_attribute("datetime")
                        msg["timestamp"] = (ts_attr or ts_text or "").strip()
                        break

                if msg["text"]:
                    messages.append(msg)
            except Exception:
                log.warning("message_parse_error", exc_info=True)
                continue

        log.info(
            "conversation_read",
            username=username,
            message_count=len(messages),
        )
        return messages

    # ------------------------------------------------------------------
    # Sending messages
    # ------------------------------------------------------------------

    async def send_message(self, username: str, message: str) -> None:
        """Open a conversation and send a message.

        Parameters
        ----------
        username:
            The Fiverr username to message.
        message:
            The text content to send.
        """
        await self._navigator.goto_inbox()
        page = await self._engine.get_page()

        opened = await self._open_conversation_by_username(page, username)
        if not opened:
            log.error("send_message_conversation_not_found", username=username)
            raise RuntimeError(
                f"Could not open conversation with {username}"
            )

        await asyncio.sleep(between_actions())

        await self._type_and_send(page, message)

        # Log the message in the database
        await self._log_sent_message(username, message)
        log.info("message_sent_inbox", username=username, length=len(message))

    async def send_message_on_order_page(
        self, order_id: str, message: str
    ) -> None:
        """Send a message from within an order's detail page.

        Parameters
        ----------
        order_id:
            The Fiverr order identifier.
        message:
            The text content to send.
        """
        await self._navigator.goto_order_page(order_id)
        page = await self._engine.get_page()
        await asyncio.sleep(between_actions())

        await self._type_and_send(page, message)

        # Log the message in the database
        await self._log_sent_message(
            f"order:{order_id}", message, order_id=order_id
        )
        log.info(
            "message_sent_order_page",
            order_id=order_id,
            length=len(message),
        )

    # ------------------------------------------------------------------
    # Unread messages
    # ------------------------------------------------------------------

    async def get_unread_messages(self) -> list[dict[str, str | bool]]:
        """Retrieve all unread conversations and their latest messages.

        Returns
        -------
        list[dict]
            Each dict contains: ``username``, ``last_message_preview``,
            ``unread`` (bool), ``conversation_url``.  Only conversations
            flagged as unread are included.
        """
        all_convos = await self.get_conversations()
        unread = [c for c in all_convos if c.get("unread")]
        log.info("unread_conversations", count=len(unread))
        return unread

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _open_conversation_by_username(
        self, page: object, username: str
    ) -> bool:
        """Click on the conversation matching *username*.

        Returns ``True`` if the conversation was found and clicked.
        """
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        item_selector = await self._selectors.find(
            pw_page, "inbox", "conversation_item"
        )
        if item_selector is None:
            return False

        elements = await pw_page.query_selector_all(item_selector)
        for el in elements:
            text = await el.text_content()
            if text and username.lower() in text.lower():
                await asyncio.sleep(human_delay(0.3, 0.8))
                await el.click()
                await asyncio.sleep(page_load_pause())
                return True

        return False

    async def _find_message_elements(self, page: object) -> list:
        """Return all message container elements on the current page."""
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        message_selectors = [
            ".message-item",
            ".chat-message",
            "[data-testid='message']",
            ".message-row",
            ".message-bubble",
            ".msg-wrapper",
        ]

        for selector in message_selectors:
            elements = await pw_page.query_selector_all(selector)
            if elements:
                return elements

        return []

    async def _type_and_send(self, page: object, message: str) -> None:
        """Type a message into the input and press send."""
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        # Find the message input
        input_selector = await self._selectors.find(
            pw_page, "inbox", "message_input"
        )
        if input_selector is None:
            log.error("message_input_not_found")
            await self._engine.screenshot("no_message_input")
            raise RuntimeError("Message input field not found on page")

        await self._engine.type_text(input_selector, message)
        await asyncio.sleep(human_delay(0.5, 1.5))

        # Find and click the send button
        send_selector = await self._selectors.find(
            pw_page, "inbox", "send_button"
        )
        if send_selector is not None:
            await self._engine.click(send_selector)
        else:
            # Fallback: press Enter to send
            log.debug("send_button_not_found_using_enter")
            await pw_page.keyboard.press("Enter")

        await asyncio.sleep(between_actions())

    async def _log_sent_message(
        self,
        username: str,
        content: str,
        order_id: str | None = None,
    ) -> None:
        """Record a sent message in the database.

        If *order_id* is provided the message is linked to that order;
        otherwise a best-effort lookup is performed.
        """
        now = datetime.now(timezone.utc).isoformat()

        if order_id is None:
            # Try to find an order associated with this username
            row = await self._db.fetch_one(
                "SELECT id FROM orders WHERE buyer_username = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (username,),
            )
            if row is not None:
                order_id = row["id"]

        if order_id is not None:
            try:
                await self._db.execute(
                    "INSERT INTO messages (order_id, direction, content, timestamp) "
                    "VALUES (?, 'sent', ?, ?)",
                    (order_id, content, now),
                )
                log.debug("message_logged", order_id=order_id)
            except Exception:
                log.warning("message_log_failed", exc_info=True)
        else:
            log.debug(
                "message_not_logged_no_order",
                username=username,
            )


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def page_load_pause() -> float:
    """Return a human-like pause duration for waiting after page interactions."""
    return human_delay(1.5, 3.5)

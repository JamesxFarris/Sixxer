"""Order actions -- delivering work, requesting extensions, and handling revisions.

The ``OrderActions`` class encapsulates all write operations that change the
state of a Fiverr order: file delivery, deadline-extension requests, and
revision acceptance.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.browser.anti_detect import human_click
from src.browser.engine import BrowserEngine
from src.browser.selectors import SelectorStore
from src.fiverr.navigation import Navigator
from src.utils.human_timing import between_actions, human_delay
from src.utils.logger import get_logger

log = get_logger(__name__, component="order_actions")


class OrderActions:
    """Perform delivery and management actions on Fiverr orders.

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
    # Deliver order
    # ------------------------------------------------------------------

    async def deliver_order(
        self,
        order_id: str,
        message: str,
        file_paths: list[str],
    ) -> bool:
        """Deliver an order with a message and attached files.

        Parameters
        ----------
        order_id:
            The Fiverr order identifier.
        message:
            Delivery message to the buyer.
        file_paths:
            List of absolute or relative file paths to upload.

        Returns
        -------
        bool
            ``True`` if the delivery was submitted successfully.
        """
        # Validate that all files exist before starting
        resolved_paths: list[Path] = []
        for fp in file_paths:
            p = Path(fp).resolve()
            if not p.is_file():
                log.error("delivery_file_not_found", path=str(p))
                return False
            resolved_paths.append(p)

        await self._navigator.goto_order_page(order_id)
        page = await self._engine.get_page()

        # Click the "Deliver Now" / "Deliver Order" button to open the form
        deliver_trigger_found = await self._click_deliver_trigger(page)
        if not deliver_trigger_found:
            log.error("deliver_trigger_not_found", order_id=order_id)
            await self._engine.screenshot("deliver_trigger_missing")
            return False

        await asyncio.sleep(between_actions())

        # -- Type the delivery message --------------------------------------
        msg_selector = await self._selectors.find(
            page, "delivery", "delivery_message_input"
        )
        if msg_selector is None:
            log.error("delivery_message_input_not_found", order_id=order_id)
            await self._engine.screenshot("delivery_no_message_input")
            return False

        await self._engine.type_text(msg_selector, message)
        await asyncio.sleep(human_delay(0.5, 1.5))

        # -- Upload files ---------------------------------------------------
        for resolved in resolved_paths:
            uploaded = await self._upload_delivery_file(page, resolved)
            if not uploaded:
                log.error(
                    "delivery_file_upload_failed",
                    order_id=order_id,
                    file=str(resolved),
                )
                await self._engine.screenshot("delivery_upload_failed")
                return False
            # Brief pause between uploads
            await asyncio.sleep(human_delay(1.0, 2.5))

        # -- Submit the delivery --------------------------------------------
        submit_selector = await self._selectors.find(
            page, "delivery", "submit_delivery"
        )
        if submit_selector is None:
            log.error("delivery_submit_button_not_found", order_id=order_id)
            await self._engine.screenshot("delivery_no_submit")
            return False

        await asyncio.sleep(human_delay(0.5, 1.0))
        await human_click(page, submit_selector)
        await asyncio.sleep(between_actions())

        # Verify submission -- look for confirmation indicators
        success = await self._verify_delivery_submitted(page)
        if success:
            log.info(
                "order_delivered",
                order_id=order_id,
                files=len(resolved_paths),
            )
        else:
            log.warning(
                "delivery_verification_uncertain",
                order_id=order_id,
            )
            await self._engine.screenshot("delivery_verification_uncertain")

        return success

    # ------------------------------------------------------------------
    # Request extension
    # ------------------------------------------------------------------

    async def request_extension(
        self, order_id: str, days: int, reason: str
    ) -> bool:
        """Request a deadline extension on an order.

        Parameters
        ----------
        order_id:
            The Fiverr order identifier.
        days:
            Number of additional days requested.
        reason:
            Explanation for the extension.

        Returns
        -------
        bool
            ``True`` if the extension request was submitted.
        """
        await self._navigator.goto_order_page(order_id)
        page = await self._engine.get_page()
        await asyncio.sleep(between_actions())

        # Look for the extension / "Extend delivery" link or button
        extension_selectors = [
            "a:has-text('Extend')",
            "button:has-text('Extend')",
            "[data-testid='extend-delivery']",
            ".extend-delivery-btn",
            "a[href*='extend']",
        ]

        clicked = False
        for selector in extension_selectors:
            try:
                el = await page.wait_for_selector(selector, timeout=3_000)
                if el is not None:
                    await asyncio.sleep(human_delay(0.3, 0.8))
                    await el.click()
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            log.error("extension_trigger_not_found", order_id=order_id)
            await self._engine.screenshot("extension_trigger_missing")
            return False

        await asyncio.sleep(between_actions())

        # Fill in the number of days
        days_selectors = [
            "input[name='days']",
            "input[type='number']",
            "select.extension-days",
            "[data-testid='extension-days']",
            "input.days-input",
        ]

        days_filled = False
        for selector in days_selectors:
            try:
                el = await page.wait_for_selector(selector, timeout=3_000)
                if el is not None:
                    tag = await el.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        await el.select_option(value=str(days))
                    else:
                        await self._engine.type_text(selector, str(days))
                    days_filled = True
                    break
            except Exception:
                continue

        if not days_filled:
            log.error("extension_days_input_not_found", order_id=order_id)
            await self._engine.screenshot("extension_no_days_input")
            return False

        await asyncio.sleep(human_delay(0.5, 1.0))

        # Fill in the reason
        reason_selectors = [
            "textarea[name='reason']",
            "textarea.extension-reason",
            "[data-testid='extension-reason']",
            "textarea",
        ]

        reason_filled = False
        for selector in reason_selectors:
            try:
                el = await page.wait_for_selector(selector, timeout=3_000)
                if el is not None:
                    await self._engine.type_text(selector, reason)
                    reason_filled = True
                    break
            except Exception:
                continue

        if not reason_filled:
            log.warning("extension_reason_input_not_found", order_id=order_id)
            # Continue anyway -- reason may be optional on some layouts

        await asyncio.sleep(human_delay(0.5, 1.0))

        # Submit the extension request
        submit_selectors = [
            "button:has-text('Submit')",
            "button:has-text('Request')",
            "button[type='submit']",
            "[data-testid='submit-extension']",
            ".extension-submit-btn",
        ]

        for selector in submit_selectors:
            try:
                el = await page.wait_for_selector(selector, timeout=3_000)
                if el is not None:
                    await human_click(page, selector)
                    await asyncio.sleep(between_actions())
                    log.info(
                        "extension_requested",
                        order_id=order_id,
                        days=days,
                    )
                    return True
            except Exception:
                continue

        log.error("extension_submit_failed", order_id=order_id)
        await self._engine.screenshot("extension_submit_failed")
        return False

    # ------------------------------------------------------------------
    # Accept revision
    # ------------------------------------------------------------------

    async def accept_revision(self, order_id: str) -> bool:
        """Accept a revision request on an order.

        Parameters
        ----------
        order_id:
            The Fiverr order identifier.

        Returns
        -------
        bool
            ``True`` if the revision was accepted successfully.
        """
        await self._navigator.goto_order_page(order_id)
        page = await self._engine.get_page()
        await asyncio.sleep(between_actions())

        # Look for an "Accept" or "Start Revision" button
        accept_selectors = [
            "button:has-text('Accept')",
            "button:has-text('Start Revision')",
            "[data-testid='accept-revision']",
            ".accept-revision-btn",
            "a:has-text('Accept Revision')",
            "button:has-text('OK')",
        ]

        for selector in accept_selectors:
            try:
                el = await page.wait_for_selector(selector, timeout=3_000)
                if el is not None:
                    await asyncio.sleep(human_delay(0.3, 0.8))
                    await human_click(page, selector)
                    await asyncio.sleep(between_actions())

                    # Handle confirmation dialogs
                    await self._confirm_dialog(page)

                    log.info("revision_accepted", order_id=order_id)
                    return True
            except Exception:
                continue

        log.error("accept_revision_button_not_found", order_id=order_id)
        await self._engine.screenshot("accept_revision_not_found")
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _click_deliver_trigger(self, page: object) -> bool:
        """Find and click the button that opens the delivery form.

        Returns ``True`` if a deliver button was found and clicked.
        """
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        trigger_selectors = [
            "button:has-text('Deliver Now')",
            "button:has-text('Deliver Order')",
            "button:has-text('Deliver')",
            "[data-testid='deliver-order']",
            ".deliver-now-btn",
            "a:has-text('Deliver Now')",
        ]

        for selector in trigger_selectors:
            try:
                el = await pw_page.wait_for_selector(selector, timeout=4_000)
                if el is not None:
                    await asyncio.sleep(human_delay(0.3, 0.8))
                    await human_click(pw_page, selector)
                    await asyncio.sleep(between_actions())
                    return True
            except Exception:
                continue

        return False

    async def _upload_delivery_file(
        self, page: object, file_path: Path
    ) -> bool:
        """Upload a single file through the delivery form.

        Returns ``True`` on success.
        """
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        # Try YAML-configured selector first, then fallbacks
        upload_selector = await self._selectors.find(
            pw_page, "delivery", "file_upload"
        )

        if upload_selector is not None:
            try:
                await self._engine.upload_file(upload_selector, str(file_path))
                log.debug("file_uploaded", file=file_path.name)
                return True
            except Exception:
                log.debug(
                    "yaml_upload_selector_failed",
                    selector=upload_selector,
                )

        # Fallback: look for any file input on the page
        fallback_selectors = [
            "input[type='file']",
            ".upload-zone input[type='file']",
            ".file-upload input",
        ]

        for selector in fallback_selectors:
            try:
                el = await pw_page.wait_for_selector(selector, timeout=3_000)
                if el is not None:
                    await self._engine.upload_file(selector, str(file_path))
                    log.debug(
                        "file_uploaded_fallback",
                        file=file_path.name,
                        selector=selector,
                    )
                    return True
            except Exception:
                continue

        log.error("file_upload_selector_not_found", file=file_path.name)
        return False

    async def _verify_delivery_submitted(self, page: object) -> bool:
        """Check for confirmation indicators after submitting a delivery.

        Returns ``True`` if a success signal is detected.
        """
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        success_indicators = [
            ".delivery-success",
            "[data-testid='delivery-success']",
            ":has-text('delivered successfully')",
            ":has-text('Order Delivered')",
            ".success-message",
            ".order-delivered-banner",
        ]

        for selector in success_indicators:
            try:
                el = await pw_page.wait_for_selector(selector, timeout=8_000)
                if el is not None:
                    return True
            except Exception:
                continue

        # Fallback: check if the page URL changed to indicate delivery
        current_url = pw_page.url
        if "delivered" in current_url.lower() or "complete" in current_url.lower():
            return True

        # Final fallback: check if the delivery button is gone
        # (which implies the form was submitted)
        try:
            deliver_btn = await pw_page.wait_for_selector(
                "button:has-text('Deliver Now')", timeout=2_000
            )
            if deliver_btn is None:
                return True
        except Exception:
            # Button not found -> likely submitted
            return True

        return False

    async def _confirm_dialog(self, page: object) -> None:
        """Handle any confirmation modals that appear after an action."""
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        confirm_selectors = [
            "button:has-text('Confirm')",
            "button:has-text('Yes')",
            "button:has-text('OK')",
            "[data-testid='confirm-button']",
            ".modal-confirm-btn",
        ]

        for selector in confirm_selectors:
            try:
                el = await pw_page.wait_for_selector(selector, timeout=2_000)
                if el is not None:
                    await asyncio.sleep(human_delay(0.3, 0.8))
                    await el.click()
                    await asyncio.sleep(between_actions())
                    log.debug("confirmation_dialog_accepted", selector=selector)
                    return
            except Exception:
                continue

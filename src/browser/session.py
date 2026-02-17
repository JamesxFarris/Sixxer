"""Login and session persistence for Fiverr.

The ``SessionManager`` coordinates login flows, session health checks,
and security-challenge handling.  It delegates all browser interaction
to a ``BrowserEngine`` instance and uses the ``SelectorStore`` for
resilient element lookups.
"""

from __future__ import annotations

import asyncio

from src.browser.anti_detect import human_click, random_scroll, simulate_reading
from src.browser.engine import BrowserEngine
from src.browser.selectors import SelectorStore
from src.utils.human_timing import between_actions, human_delay, page_load_wait
from src.utils.logger import get_logger

log = get_logger(__name__, component="session")

_FIVERR_LOGIN_URL = "https://www.fiverr.com/login"
_FIVERR_DASHBOARD_URL = "https://www.fiverr.com/seller_dashboard"
_FIVERR_HOME_URL = "https://www.fiverr.com"


class SessionManager:
    """Manages Fiverr login state using a persistent browser profile.

    Parameters
    ----------
    engine:
        An already-started ``BrowserEngine`` instance.
    username:
        Fiverr account username (or e-mail).
    password:
        Fiverr account password.
    """

    def __init__(
        self,
        engine: BrowserEngine,
        username: str,
        password: str,
    ) -> None:
        self._engine = engine
        self._username = username
        self._password = password
        self._selectors = SelectorStore()

    # ------------------------------------------------------------------
    # Session health
    # ------------------------------------------------------------------

    async def is_logged_in(self) -> bool:
        """Return ``True`` if the browser appears to have an active session.

        Navigates to the seller dashboard and looks for recognisable
        dashboard elements.  If none are found within a short timeout the
        user is considered logged out.
        """
        page = await self._engine.get_page()

        # If we are not already on the dashboard, navigate there.
        current_url = page.url
        if "seller_dashboard" not in current_url and "fiverr.com" not in current_url:
            await self._engine.navigate(_FIVERR_DASHBOARD_URL)

        # Try multiple dashboard indicators
        dashboard_selectors = self._selectors.get_all("dashboard", "active_orders")
        for selector in dashboard_selectors:
            try:
                el = await page.wait_for_selector(selector, timeout=5_000)
                if el is not None:
                    log.info("session_active")
                    return True
            except Exception:
                continue

        # Fallback: check if the URL itself indicates we landed on a logged-in page
        current_url = page.url
        if "seller_dashboard" in current_url or "manage_orders" in current_url:
            log.info("session_active_by_url")
            return True

        # Check for username display as another indicator
        username_selectors = self._selectors.get_all("dashboard", "username_display") if "dashboard" in self._selectors._data and "username_display" in self._selectors._data.get("dashboard", {}) else []
        for selector in username_selectors:
            try:
                el = await page.wait_for_selector(selector, timeout=3_000)
                if el is not None:
                    log.info("session_active_by_username")
                    return True
            except Exception:
                continue

        log.info("session_not_active")
        return False

    async def ensure_session(self) -> None:
        """Guarantee that the browser has an active Fiverr session.

        If the session has expired or was never established, a full
        login flow is executed.  Raises ``RuntimeError`` if login fails.
        """
        if await self.is_logged_in():
            return

        log.info("session_expired_relogging")
        success = await self.login()
        if not success:
            raise RuntimeError("Failed to establish a Fiverr session.")

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------

    async def login(self) -> bool:
        """Perform the complete Fiverr login flow.

        Returns ``True`` on success, ``False`` on failure (including
        unresolvable security challenges).
        """
        page = await self._engine.get_page()

        # ------ 1. Navigate to login page -----------------------------------
        await self._engine.navigate(_FIVERR_LOGIN_URL)
        await asyncio.sleep(page_load_wait())

        # ------ 2. Dismiss cookie / consent banners -------------------------
        await self._dismiss_cookie_banner()

        # ------ 3. Check for security challenges ----------------------------
        if await self._detect_security_check():
            await self.handle_security_check()
            # After human intervention, re-check login state
            if await self.is_logged_in():
                return True
            # If still not logged in, the user didn't complete the challenge
            log.warning("security_check_not_resolved")
            return False

        # ------ 4. Fill username -------------------------------------------
        username_sel = await self._selectors.find(
            page, "login", "username_input"
        )
        if username_sel is None:
            log.error("login_username_field_not_found")
            await self._engine.screenshot("login_no_username_field")
            return False

        await asyncio.sleep(between_actions())
        await self._engine.type_text(username_sel, self._username)
        log.info("login_username_entered")

        # ------ 5. Fill password -------------------------------------------
        password_sel = await self._selectors.find(
            page, "login", "password_input"
        )
        if password_sel is None:
            log.error("login_password_field_not_found")
            await self._engine.screenshot("login_no_password_field")
            return False

        await asyncio.sleep(between_actions())
        await self._engine.type_text(password_sel, self._password)
        log.info("login_password_entered")

        # ------ 6. Submit the form -----------------------------------------
        submit_sel = await self._selectors.find(
            page, "login", "submit_button"
        )
        if submit_sel is None:
            log.error("login_submit_button_not_found")
            await self._engine.screenshot("login_no_submit")
            return False

        await asyncio.sleep(human_delay(0.3, 0.8))
        await human_click(page, submit_sel)
        log.info("login_form_submitted")

        # ------ 7. Wait for navigation / dashboard -------------------------
        await asyncio.sleep(page_load_wait())

        # Check for post-login security challenge
        if await self._detect_security_check():
            await self.handle_security_check()
            if await self.is_logged_in():
                return True
            return False

        # Check for login errors
        error_selectors = self._selectors.get_all("login", "error_message") if "login" in self._selectors._data and "error_message" in self._selectors._data.get("login", {}) else []
        for err_sel in error_selectors:
            try:
                el = await page.wait_for_selector(err_sel, timeout=2_000)
                if el is not None:
                    error_text = await el.text_content()
                    log.error("login_error_displayed", error=error_text)
                    await self._engine.screenshot("login_error")
                    return False
            except Exception:
                continue

        # Verify we actually reached the dashboard
        logged_in = await self.is_logged_in()
        if logged_in:
            log.info("login_successful")
        else:
            log.warning("login_verification_failed")
            await self._engine.screenshot("login_verification_failed")

        return logged_in

    # ------------------------------------------------------------------
    # Security challenge handling
    # ------------------------------------------------------------------

    async def handle_security_check(self) -> None:
        """Pause execution and prompt the operator to solve a challenge.

        Some challenges (CAPTCHA, 2FA, e-mail verification) cannot be
        solved programmatically.  This method takes a debug screenshot,
        logs a prominent warning, and blocks until the operator signals
        that the challenge has been resolved.

        In an attended environment (e.g. ``manual_login.py``) the
        operator sees the visible browser and solves the challenge
        manually; then the calling code can proceed.
        """
        await self._engine.screenshot("security_challenge")
        log.warning(
            "security_check_detected",
            message=(
                "A security challenge (CAPTCHA, 2FA, or verification) was "
                "detected.  Please solve it in the browser window."
            ),
        )

        page = await self._engine.get_page()

        # Poll every 5 seconds for up to 5 minutes, waiting for the
        # challenge to disappear (i.e. the page navigates away).
        max_wait_seconds = 300
        poll_interval = 5.0
        elapsed = 0.0

        while elapsed < max_wait_seconds:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            if not await self._detect_security_check():
                log.info("security_check_resolved", elapsed_secs=round(elapsed))
                return

        log.error("security_check_timeout")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _detect_security_check(self) -> bool:
        """Return ``True`` if a CAPTCHA / challenge iframe is present."""
        page = await self._engine.get_page()
        captcha_selectors = self._selectors.get_all("login", "captcha_indicator")
        for selector in captcha_selectors:
            try:
                el = await page.wait_for_selector(selector, timeout=2_000)
                if el is not None:
                    log.debug("security_check_indicator_found", selector=selector)
                    return True
            except Exception:
                continue
        return False

    async def _dismiss_cookie_banner(self) -> None:
        """Attempt to close a cookie-consent banner if present."""
        page = await self._engine.get_page()
        cookie_selectors = self._selectors.get_all("common", "cookie_banner_close")
        for selector in cookie_selectors:
            try:
                el = await page.wait_for_selector(selector, timeout=3_000)
                if el is not None:
                    await asyncio.sleep(human_delay(0.3, 1.0))
                    await el.click()
                    log.info("cookie_banner_dismissed", selector=selector)
                    await asyncio.sleep(human_delay(0.5, 1.5))
                    return
            except Exception:
                continue

        log.debug("no_cookie_banner_found")

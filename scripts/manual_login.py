"""Interactive first-time login helper.

Launch this script to open a visible browser window pointing at Fiverr.
Log in manually (including any 2FA / CAPTCHA challenges), then press
Enter in the terminal.  The script verifies the session and exits.

The persistent browser profile ensures the session survives across
subsequent automated runs.

Usage::

    python -m scripts.manual_login
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure the project root is on ``sys.path`` so that ``src.*`` imports work
# when the script is executed directly.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import settings  # noqa: E402
from src.browser.engine import BrowserEngine  # noqa: E402
from src.browser.session import SessionManager  # noqa: E402
from src.utils.logger import get_logger, setup_logging  # noqa: E402

log = get_logger(__name__, component="manual_login")


async def main() -> None:
    setup_logging(settings.log_level)

    print("=" * 60)
    print("  Sixxer - Manual Login Helper")
    print("=" * 60)
    print()
    print(f"  Browser data dir : {settings.abs_browser_data_dir}")
    print(f"  Target site      : https://www.fiverr.com")
    print()

    engine = BrowserEngine(
        data_dir=str(settings.abs_browser_data_dir),
        headless=False,
    )

    try:
        await engine.start()
        await engine.navigate("https://www.fiverr.com")

        session = SessionManager(
            engine=engine,
            username=settings.fiverr_username,
            password=settings.fiverr_password,
        )

        # Check if already logged in from a previous session
        already_logged_in = await session.is_logged_in()
        if already_logged_in:
            print()
            print("  [OK] You are already logged in from a previous session!")
            print("  Session data is persisted. You can close this window.")
            print()
        else:
            print("  The browser is now open. Please do the following:")
            print()
            print("    1. Navigate to https://www.fiverr.com/login if not already there")
            print("    2. Log in with your Fiverr credentials")
            print("    3. Complete any CAPTCHA / 2FA challenges")
            print("    4. Wait until you see your Fiverr dashboard")
            print()
            print("  When you are fully logged in, come back here and press ENTER.")
            print()

            # Block until the operator presses Enter
            await asyncio.get_event_loop().run_in_executor(None, input, "  Press ENTER to verify login... ")

            # Verify the session
            logged_in = await session.is_logged_in()
            if logged_in:
                print()
                print("  [OK] Login verified successfully!")
                print("  Your session has been saved to the persistent browser profile.")
                print("  Future automated runs will reuse this session.")
                print()
            else:
                print()
                print("  [FAIL] Login could not be verified.")
                print("  Please make sure you are on the Fiverr seller dashboard")
                print("  and try running this script again.")
                print()
                # Give the user another chance
                await asyncio.get_event_loop().run_in_executor(
                    None, input, "  Press ENTER to retry verification (or Ctrl+C to quit)... "
                )
                if await session.is_logged_in():
                    print()
                    print("  [OK] Login verified on retry!")
                    print()
                else:
                    print()
                    print("  [FAIL] Still unable to verify login. Exiting.")
                    print()

    finally:
        await engine.stop()
        print("  Browser closed.")


if __name__ == "__main__":
    asyncio.run(main())

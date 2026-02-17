"""Quick dashboard status check.

Scrapes the Fiverr seller dashboard and prints key metrics plus the
current gig listing.

Usage::

    python -m scripts.check_status
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
from src.browser.selectors import SelectorStore  # noqa: E402
from src.browser.session import SessionManager  # noqa: E402
from src.fiverr.dashboard import DashboardScraper  # noqa: E402
from src.fiverr.gig_manager import GigManager  # noqa: E402
from src.fiverr.navigation import Navigator  # noqa: E402
from src.models.database import Database  # noqa: E402
from src.utils.logger import get_logger, setup_logging  # noqa: E402

log = get_logger(__name__, component="check_status")


async def main() -> None:
    setup_logging(settings.log_level)

    print("=" * 60)
    print("  Sixxer - Dashboard Status Check")
    print("=" * 60)
    print()

    engine = BrowserEngine(
        data_dir=str(settings.abs_browser_data_dir),
        headless=False,
    )
    selectors = SelectorStore()
    db = Database(db_path=str(settings.abs_db_path))

    try:
        await engine.start()
        await db.connect()

        # Ensure we have an active session
        session = SessionManager(
            engine=engine,
            username=settings.fiverr_username,
            password=settings.fiverr_password,
        )
        await session.ensure_session()
        print("  [OK] Logged in to Fiverr")
        print()

        navigator = Navigator(engine, selectors)

        # -- Dashboard metrics ----------------------------------------------
        print("  Scraping dashboard metrics...")
        dashboard = DashboardScraper(engine, selectors, navigator)
        metrics = await dashboard.scrape()

        print()
        print("  Dashboard Metrics")
        print("  " + "-" * 40)
        print(f"    Active Orders  : {metrics.get('active_orders', 'N/A')}")
        print(f"    Earnings       : {metrics.get('earnings', 'N/A')}")
        print(f"    Response Rate  : {metrics.get('response_rate', 'N/A')}")

        has_messages = metrics.get("has_new_messages", False)
        msg_indicator = "Yes" if has_messages else "No"
        print(f"    New Messages   : {msg_indicator}")
        print()

        # -- Notifications --------------------------------------------------
        print("  Checking notifications...")
        notifications = await dashboard.get_notifications()
        if notifications:
            print()
            print("  Notifications:")
            for note in notifications:
                print(f"    - {note}")
            print()
        else:
            print("  No notifications found.")
            print()

        # -- Gig listing ----------------------------------------------------
        print("  Fetching gig list...")
        gig_manager = GigManager(engine, selectors, navigator, db)
        gigs = await gig_manager.get_my_gigs()

        if gigs:
            print()
            print(f"  Your Gigs ({len(gigs)}):")
            print("  " + "-" * 40)
            for gig in gigs:
                title = gig.get("title", "Untitled")
                status = gig.get("status", "unknown")
                url = gig.get("url", "")
                impressions = gig.get("impressions", "0")
                clicks = gig.get("clicks", "0")
                orders = gig.get("orders", "0")

                print(f"    {title}")
                print(f"      Status: {status}  |  URL: {url}")
                print(
                    f"      Impressions: {impressions}  "
                    f"Clicks: {clicks}  "
                    f"Orders: {orders}"
                )
                print()
        else:
            print("  No gigs found on your account.")
            print()

    except Exception as exc:
        print(f"  [ERROR] {exc}")
        log.error("check_status_failed", exc_info=True)

    finally:
        await db.close()
        await engine.stop()
        print("  Browser closed.")


if __name__ == "__main__":
    asyncio.run(main())

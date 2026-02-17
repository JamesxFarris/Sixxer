"""Create all Fiverr gigs from the YAML template configuration.

Usage::

    python -m scripts.setup_gigs
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
from src.fiverr.gig_manager import GigManager  # noqa: E402
from src.fiverr.navigation import Navigator  # noqa: E402
from src.models.database import Database  # noqa: E402
from src.utils.logger import get_logger, setup_logging  # noqa: E402

log = get_logger(__name__, component="setup_gigs")


async def main() -> None:
    setup_logging(settings.log_level)

    print("=" * 60)
    print("  Sixxer - Gig Setup")
    print("=" * 60)
    print()

    templates_path = str(_PROJECT_ROOT / "config" / "gig_templates.yaml")
    print(f"  Templates file : {templates_path}")
    print(f"  Browser data   : {settings.abs_browser_data_dir}")
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
        gig_manager = GigManager(engine, selectors, navigator, db)

        # Create all gigs
        print("  Creating gigs from templates...")
        print()

        created_ids = await gig_manager.create_all_gigs(templates_path)

        # Print results
        print()
        print("-" * 60)
        print(f"  Gigs created: {len(created_ids)}")
        print()

        if created_ids:
            for gig_id in created_ids:
                url = f"https://www.fiverr.com/manage_gigs/{gig_id}"
                print(f"    - Gig ID: {gig_id}")
                print(f"      URL:    {url}")
                print()
        else:
            print("  No gigs were created. Check the logs for errors.")
            print()

        # List all gigs for verification
        print("  Fetching current gig list for verification...")
        gigs = await gig_manager.get_my_gigs()
        if gigs:
            print()
            print("  Current gigs on account:")
            for gig in gigs:
                print(f"    - {gig['title']}")
                print(f"      Status: {gig['status']}  |  URL: {gig['url']}")
                print(
                    f"      Impressions: {gig['impressions']}  "
                    f"Clicks: {gig['clicks']}  "
                    f"Orders: {gig['orders']}"
                )
                print()
        else:
            print("  No gigs found on account.")
            print()

    except Exception as exc:
        print(f"  [ERROR] {exc}")
        log.error("setup_gigs_failed", exc_info=True)

    finally:
        await db.close()
        await engine.stop()
        print("  Browser closed.")


if __name__ == "__main__":
    asyncio.run(main())

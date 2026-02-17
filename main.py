"""Sixxer -- Autonomous Fiverr Freelancing System.

Entry point that wires together all components and starts the scheduler.

Usage::

    python main.py              # Run the full autonomous loop
    python main.py --headless   # Run with browser in headless mode
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

# Ensure project root is on sys.path so that ``src.*`` imports resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import Settings
from src.ai.analyzer import OrderAnalyzer
from src.ai.client import AIClient
from src.ai.communicator import BuyerCommunicator
from src.ai.prompts import PromptManager
from src.browser.engine import BrowserEngine
from src.browser.selectors import SelectorStore
from src.browser.session import SessionManager
from src.fiverr.inbox import InboxManager
from src.fiverr.navigation import Navigator
from src.fiverr.order_actions import OrderActions
from src.fiverr.order_monitor import OrderMonitor
from src.models.database import Database
from src.models.schemas import GigType
from src.orchestrator.dispatcher import Dispatcher
from src.orchestrator.scheduler import Scheduler
from src.orchestrator.state_machine import OrderStateMachine
from src.utils.file_handler import DeliverableManager
from src.utils.logger import get_logger, setup_logging
from scripts.health_check import HealthCheckServer
from src.workers.coding_worker import CodingWorker
from src.workers.data_entry_worker import DataEntryWorker
from src.workers.revision_worker import RevisionWorker
from src.workers.writing_worker import WritingWorker

log = get_logger(__name__, component="main")


async def main(headless: bool = False) -> None:
    """Bootstrap all components and run the autonomous loop."""

    # ---- Settings --------------------------------------------------------
    settings = Settings()
    setup_logging(settings.log_level)
    log.info("sixxer.starting", headless=headless)

    # ---- Data directory verification -------------------------------------
    data_dir = Path(settings.db_path).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        "sixxer.data_dir_check",
        data_dir=str(data_dir.resolve()),
        exists=data_dir.exists(),
        is_mount=Path("/app/data").is_mount() if Path("/app/data").exists() else False,
        contents=str(list(data_dir.iterdir())) if data_dir.exists() else "N/A",
    )

    # ---- Database --------------------------------------------------------
    db = Database(str(settings.abs_db_path))
    await db.connect()

    # ---- Browser ---------------------------------------------------------
    engine = BrowserEngine(
        data_dir=str(settings.abs_browser_data_dir),
        headless=headless,
    )
    await engine.start()

    selectors = SelectorStore(str(_PROJECT_ROOT / "config" / "selectors.yaml"))
    session = SessionManager(engine, settings.fiverr_username, settings.fiverr_password)

    # NOTE: We do NOT call ensure_session() here. The scheduler's first cycle
    # handles it, and if PerimeterX blocks, the scheduler enters pause-and-retry
    # mode while the health server stays reachable for /debug/solve-px.

    # ---- Health check server (start EARLY so Railway sees /health) --------
    health_server = HealthCheckServer(
        port=settings.port,
        db=db,
        engine=engine,
        selectors=selectors,
        debug_token=settings.debug_token or None,
    )
    await health_server.start()
    log.info("sixxer.health_check_started", port=settings.port)

    # ---- Navigation & Fiverr ops -----------------------------------------
    navigator = Navigator(engine, selectors)
    inbox = InboxManager(engine, selectors, navigator, db)
    order_monitor = OrderMonitor(engine, selectors, navigator, db)
    order_actions = OrderActions(engine, selectors, navigator)

    # ---- AI layer --------------------------------------------------------
    ai_client = AIClient(
        api_key=settings.anthropic_api_key,
        model=settings.claude_model,
        db=db,
        daily_cap=settings.daily_cost_cap_usd,
    )
    prompts = PromptManager(str(_PROJECT_ROOT / "config" / "prompts.yaml"))
    analyzer = OrderAnalyzer(ai_client, prompts)
    communicator = BuyerCommunicator(ai_client, prompts)

    # ---- Workers ---------------------------------------------------------
    file_manager = DeliverableManager(str(settings.abs_deliverables_dir))

    writing_worker = WritingWorker(ai_client, prompts, file_manager)
    coding_worker = CodingWorker(ai_client, prompts, file_manager)
    data_entry_worker = DataEntryWorker(ai_client, prompts, file_manager)

    workers = {
        GigType.WRITING: writing_worker,
        GigType.CODING: coding_worker,
        GigType.DATA_ENTRY: data_entry_worker,
    }

    revision_worker = RevisionWorker(ai_client, prompts, file_manager, workers)

    # ---- Orchestration ---------------------------------------------------
    state_machine = OrderStateMachine(db)
    dispatcher = Dispatcher(
        state_machine=state_machine,
        analyzer=analyzer,
        communicator=communicator,
        workers=workers,
        revision_worker=revision_worker,
        db=db,
    )

    scheduler = Scheduler(
        session=session,
        order_monitor=order_monitor,
        order_actions=order_actions,
        inbox=inbox,
        dispatcher=dispatcher,
        state_machine=state_machine,
        db=db,
        poll_min=settings.poll_interval_min,
        poll_max=settings.poll_interval_max,
    )

    # Wire scheduler into health server (it was started early, before scheduler existed)
    health_server._scheduler = scheduler

    # ---- Signal handling -------------------------------------------------
    def handle_shutdown(sig: int, frame: object) -> None:
        log.info("sixxer.shutdown_signal", signal=sig)
        scheduler.stop()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # ---- Run -------------------------------------------------------------
    try:
        log.info("sixxer.autonomous_loop_starting")
        await scheduler.run()
    finally:
        log.info("sixxer.shutting_down")
        await health_server.stop()
        await engine.stop()
        await db.close()
        log.info("sixxer.shutdown_complete")


def cli() -> None:
    """Parse CLI arguments and run the event loop."""
    parser = argparse.ArgumentParser(
        description="Sixxer - Autonomous Fiverr Freelancing System",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser in headless mode (no visible window)",
    )
    args = parser.parse_args()

    print("\n  Sixxer - Autonomous Fiverr Freelancing System")
    print("  =============================================\n")
    print("  Starting autonomous loop...")
    print("  Press Ctrl+C to stop.\n")

    asyncio.run(main(headless=args.headless))


if __name__ == "__main__":
    cli()

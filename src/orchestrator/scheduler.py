"""Main async event loop scheduler.

The scheduler is the heartbeat of the Sixxer system.  It runs an infinite
polling loop that:

1. Ensures the browser session is alive.
2. Checks for new / changed orders.
3. Dispatches new orders through the processing pipeline.
4. Handles revision requests on delivered orders.
5. Delivers completed work.
6. Sleeps for a randomised interval before repeating.

All heavy lifting is delegated to the ``Dispatcher``, ``OrderMonitor``,
``OrderActions``, and ``InboxManager``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from src.ai.client import BudgetExceededError
from src.browser.session import SessionManager
from src.fiverr.inbox import InboxManager
from src.fiverr.order_actions import OrderActions
from src.fiverr.order_monitor import OrderMonitor
from src.models.database import Database
from src.models.schemas import OrderStatus
from src.orchestrator.dispatcher import Dispatcher
from src.orchestrator.state_machine import OrderStateMachine
from src.utils.human_timing import poll_interval
from src.utils.logger import get_logger

log = get_logger(__name__, component="scheduler")


class Scheduler:
    """Autonomous polling loop that drives the entire Sixxer system.

    Parameters
    ----------
    session:
        Session manager for keeping the Fiverr login alive.
    order_monitor:
        For detecting new and changed orders.
    order_actions:
        For delivering work and handling order-level actions.
    inbox:
        For sending buyer messages.
    dispatcher:
        Central dispatch/processing engine.
    state_machine:
        For querying and updating order states.
    db:
        Database connection.
    poll_min:
        Minimum polling interval in minutes.
    poll_max:
        Maximum polling interval in minutes.
    """

    def __init__(
        self,
        session: SessionManager,
        order_monitor: OrderMonitor,
        order_actions: OrderActions,
        inbox: InboxManager,
        dispatcher: Dispatcher,
        state_machine: OrderStateMachine,
        db: Database,
        poll_min: int = 3,
        poll_max: int = 5,
    ) -> None:
        self._session = session
        self._monitor = order_monitor
        self._actions = order_actions
        self._inbox = inbox
        self._dispatcher = dispatcher
        self._sm = state_machine
        self._db = db
        self._poll_min = poll_min
        self._poll_max = poll_max
        self._running = False
        self._cycle_count = 0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the autonomous polling loop.

        Runs until ``stop()`` is called or an unrecoverable error occurs.
        """
        self._running = True
        log.info("scheduler.started", poll_min=self._poll_min, poll_max=self._poll_max)

        while self._running:
            self._cycle_count += 1
            log.info("scheduler.cycle_start", cycle=self._cycle_count)

            try:
                await self._run_cycle()
            except BudgetExceededError:
                log.warning("scheduler.budget_exceeded_pausing")
                # Sleep for an hour before checking again
                await asyncio.sleep(3600)
                continue
            except Exception:
                log.exception("scheduler.cycle_error", cycle=self._cycle_count)
                # Brief pause after error before retrying
                await asyncio.sleep(60)
                continue

            # Sleep for a randomised interval
            sleep_seconds = poll_interval(self._poll_min, self._poll_max)
            log.info(
                "scheduler.sleeping",
                seconds=round(sleep_seconds, 1),
                cycle=self._cycle_count,
            )
            await asyncio.sleep(sleep_seconds)

        log.info("scheduler.stopped", total_cycles=self._cycle_count)

    def stop(self) -> None:
        """Signal the scheduler to stop after the current cycle."""
        self._running = False
        log.info("scheduler.stop_requested")

    # ------------------------------------------------------------------
    # Single cycle
    # ------------------------------------------------------------------

    async def _run_cycle(self) -> None:
        """Execute one full polling cycle."""

        # 1. Ensure session is alive
        log.debug("scheduler.ensuring_session")
        await self._session.ensure_session()

        # 2. Check for new / changed orders
        log.debug("scheduler.checking_orders")
        new_orders = await self._monitor.check_for_new_orders()

        # 3. Process new orders
        for order_data in new_orders:
            fiverr_id = order_data.get("fiverr_order_id", "")
            status = order_data.get("status", "").lower()

            if not fiverr_id:
                continue

            # Only process genuinely new orders (status == "new" or "active")
            if status in ("new", "active", "in progress", ""):
                await self._handle_new_order(order_data)

        # 4. Check for revision requests on delivered orders
        await self._check_for_revisions()

        # 5. Deliver any orders in REVIEW state
        await self._deliver_ready_orders()

        # 6. Send acknowledgments for orders in ANALYZING state
        await self._send_pending_acknowledgments()

        log.info("scheduler.cycle_complete", cycle=self._cycle_count)

    # ------------------------------------------------------------------
    # Order handling
    # ------------------------------------------------------------------

    async def _handle_new_order(self, order_data: dict) -> None:
        """Process a single newly detected order."""
        fiverr_id = order_data["fiverr_order_id"]
        log.info("scheduler.handling_new_order", fiverr_order_id=fiverr_id)

        # Get full order details (requirements text)
        try:
            details = await self._monitor.get_order_details(fiverr_id)
            requirements_text = details.get("requirements", "")
            if isinstance(requirements_text, list):
                requirements_text = "\n".join(str(r) for r in requirements_text)
            requirements_text = str(requirements_text)
        except Exception:
            log.exception("scheduler.get_details_failed", order_id=fiverr_id)
            return

        if not requirements_text:
            log.warning("scheduler.empty_requirements", order_id=fiverr_id)
            requirements_text = f"Order for gig type: {order_data.get('gig_type', 'unknown')}"

        # Determine gig title from context
        gig_title = order_data.get("gig_type", "unknown")

        # Send acknowledgment first
        ack_msg = await self._dispatcher.generate_acknowledgment(fiverr_id)
        if ack_msg:
            try:
                buyer = order_data.get("buyer_username", "")
                if buyer:
                    await self._inbox.send_message_on_order_page(fiverr_id, ack_msg)
            except Exception:
                log.warning("scheduler.ack_send_failed", order_id=fiverr_id)

        # Process through dispatcher
        try:
            result = await self._dispatcher.process_new_order(
                order_id=fiverr_id,
                requirements_text=requirements_text,
                gig_title=gig_title,
            )
        except Exception:
            log.exception("scheduler.dispatch_failed", order_id=fiverr_id)
            return

        if result is None:
            log.info("scheduler.order_needs_clarification", order_id=fiverr_id)
            return

        # If the result has no files, it's a clarification message
        if not result.file_paths and result.message:
            try:
                await self._inbox.send_message_on_order_page(
                    fiverr_id, result.message
                )
            except Exception:
                log.warning("scheduler.clarification_send_failed", order_id=fiverr_id)

    # ------------------------------------------------------------------
    # Revision handling
    # ------------------------------------------------------------------

    async def _check_for_revisions(self) -> None:
        """Check delivered orders for revision requests."""
        delivered = await self._sm.get_orders_by_status(OrderStatus.DELIVERED)

        for order_row in delivered:
            fiverr_id = order_row["fiverr_order_id"]
            try:
                revision = await self._monitor.detect_revision_request(fiverr_id)
                if revision is not None:
                    feedback = revision.get("feedback", "")
                    log.info(
                        "scheduler.revision_detected",
                        order_id=fiverr_id,
                        feedback_preview=feedback[:100],
                    )
                    await self._handle_revision(fiverr_id, feedback)
            except Exception:
                log.warning(
                    "scheduler.revision_check_failed",
                    order_id=fiverr_id,
                    exc_info=True,
                )

    async def _handle_revision(self, order_id: str, feedback: str) -> None:
        """Process a revision request."""
        try:
            result = await self._dispatcher.process_revision(order_id, feedback)
            if result is not None and result.file_paths:
                # The order is now in REVIEW state, will be delivered next cycle
                log.info("scheduler.revision_processed", order_id=order_id)
        except Exception:
            log.exception("scheduler.revision_failed", order_id=order_id)

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def _deliver_ready_orders(self) -> None:
        """Deliver orders that are in REVIEW state."""
        review_orders = await self._sm.get_orders_by_status(OrderStatus.REVIEW)

        for order_row in review_orders:
            fiverr_id = order_row["fiverr_order_id"]
            paths = order_row.get("deliverable_paths", [])
            if isinstance(paths, str):
                try:
                    paths = json.loads(paths)
                except (json.JSONDecodeError, TypeError):
                    paths = []

            if not paths:
                log.warning("scheduler.no_deliverables", order_id=fiverr_id)
                continue

            # Generate delivery message
            buyer = order_row.get("buyer_username", "buyer")
            gig_type = order_row.get("gig_type", "general")
            delivery_msg = await self._dispatcher._communicator.generate_delivery_message(
                buyer_name=buyer,
                gig_type=gig_type,
                summary=f"Completed {gig_type} order",
                files_delivered=", ".join(p.split("/")[-1] for p in paths),
            )

            # Transition to DELIVERING
            try:
                await self._sm.transition(fiverr_id, OrderStatus.DELIVERING)
            except Exception:
                log.warning("scheduler.delivering_transition_failed", order_id=fiverr_id)
                continue

            # Deliver via browser
            try:
                success = await self._actions.deliver_order(
                    order_id=fiverr_id,
                    message=delivery_msg,
                    file_paths=paths,
                )
                if success:
                    await self._sm.transition(fiverr_id, OrderStatus.DELIVERED)
                    log.info("scheduler.order_delivered", order_id=fiverr_id)
                else:
                    log.error("scheduler.delivery_failed", order_id=fiverr_id)
                    await self._sm.transition(
                        fiverr_id,
                        OrderStatus.FAILED,
                        notes="Browser delivery failed",
                    )
            except Exception:
                log.exception("scheduler.delivery_error", order_id=fiverr_id)

    # ------------------------------------------------------------------
    # Acknowledgments
    # ------------------------------------------------------------------

    async def _send_pending_acknowledgments(self) -> None:
        """Send acknowledgment messages for orders that just entered ANALYZING."""
        # This is handled inline in _handle_new_order, but we also check
        # for any orders stuck in NEW state (e.g., from a crash recovery)
        new_orders = await self._sm.get_orders_by_status(OrderStatus.NEW)

        for order_row in new_orders:
            fiverr_id = order_row["fiverr_order_id"]
            log.info("scheduler.recovering_new_order", order_id=fiverr_id)

            # Re-process as if newly detected
            order_data = {
                "fiverr_order_id": fiverr_id,
                "buyer_username": order_row.get("buyer_username", ""),
                "gig_type": order_row.get("gig_type", "writing"),
                "price": str(order_row.get("price", 0)),
                "status": "new",
            }
            await self._handle_new_order(order_data)

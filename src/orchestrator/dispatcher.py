"""Order dispatcher -- routes orders to the correct worker and coordinates
the full processing pipeline: analysis, optional clarification, work
execution, and delivery preparation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.ai.analyzer import OrderAnalyzer
from src.ai.communicator import BuyerCommunicator
from src.models.database import Database
from src.models.schemas import DeliveryPayload, GigType, Order, OrderStatus
from src.orchestrator.state_machine import OrderStateMachine
from src.utils.logger import get_logger
from src.workers.base import BaseWorker
from src.workers.revision_worker import RevisionWorker

log = get_logger(__name__, component="dispatcher")


class Dispatcher:
    """Route orders to the appropriate worker and manage processing.

    The dispatcher is the central brain that ties together order analysis,
    worker execution, buyer communication, and state transitions.

    Parameters
    ----------
    state_machine:
        For managing order status transitions.
    analyzer:
        For analyzing order requirements via Claude.
    communicator:
        For generating buyer-facing messages.
    workers:
        Mapping from ``GigType`` to the worker that handles that type.
    revision_worker:
        Handles revision requests across all gig types.
    db:
        Database for persisting order data.
    """

    def __init__(
        self,
        state_machine: OrderStateMachine,
        analyzer: OrderAnalyzer,
        communicator: BuyerCommunicator,
        workers: dict[GigType, BaseWorker],
        revision_worker: RevisionWorker,
        db: Database,
    ) -> None:
        self._sm = state_machine
        self._analyzer = analyzer
        self._communicator = communicator
        self._workers = workers
        self._revision_worker = revision_worker
        self._db = db

    # ------------------------------------------------------------------
    # Main dispatch entry point
    # ------------------------------------------------------------------

    async def process_new_order(
        self, order_id: str, requirements_text: str, gig_title: str
    ) -> DeliveryPayload | None:
        """Process a newly detected order through the full pipeline.

        Steps:
        1. Transition to ANALYZING
        2. Analyze the order with Claude
        3. If clarification needed -> CLARIFYING (return None, message sent)
        4. Transition to IN_PROGRESS
        5. Execute the worker
        6. Transition to REVIEW
        7. Generate delivery message
        8. Return DeliveryPayload ready for upload

        Returns ``None`` if the order needs clarification first.
        """
        log.info("dispatch.process_new", order_id=order_id)

        # 1. ANALYZING
        await self._sm.transition(order_id, OrderStatus.ANALYZING)

        # 2. Analyze
        order_row = await self._sm.get_order(order_id)
        if order_row is None:
            log.error("dispatch.order_not_found", order_id=order_id)
            return None

        try:
            analysis = await self._analyzer.analyze_order(
                order_text=requirements_text,
                gig_title=gig_title,
                price=order_row.get("price", 0),
                buyer_username=order_row.get("buyer_username", "buyer"),
            )
        except Exception:
            log.exception("dispatch.analysis_failed", order_id=order_id)
            await self._sm.transition(
                order_id, OrderStatus.FAILED, notes="Analysis failed"
            )
            return None

        # Update order with analysis results
        await self._db.execute(
            "UPDATE orders SET gig_type = ?, requirements = ?, updated_at = ? "
            "WHERE id = ? OR fiverr_order_id = ?",
            (
                analysis.gig_type.value,
                json.dumps(analysis.requirements),
                datetime.now(timezone.utc).isoformat(),
                order_id,
                order_id,
            ),
        )

        # 3. Check for clarification
        if analysis.needs_clarification and analysis.clarification_questions:
            await self._sm.transition(order_id, OrderStatus.CLARIFYING)
            buyer = order_row.get("buyer_username", "buyer")
            clarification_msg = await self._communicator.generate_clarification(
                buyer_name=buyer,
                questions=analysis.clarification_questions,
                gig_type=analysis.gig_type.value,
                current_requirements=requirements_text,
            )
            log.info(
                "dispatch.clarification_needed",
                order_id=order_id,
                question_count=len(analysis.clarification_questions),
            )
            return DeliveryPayload(
                message=clarification_msg,
                file_paths=[],
            )

        # 4. IN_PROGRESS
        await self._sm.transition(order_id, OrderStatus.IN_PROGRESS)

        # 5. Execute worker
        order_model = await self._sm.build_order_model(order_id)
        if order_model is None:
            log.error("dispatch.model_build_failed", order_id=order_id)
            return None

        worker = self._workers.get(analysis.gig_type)
        if worker is None:
            log.error(
                "dispatch.no_worker",
                order_id=order_id,
                gig_type=analysis.gig_type.value,
            )
            await self._sm.transition(
                order_id,
                OrderStatus.FAILED,
                notes=f"No worker for gig type: {analysis.gig_type.value}",
            )
            return None

        try:
            deliverable_paths = await worker.process(order_model)
        except Exception:
            log.exception("dispatch.worker_failed", order_id=order_id)
            await self._sm.transition(
                order_id, OrderStatus.FAILED, notes="Worker execution failed"
            )
            return None

        # 6. REVIEW
        path_strings = [str(p) for p in deliverable_paths]
        await self._sm.transition(
            order_id,
            OrderStatus.REVIEW,
            deliverable_paths=path_strings,
        )

        # 7. Generate delivery message
        buyer = order_row.get("buyer_username", "buyer")
        file_names = ", ".join(p.name for p in deliverable_paths)
        summary = f"Completed {analysis.gig_type.value} order with {len(deliverable_paths)} file(s)"

        delivery_msg = await self._communicator.generate_delivery_message(
            buyer_name=buyer,
            gig_type=analysis.gig_type.value,
            summary=summary,
            files_delivered=file_names,
        )

        log.info(
            "dispatch.ready_for_delivery",
            order_id=order_id,
            file_count=len(deliverable_paths),
        )

        return DeliveryPayload(
            message=delivery_msg,
            file_paths=path_strings,
        )

    # ------------------------------------------------------------------
    # Revision processing
    # ------------------------------------------------------------------

    async def process_revision(
        self, order_id: str, feedback: str
    ) -> DeliveryPayload | None:
        """Process a revision request for an existing order.

        Steps:
        1. Transition to IN_PROGRESS
        2. Execute revision worker
        3. Transition to REVIEW
        4. Generate revision delivery message
        5. Return DeliveryPayload

        Returns ``None`` on failure.
        """
        log.info(
            "dispatch.process_revision",
            order_id=order_id,
            feedback_length=len(feedback),
        )

        await self._sm.transition(order_id, OrderStatus.IN_PROGRESS)

        order_model = await self._sm.build_order_model(order_id)
        if order_model is None:
            log.error("dispatch.revision_model_failed", order_id=order_id)
            return None

        try:
            revised_paths = await self._revision_worker.process_revision(
                order_model, feedback
            )
        except Exception:
            log.exception("dispatch.revision_worker_failed", order_id=order_id)
            await self._sm.transition(
                order_id, OrderStatus.FAILED, notes="Revision worker failed"
            )
            return None

        path_strings = [str(p) for p in revised_paths]
        await self._sm.transition(
            order_id,
            OrderStatus.REVIEW,
            deliverable_paths=path_strings,
        )

        buyer = order_model.buyer_username
        changes_summary = f"Applied revision based on feedback: {feedback[:200]}"
        revision_msg = await self._communicator.generate_revision_response(
            buyer_name=buyer,
            changes_summary=changes_summary,
        )

        log.info(
            "dispatch.revision_ready",
            order_id=order_id,
            file_count=len(revised_paths),
        )

        return DeliveryPayload(
            message=revision_msg,
            file_paths=path_strings,
        )

    # ------------------------------------------------------------------
    # Acknowledgment
    # ------------------------------------------------------------------

    async def generate_acknowledgment(self, order_id: str) -> str | None:
        """Generate an acknowledgment message for a new order.

        Returns the message text, or ``None`` if the order is not found.
        """
        order_row = await self._sm.get_order(order_id)
        if order_row is None:
            return None

        buyer = order_row.get("buyer_username", "buyer")
        reqs = order_row.get("requirements", [])
        if isinstance(reqs, list):
            summary = "; ".join(reqs) if reqs else "As described in order"
        else:
            summary = str(reqs)

        msg = await self._communicator.generate_acknowledgment(
            buyer_name=buyer,
            order_summary=summary,
            gig_type=order_row.get("gig_type", "general"),
        )
        return msg

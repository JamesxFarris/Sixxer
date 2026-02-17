"""Tests for the Dispatcher (``src.orchestrator.dispatcher``).

All external dependencies (AI client, analyzer, communicator, workers) are
mocked so these tests run offline without any API keys or network access.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.database import Database
from src.models.schemas import (
    DeliveryPayload,
    GigType,
    Order,
    OrderAnalysis,
    OrderStatus,
)
from src.orchestrator.dispatcher import Dispatcher
from src.orchestrator.state_machine import OrderStateMachine


# =========================================================================
# Helpers
# =========================================================================

def _make_order_row(
    order_id: str = "disp-order-001",
    fiverr_order_id: str = "FO-DISP",
    status: str = "new",
    gig_type: str = "writing",
    buyer: str = "testbuyer",
    price: float = 25.0,
    requirements: list[str] | None = None,
) -> dict:
    """Build a dict resembling a database row for mocking."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": order_id,
        "fiverr_order_id": fiverr_order_id,
        "status": status,
        "gig_type": gig_type,
        "buyer_username": buyer,
        "price": price,
        "requirements": requirements if requirements is not None else ["Test requirement"],
        "created_at": now,
        "updated_at": now,
        "delivered_at": None,
        "deliverable_paths": [],
        "revision_count": 0,
        "notes": "",
    }


def _make_order_model(
    order_id: str = "disp-order-001",
    gig_type: GigType = GigType.WRITING,
    status: OrderStatus = OrderStatus.IN_PROGRESS,
) -> Order:
    """Build an Order model for mocking."""
    return Order(
        id=order_id,
        fiverr_order_id="FO-DISP",
        gig_type=gig_type,
        status=status,
        requirements=["Test requirement"],
        buyer_username="testbuyer",
        price=25.0,
    )


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
def mock_state_machine() -> AsyncMock:
    sm = AsyncMock(spec=OrderStateMachine)
    sm.can_transition = MagicMock(return_value=True)
    return sm


@pytest.fixture
def mock_analyzer() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_communicator() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_worker() -> AsyncMock:
    worker = AsyncMock()
    worker.process = AsyncMock(return_value=[Path("/tmp/article.docx")])
    return worker


@pytest.fixture
def mock_revision_worker() -> AsyncMock:
    rw = AsyncMock()
    rw.process_revision = AsyncMock(return_value=[Path("/tmp/article_rev1.docx")])
    return rw


@pytest.fixture
def mock_db() -> AsyncMock:
    return AsyncMock(spec=Database)


@pytest.fixture
def dispatcher(
    mock_state_machine: AsyncMock,
    mock_analyzer: AsyncMock,
    mock_communicator: AsyncMock,
    mock_worker: AsyncMock,
    mock_revision_worker: AsyncMock,
    mock_db: AsyncMock,
) -> Dispatcher:
    """Return a Dispatcher wired up with all-mock dependencies."""
    workers = {GigType.WRITING: mock_worker}
    return Dispatcher(
        state_machine=mock_state_machine,
        analyzer=mock_analyzer,
        communicator=mock_communicator,
        workers=workers,
        revision_worker=mock_revision_worker,
        db=mock_db,
    )


# =========================================================================
# process_new_order -- happy path
# =========================================================================


class TestProcessNewOrder:
    """Test the full process_new_order pipeline with mocked services."""

    async def test_happy_path(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
        mock_analyzer: AsyncMock,
        mock_communicator: AsyncMock,
        mock_worker: AsyncMock,
        mock_db: AsyncMock,
    ) -> None:
        order_id = "disp-order-001"
        order_row = _make_order_row(order_id)
        order_model = _make_order_model(order_id)

        # Configure mocks
        mock_state_machine.get_order.return_value = order_row
        mock_state_machine.build_order_model.return_value = order_model
        mock_analyzer.analyze_order.return_value = OrderAnalysis(
            gig_type=GigType.WRITING,
            requirements=["Write article"],
            word_count=500,
            needs_clarification=False,
        )
        mock_communicator.generate_delivery_message.return_value = (
            "Here is your completed article!"
        )
        mock_worker.process.return_value = [Path("/tmp/article.docx")]

        result = await dispatcher.process_new_order(
            order_id, "Write a blog post about Python", "SEO Blog Writing"
        )

        # Verify result
        assert result is not None
        assert isinstance(result, DeliveryPayload)
        assert result.message == "Here is your completed article!"
        assert len(result.file_paths) == 1

        # Verify state transitions
        transition_calls = mock_state_machine.transition.call_args_list
        statuses = [call.args[1] for call in transition_calls]
        assert OrderStatus.ANALYZING in statuses
        assert OrderStatus.IN_PROGRESS in statuses
        assert OrderStatus.REVIEW in statuses

        # Verify worker was called
        mock_worker.process.assert_called_once_with(order_model)

        # Verify DB was updated with analysis results
        mock_db.execute.assert_called()

    async def test_clarification_needed(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
        mock_analyzer: AsyncMock,
        mock_communicator: AsyncMock,
        mock_worker: AsyncMock,
    ) -> None:
        """When analysis says clarification is needed, return a clarification
        message and transition to CLARIFYING instead of IN_PROGRESS."""
        order_id = "disp-clarify"
        order_row = _make_order_row(order_id)
        mock_state_machine.get_order.return_value = order_row

        mock_analyzer.analyze_order.return_value = OrderAnalysis(
            gig_type=GigType.WRITING,
            requirements=["Vague requirement"],
            needs_clarification=True,
            clarification_questions=["What tone do you prefer?", "Target audience?"],
        )
        mock_communicator.generate_clarification.return_value = (
            "Hi! Could you clarify a few things?"
        )

        result = await dispatcher.process_new_order(
            order_id, "Write something", "Writing Gig"
        )

        # Should return a DeliveryPayload with the clarification message
        assert result is not None
        assert result.message == "Hi! Could you clarify a few things?"
        assert result.file_paths == []

        # State should have gone to ANALYZING then CLARIFYING
        transition_calls = mock_state_machine.transition.call_args_list
        statuses = [call.args[1] for call in transition_calls]
        assert OrderStatus.ANALYZING in statuses
        assert OrderStatus.CLARIFYING in statuses
        # Worker should NOT have been called
        mock_worker.process.assert_not_called()

    async def test_analysis_failure_transitions_to_failed(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
        mock_analyzer: AsyncMock,
        mock_worker: AsyncMock,
    ) -> None:
        """When the analyzer raises, the order should move to FAILED."""
        order_id = "disp-fail"
        order_row = _make_order_row(order_id)
        mock_state_machine.get_order.return_value = order_row
        mock_analyzer.analyze_order.side_effect = RuntimeError("AI error")

        result = await dispatcher.process_new_order(
            order_id, "Requirements", "Gig Title"
        )

        assert result is None

        # Should transition to ANALYZING first, then FAILED
        transition_calls = mock_state_machine.transition.call_args_list
        statuses = [call.args[1] for call in transition_calls]
        assert OrderStatus.ANALYZING in statuses
        assert OrderStatus.FAILED in statuses
        mock_worker.process.assert_not_called()

    async def test_order_not_found_after_analyzing(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
        mock_analyzer: AsyncMock,
    ) -> None:
        """If get_order returns None after ANALYZING, return None."""
        mock_state_machine.get_order.return_value = None

        result = await dispatcher.process_new_order(
            "missing", "Reqs", "Title"
        )
        assert result is None

    async def test_no_worker_for_gig_type(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
        mock_analyzer: AsyncMock,
    ) -> None:
        """If there is no worker registered for the gig type, go to FAILED."""
        order_id = "disp-no-worker"
        order_row = _make_order_row(order_id, gig_type="coding")
        order_model = _make_order_model(order_id, gig_type=GigType.CODING)

        mock_state_machine.get_order.return_value = order_row
        mock_state_machine.build_order_model.return_value = order_model
        mock_analyzer.analyze_order.return_value = OrderAnalysis(
            gig_type=GigType.CODING,
            requirements=["Build API"],
            needs_clarification=False,
        )

        result = await dispatcher.process_new_order(
            order_id, "Build an API", "Coding Gig"
        )

        assert result is None
        transition_calls = mock_state_machine.transition.call_args_list
        statuses = [call.args[1] for call in transition_calls]
        assert OrderStatus.FAILED in statuses

    async def test_worker_failure_transitions_to_failed(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
        mock_analyzer: AsyncMock,
        mock_worker: AsyncMock,
    ) -> None:
        """If the worker raises an exception, order should move to FAILED."""
        order_id = "disp-worker-fail"
        order_row = _make_order_row(order_id)
        order_model = _make_order_model(order_id)

        mock_state_machine.get_order.return_value = order_row
        mock_state_machine.build_order_model.return_value = order_model
        mock_analyzer.analyze_order.return_value = OrderAnalysis(
            gig_type=GigType.WRITING,
            requirements=["Write stuff"],
            needs_clarification=False,
        )
        mock_worker.process.side_effect = RuntimeError("Worker crashed")

        result = await dispatcher.process_new_order(
            order_id, "Requirements", "Title"
        )

        assert result is None
        transition_calls = mock_state_machine.transition.call_args_list
        statuses = [call.args[1] for call in transition_calls]
        assert OrderStatus.FAILED in statuses


# =========================================================================
# process_revision
# =========================================================================


class TestProcessRevision:
    """Test the revision processing pipeline."""

    async def test_revision_happy_path(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
        mock_revision_worker: AsyncMock,
        mock_communicator: AsyncMock,
    ) -> None:
        order_id = "disp-rev-001"
        order_model = _make_order_model(
            order_id, status=OrderStatus.REVISION_REQUESTED
        )
        mock_state_machine.build_order_model.return_value = order_model
        mock_revision_worker.process_revision.return_value = [
            Path("/tmp/article_rev1.docx")
        ]
        mock_communicator.generate_revision_response.return_value = (
            "I have updated the article based on your feedback."
        )

        result = await dispatcher.process_revision(
            order_id, "Please make the intro shorter"
        )

        assert result is not None
        assert isinstance(result, DeliveryPayload)
        assert "updated the article" in result.message
        assert len(result.file_paths) == 1

        # Verify transitions: IN_PROGRESS then REVIEW
        transition_calls = mock_state_machine.transition.call_args_list
        statuses = [call.args[1] for call in transition_calls]
        assert OrderStatus.IN_PROGRESS in statuses
        assert OrderStatus.REVIEW in statuses

        mock_revision_worker.process_revision.assert_called_once_with(
            order_model, "Please make the intro shorter"
        )

    async def test_revision_model_not_found(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
    ) -> None:
        mock_state_machine.build_order_model.return_value = None

        result = await dispatcher.process_revision("missing", "feedback")
        assert result is None

    async def test_revision_worker_failure(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
        mock_revision_worker: AsyncMock,
    ) -> None:
        order_model = _make_order_model("rev-fail")
        mock_state_machine.build_order_model.return_value = order_model
        mock_revision_worker.process_revision.side_effect = RuntimeError("crash")

        result = await dispatcher.process_revision("rev-fail", "fix it")

        assert result is None
        transition_calls = mock_state_machine.transition.call_args_list
        statuses = [call.args[1] for call in transition_calls]
        assert OrderStatus.FAILED in statuses


# =========================================================================
# generate_acknowledgment
# =========================================================================


class TestGenerateAcknowledgment:
    """Test the acknowledgment message generation."""

    async def test_generates_message(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
        mock_communicator: AsyncMock,
    ) -> None:
        order_row = _make_order_row(
            "ack-001",
            requirements=["Write about AI", "500 words"],
        )
        mock_state_machine.get_order.return_value = order_row
        mock_communicator.generate_acknowledgment.return_value = (
            "Thank you for your order!"
        )

        result = await dispatcher.generate_acknowledgment("ack-001")

        assert result == "Thank you for your order!"
        mock_communicator.generate_acknowledgment.assert_called_once()

        # Verify the communicator was called with correct buyer name
        call_kwargs = mock_communicator.generate_acknowledgment.call_args
        assert call_kwargs.kwargs["buyer_name"] == "testbuyer"

    async def test_order_not_found_returns_none(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
    ) -> None:
        mock_state_machine.get_order.return_value = None
        result = await dispatcher.generate_acknowledgment("missing")
        assert result is None

    async def test_empty_requirements_handled(
        self,
        dispatcher: Dispatcher,
        mock_state_machine: AsyncMock,
        mock_communicator: AsyncMock,
    ) -> None:
        """When requirements is an empty list, should use fallback text."""
        order_row = _make_order_row("ack-empty", requirements=[])
        mock_state_machine.get_order.return_value = order_row
        mock_communicator.generate_acknowledgment.return_value = "Got it!"

        result = await dispatcher.generate_acknowledgment("ack-empty")
        assert result == "Got it!"

        call_kwargs = mock_communicator.generate_acknowledgment.call_args
        assert call_kwargs.kwargs["order_summary"] == "As described in order"

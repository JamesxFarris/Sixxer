"""Tests for the order lifecycle state machine (``src.orchestrator.state_machine``)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.models.database import Database
from src.models.schemas import GigType, Order, OrderStatus
from src.orchestrator.state_machine import InvalidTransitionError, OrderStateMachine


# =========================================================================
# Helpers
# =========================================================================

async def _insert_order(
    db: Database,
    order_id: str = "test-order-001",
    fiverr_order_id: str = "FO-12345",
    status: OrderStatus = OrderStatus.NEW,
    gig_type: GigType = GigType.WRITING,
    buyer: str = "testbuyer",
    price: float = 25.0,
    requirements: list[str] | None = None,
) -> str:
    """Insert a test order and return its id."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO orders "
        "(id, fiverr_order_id, gig_type, status, requirements, "
        " buyer_username, price, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            order_id,
            fiverr_order_id,
            gig_type.value,
            status.value,
            json.dumps(requirements or ["Test requirement"]),
            buyer,
            price,
            now,
            now,
        ),
    )
    return order_id


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture
async def sm(db: Database) -> OrderStateMachine:
    """Return a state machine backed by the in-memory test database."""
    return OrderStateMachine(db)


@pytest.fixture
async def sm_with_order(
    db: Database, sm: OrderStateMachine
) -> tuple[OrderStateMachine, str]:
    """Return a state machine plus the id of a pre-inserted NEW order."""
    oid = await _insert_order(db)
    return sm, oid


# =========================================================================
# can_transition (synchronous)
# =========================================================================


class TestCanTransition:
    """Verify the static transition-table lookup."""

    @pytest.mark.parametrize(
        "current, target",
        [
            (OrderStatus.NEW, OrderStatus.ANALYZING),
            (OrderStatus.NEW, OrderStatus.FAILED),
            (OrderStatus.NEW, OrderStatus.CANCELLED),
            (OrderStatus.ANALYZING, OrderStatus.CLARIFYING),
            (OrderStatus.ANALYZING, OrderStatus.IN_PROGRESS),
            (OrderStatus.ANALYZING, OrderStatus.FAILED),
            (OrderStatus.CLARIFYING, OrderStatus.IN_PROGRESS),
            (OrderStatus.IN_PROGRESS, OrderStatus.REVIEW),
            (OrderStatus.REVIEW, OrderStatus.DELIVERING),
            (OrderStatus.REVIEW, OrderStatus.IN_PROGRESS),
            (OrderStatus.DELIVERING, OrderStatus.DELIVERED),
            (OrderStatus.DELIVERED, OrderStatus.COMPLETED),
            (OrderStatus.DELIVERED, OrderStatus.REVISION_REQUESTED),
            (OrderStatus.REVISION_REQUESTED, OrderStatus.IN_PROGRESS),
            (OrderStatus.FAILED, OrderStatus.NEW),
        ],
    )
    async def test_valid_transitions(
        self, sm: OrderStateMachine, current: OrderStatus, target: OrderStatus
    ) -> None:
        assert sm.can_transition(current, target) is True

    @pytest.mark.parametrize(
        "current, target",
        [
            (OrderStatus.NEW, OrderStatus.DELIVERED),
            (OrderStatus.NEW, OrderStatus.IN_PROGRESS),
            (OrderStatus.COMPLETED, OrderStatus.NEW),
            (OrderStatus.COMPLETED, OrderStatus.DELIVERED),
            (OrderStatus.CANCELLED, OrderStatus.NEW),
            (OrderStatus.DELIVERING, OrderStatus.IN_PROGRESS),
            (OrderStatus.IN_PROGRESS, OrderStatus.DELIVERED),
            (OrderStatus.DELIVERED, OrderStatus.IN_PROGRESS),
        ],
    )
    async def test_invalid_transitions(
        self, sm: OrderStateMachine, current: OrderStatus, target: OrderStatus
    ) -> None:
        assert sm.can_transition(current, target) is False


# =========================================================================
# transition (async, persisted to DB)
# =========================================================================


class TestTransition:
    """Verify that ``transition()`` updates the database correctly."""

    async def test_simple_transition(
        self, db: Database, sm_with_order: tuple[OrderStateMachine, str]
    ) -> None:
        sm, oid = sm_with_order
        await sm.transition(oid, OrderStatus.ANALYZING)
        row = await db.fetch_one("SELECT status FROM orders WHERE id = ?", (oid,))
        assert row is not None
        assert row["status"] == OrderStatus.ANALYZING.value

    async def test_invalid_transition_raises(
        self, sm_with_order: tuple[OrderStateMachine, str]
    ) -> None:
        sm, oid = sm_with_order
        with pytest.raises(InvalidTransitionError, match="new -> delivered"):
            await sm.transition(oid, OrderStatus.DELIVERED)

    async def test_order_not_found_raises(self, sm: OrderStateMachine) -> None:
        with pytest.raises(ValueError, match="Order not found"):
            await sm.transition("nonexistent-id", OrderStatus.ANALYZING)

    async def test_transition_updates_updated_at(
        self, db: Database, sm_with_order: tuple[OrderStateMachine, str]
    ) -> None:
        sm, oid = sm_with_order
        before = await db.fetch_one("SELECT updated_at FROM orders WHERE id = ?", (oid,))
        assert before is not None

        await sm.transition(oid, OrderStatus.ANALYZING)

        after = await db.fetch_one("SELECT updated_at FROM orders WHERE id = ?", (oid,))
        assert after is not None
        assert after["updated_at"] != before["updated_at"]

    async def test_transition_with_notes(
        self, db: Database, sm_with_order: tuple[OrderStateMachine, str]
    ) -> None:
        sm, oid = sm_with_order
        await sm.transition(oid, OrderStatus.FAILED, notes="Timeout error")
        row = await db.fetch_one("SELECT notes FROM orders WHERE id = ?", (oid,))
        assert row is not None
        assert "Timeout error" in row["notes"]

    async def test_transition_with_deliverable_paths(
        self, db: Database, sm_with_order: tuple[OrderStateMachine, str]
    ) -> None:
        sm, oid = sm_with_order
        # Walk to IN_PROGRESS -> REVIEW so we can set deliverable_paths
        await sm.transition(oid, OrderStatus.ANALYZING)
        await sm.transition(oid, OrderStatus.IN_PROGRESS)

        paths = ["/tmp/article.docx"]
        await sm.transition(oid, OrderStatus.REVIEW, deliverable_paths=paths)

        row = await db.fetch_one(
            "SELECT deliverable_paths FROM orders WHERE id = ?", (oid,)
        )
        assert row is not None
        assert row["deliverable_paths"] == paths

    async def test_delivered_sets_delivered_at(
        self, db: Database, sm_with_order: tuple[OrderStateMachine, str]
    ) -> None:
        sm, oid = sm_with_order
        # Walk order to DELIVERING
        await sm.transition(oid, OrderStatus.ANALYZING)
        await sm.transition(oid, OrderStatus.IN_PROGRESS)
        await sm.transition(oid, OrderStatus.REVIEW)
        await sm.transition(oid, OrderStatus.DELIVERING)

        await sm.transition(oid, OrderStatus.DELIVERED)

        row = await db.fetch_one("SELECT delivered_at FROM orders WHERE id = ?", (oid,))
        assert row is not None
        assert row["delivered_at"] is not None

    async def test_revision_requested_increments_revision_count(
        self, db: Database, sm_with_order: tuple[OrderStateMachine, str]
    ) -> None:
        sm, oid = sm_with_order
        # Walk order to DELIVERED
        await sm.transition(oid, OrderStatus.ANALYZING)
        await sm.transition(oid, OrderStatus.IN_PROGRESS)
        await sm.transition(oid, OrderStatus.REVIEW)
        await sm.transition(oid, OrderStatus.DELIVERING)
        await sm.transition(oid, OrderStatus.DELIVERED)

        await sm.transition(oid, OrderStatus.REVISION_REQUESTED)

        row = await db.fetch_one(
            "SELECT revision_count FROM orders WHERE id = ?", (oid,)
        )
        assert row is not None
        assert row["revision_count"] == 1

    async def test_lookup_by_fiverr_order_id(
        self, db: Database, sm: OrderStateMachine
    ) -> None:
        """transition() should also work when given a fiverr_order_id."""
        await _insert_order(db, order_id="oid-99", fiverr_order_id="FO-LOOKUP")
        await sm.transition("FO-LOOKUP", OrderStatus.ANALYZING)
        row = await db.fetch_one(
            "SELECT status FROM orders WHERE fiverr_order_id = ?", ("FO-LOOKUP",)
        )
        assert row is not None
        assert row["status"] == "analyzing"


# =========================================================================
# Happy-path lifecycle
# =========================================================================


class TestFullLifecycle:
    """Walk an order through the entire happy path and the revision path."""

    async def test_happy_path(
        self, db: Database, sm_with_order: tuple[OrderStateMachine, str]
    ) -> None:
        """NEW -> ANALYZING -> IN_PROGRESS -> REVIEW -> DELIVERING -> DELIVERED -> COMPLETED"""
        sm, oid = sm_with_order
        path = [
            OrderStatus.ANALYZING,
            OrderStatus.IN_PROGRESS,
            OrderStatus.REVIEW,
            OrderStatus.DELIVERING,
            OrderStatus.DELIVERED,
            OrderStatus.COMPLETED,
        ]
        for target in path:
            await sm.transition(oid, target)

        row = await db.fetch_one("SELECT status FROM orders WHERE id = ?", (oid,))
        assert row is not None
        assert row["status"] == OrderStatus.COMPLETED.value

    async def test_revision_path(
        self, db: Database, sm_with_order: tuple[OrderStateMachine, str]
    ) -> None:
        """Walk through delivery, then revision, then re-delivery."""
        sm, oid = sm_with_order
        # Deliver first
        for target in [
            OrderStatus.ANALYZING,
            OrderStatus.IN_PROGRESS,
            OrderStatus.REVIEW,
            OrderStatus.DELIVERING,
            OrderStatus.DELIVERED,
        ]:
            await sm.transition(oid, target)

        # Revision cycle
        await sm.transition(oid, OrderStatus.REVISION_REQUESTED)
        await sm.transition(oid, OrderStatus.IN_PROGRESS)
        await sm.transition(oid, OrderStatus.REVIEW)
        await sm.transition(oid, OrderStatus.DELIVERING)
        await sm.transition(oid, OrderStatus.DELIVERED)

        # Verify revision_count incremented
        row = await db.fetch_one(
            "SELECT revision_count, status FROM orders WHERE id = ?", (oid,)
        )
        assert row is not None
        assert row["revision_count"] == 1
        assert row["status"] == OrderStatus.DELIVERED.value

        # Complete
        await sm.transition(oid, OrderStatus.COMPLETED)
        row = await db.fetch_one("SELECT status FROM orders WHERE id = ?", (oid,))
        assert row is not None
        assert row["status"] == OrderStatus.COMPLETED.value

    async def test_failed_to_new_retry(
        self, db: Database, sm_with_order: tuple[OrderStateMachine, str]
    ) -> None:
        """FAILED -> NEW retry path should work."""
        sm, oid = sm_with_order
        await sm.transition(oid, OrderStatus.FAILED, notes="Temporary error")
        await sm.transition(oid, OrderStatus.NEW)

        row = await db.fetch_one("SELECT status FROM orders WHERE id = ?", (oid,))
        assert row is not None
        assert row["status"] == OrderStatus.NEW.value


# =========================================================================
# Query helpers
# =========================================================================


class TestQueryHelpers:
    """Verify get_orders_by_status and build_order_model."""

    async def test_get_orders_by_status(
        self, db: Database, sm: OrderStateMachine
    ) -> None:
        await _insert_order(db, "o1", "FO-1", OrderStatus.NEW)
        await _insert_order(db, "o2", "FO-2", OrderStatus.NEW)
        await _insert_order(db, "o3", "FO-3", OrderStatus.IN_PROGRESS)

        new_orders = await sm.get_orders_by_status(OrderStatus.NEW)
        assert len(new_orders) == 2

        in_progress = await sm.get_orders_by_status(OrderStatus.IN_PROGRESS)
        assert len(in_progress) == 1

        combined = await sm.get_orders_by_status(
            OrderStatus.NEW, OrderStatus.IN_PROGRESS
        )
        assert len(combined) == 3

    async def test_get_orders_by_status_empty(
        self, sm: OrderStateMachine
    ) -> None:
        result = await sm.get_orders_by_status(OrderStatus.COMPLETED)
        assert result == []

    async def test_build_order_model(
        self, db: Database, sm: OrderStateMachine
    ) -> None:
        await _insert_order(
            db,
            order_id="model-test",
            fiverr_order_id="FO-MODEL",
            requirements=["Req A", "Req B"],
        )
        order = await sm.build_order_model("model-test")
        assert order is not None
        assert isinstance(order, Order)
        assert order.id == "model-test"
        assert order.fiverr_order_id == "FO-MODEL"
        assert order.gig_type == GigType.WRITING
        assert order.status == OrderStatus.NEW
        assert order.requirements == ["Req A", "Req B"]
        assert order.buyer_username == "testbuyer"
        assert order.price == 25.0

    async def test_build_order_model_not_found(
        self, sm: OrderStateMachine
    ) -> None:
        result = await sm.build_order_model("does-not-exist")
        assert result is None

    async def test_get_order(
        self, db: Database, sm: OrderStateMachine
    ) -> None:
        await _insert_order(db, "get-test", "FO-GET")
        row = await sm.get_order("get-test")
        assert row is not None
        assert row["id"] == "get-test"

        # Also works by fiverr_order_id
        row2 = await sm.get_order("FO-GET")
        assert row2 is not None
        assert row2["id"] == "get-test"

    async def test_get_order_not_found(self, sm: OrderStateMachine) -> None:
        result = await sm.get_order("nope")
        assert result is None

"""Tests for Pydantic models and enums defined in ``src.models.schemas``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models.schemas import (
    ApiCost,
    DeliveryPayload,
    Gig,
    GigType,
    Message,
    Order,
    OrderAnalysis,
    OrderStatus,
)


# =========================================================================
# OrderStatus enum
# =========================================================================


class TestOrderStatus:
    """Validate the OrderStatus enum members and their string values."""

    def test_member_count(self) -> None:
        assert len(OrderStatus) == 11

    @pytest.mark.parametrize(
        "member, value",
        [
            (OrderStatus.NEW, "new"),
            (OrderStatus.ANALYZING, "analyzing"),
            (OrderStatus.CLARIFYING, "clarifying"),
            (OrderStatus.IN_PROGRESS, "in_progress"),
            (OrderStatus.REVIEW, "review"),
            (OrderStatus.DELIVERING, "delivering"),
            (OrderStatus.DELIVERED, "delivered"),
            (OrderStatus.COMPLETED, "completed"),
            (OrderStatus.REVISION_REQUESTED, "revision_requested"),
            (OrderStatus.CANCELLED, "cancelled"),
            (OrderStatus.FAILED, "failed"),
        ],
    )
    def test_values(self, member: OrderStatus, value: str) -> None:
        assert member.value == value

    def test_is_str_subclass(self) -> None:
        """OrderStatus members should behave as plain strings."""
        assert isinstance(OrderStatus.NEW, str)
        assert OrderStatus.NEW == "new"

    def test_construction_from_value(self) -> None:
        assert OrderStatus("in_progress") is OrderStatus.IN_PROGRESS

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            OrderStatus("nonexistent")


# =========================================================================
# GigType enum
# =========================================================================


class TestGigType:
    """Validate the GigType enum members and their string values."""

    def test_member_count(self) -> None:
        assert len(GigType) == 3

    @pytest.mark.parametrize(
        "member, value",
        [
            (GigType.WRITING, "writing"),
            (GigType.CODING, "coding"),
            (GigType.DATA_ENTRY, "data_entry"),
        ],
    )
    def test_values(self, member: GigType, value: str) -> None:
        assert member.value == value

    def test_is_str_subclass(self) -> None:
        assert isinstance(GigType.WRITING, str)

    def test_construction_from_value(self) -> None:
        assert GigType("coding") is GigType.CODING

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            GigType("photography")


# =========================================================================
# Order model
# =========================================================================


class TestOrder:
    """Validate the Order Pydantic model."""

    def test_creation_with_required_fields_only(self) -> None:
        order = Order(
            fiverr_order_id="FO-001",
            gig_type=GigType.WRITING,
            buyer_username="alice",
            price=15.0,
        )
        # Defaults
        assert order.status == OrderStatus.NEW
        assert order.requirements == []
        assert order.deliverable_paths == []
        assert order.revision_count == 0
        assert order.notes == ""
        assert order.delivered_at is None
        # Auto-generated
        assert isinstance(order.id, str)
        assert len(order.id) == 32  # uuid4().hex length
        assert isinstance(order.created_at, datetime)
        assert isinstance(order.updated_at, datetime)

    def test_creation_with_all_fields(self) -> None:
        now = datetime.now(timezone.utc)
        order = Order(
            id="custom-id-123",
            fiverr_order_id="FO-999",
            gig_type=GigType.CODING,
            status=OrderStatus.IN_PROGRESS,
            requirements=["Build a REST API", "Use FastAPI"],
            buyer_username="bob",
            price=150.0,
            created_at=now,
            updated_at=now,
            delivered_at=now,
            deliverable_paths=["/tmp/api.py"],
            revision_count=2,
            notes="Urgent order",
        )
        assert order.id == "custom-id-123"
        assert order.fiverr_order_id == "FO-999"
        assert order.gig_type == GigType.CODING
        assert order.status == OrderStatus.IN_PROGRESS
        assert order.requirements == ["Build a REST API", "Use FastAPI"]
        assert order.buyer_username == "bob"
        assert order.price == 150.0
        assert order.delivered_at == now
        assert order.deliverable_paths == ["/tmp/api.py"]
        assert order.revision_count == 2
        assert order.notes == "Urgent order"

    def test_unique_ids_generated(self) -> None:
        order_a = Order(
            fiverr_order_id="FO-A",
            gig_type=GigType.WRITING,
            buyer_username="user",
            price=10.0,
        )
        order_b = Order(
            fiverr_order_id="FO-B",
            gig_type=GigType.WRITING,
            buyer_username="user",
            price=10.0,
        )
        assert order_a.id != order_b.id

    def test_serialization_round_trip(self) -> None:
        order = Order(
            fiverr_order_id="FO-RT",
            gig_type=GigType.DATA_ENTRY,
            buyer_username="charlie",
            price=30.0,
            requirements=["Convert PDF to Excel"],
        )
        data = order.model_dump()
        restored = Order(**data)
        assert restored.fiverr_order_id == order.fiverr_order_id
        assert restored.requirements == order.requirements
        assert restored.gig_type == order.gig_type

    def test_gig_type_from_string(self) -> None:
        """Pydantic should coerce string values to the GigType enum."""
        order = Order(
            fiverr_order_id="FO-S",
            gig_type="writing",  # type: ignore[arg-type]
            buyer_username="user",
            price=10.0,
        )
        assert order.gig_type is GigType.WRITING


# =========================================================================
# OrderAnalysis model
# =========================================================================


class TestOrderAnalysis:
    """Validate the OrderAnalysis Pydantic model."""

    def test_minimal_creation(self) -> None:
        analysis = OrderAnalysis(
            gig_type=GigType.WRITING,
            requirements=["Write an article"],
        )
        assert analysis.gig_type == GigType.WRITING
        assert analysis.requirements == ["Write an article"]
        assert analysis.word_count is None
        assert analysis.row_count is None
        assert analysis.script_complexity is None
        assert analysis.needs_clarification is False
        assert analysis.clarification_questions == []

    def test_full_creation(self) -> None:
        analysis = OrderAnalysis(
            gig_type=GigType.CODING,
            requirements=["Build CLI tool", "Add tests"],
            word_count=None,
            row_count=None,
            script_complexity="complex",
            needs_clarification=True,
            clarification_questions=["What Python version?", "Any dependencies?"],
        )
        assert analysis.script_complexity == "complex"
        assert analysis.needs_clarification is True
        assert len(analysis.clarification_questions) == 2

    def test_data_entry_fields(self) -> None:
        analysis = OrderAnalysis(
            gig_type=GigType.DATA_ENTRY,
            requirements=["Enter 500 rows"],
            row_count=500,
        )
        assert analysis.row_count == 500
        assert analysis.word_count is None


# =========================================================================
# DeliveryPayload model
# =========================================================================


class TestDeliveryPayload:
    """Validate the DeliveryPayload Pydantic model."""

    def test_message_only(self) -> None:
        payload = DeliveryPayload(message="Here is your order!")
        assert payload.message == "Here is your order!"
        assert payload.file_paths == []

    def test_message_with_files(self) -> None:
        payload = DeliveryPayload(
            message="Delivery attached.",
            file_paths=["/tmp/article.docx", "/tmp/outline.txt"],
        )
        assert len(payload.file_paths) == 2
        assert "/tmp/article.docx" in payload.file_paths

    def test_serialization(self) -> None:
        payload = DeliveryPayload(message="Done", file_paths=["a.txt"])
        data = payload.model_dump()
        assert data["message"] == "Done"
        assert data["file_paths"] == ["a.txt"]


# =========================================================================
# Message model
# =========================================================================


class TestMessage:
    """Validate the Message Pydantic model."""

    def test_creation(self) -> None:
        msg = Message(
            order_id="ord-1",
            direction="sent",
            content="Hello!",
        )
        assert msg.order_id == "ord-1"
        assert msg.direction == "sent"
        assert msg.content == "Hello!"
        assert msg.id is None
        assert isinstance(msg.timestamp, datetime)

    def test_received_direction(self) -> None:
        msg = Message(
            order_id="ord-2",
            direction="received",
            content="Thanks!",
        )
        assert msg.direction == "received"


# =========================================================================
# ApiCost model
# =========================================================================


class TestApiCost:
    """Validate the ApiCost Pydantic model."""

    def test_creation(self) -> None:
        cost = ApiCost(
            model="claude-haiku-3-5-20241022",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0006,
            purpose="test",
        )
        assert cost.model == "claude-haiku-3-5-20241022"
        assert cost.input_tokens == 100
        assert cost.output_tokens == 50
        assert cost.cost_usd == pytest.approx(0.0006)
        assert cost.purpose == "test"
        assert cost.id is None
        assert isinstance(cost.timestamp, datetime)


# =========================================================================
# Gig model
# =========================================================================


class TestGig:
    """Validate the Gig Pydantic model."""

    def test_creation(self) -> None:
        gig = Gig(
            gig_type=GigType.WRITING,
            title="I will write SEO blog posts",
        )
        assert gig.gig_type == GigType.WRITING
        assert gig.title == "I will write SEO blog posts"
        assert gig.status == "active"
        assert gig.id is None
        assert gig.fiverr_gig_id is None
        assert isinstance(gig.created_at, datetime)

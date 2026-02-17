"""Pydantic models and enums for the Sixxer domain.

These schemas are the single source of truth for data shapes used across the
application -- from database rows to AI payloads to inter-module contracts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OrderStatus(str, Enum):
    """Lifecycle stages of a Fiverr order."""

    NEW = "new"
    ANALYZING = "analyzing"
    CLARIFYING = "clarifying"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    REVISION_REQUESTED = "revision_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"


class GigType(str, Enum):
    """Supported gig verticals."""

    WRITING = "writing"
    CODING = "coding"
    DATA_ENTRY = "data_entry"


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


class Order(BaseModel):
    """Represents a single Fiverr order throughout its lifecycle."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    fiverr_order_id: str
    gig_type: GigType
    status: OrderStatus = OrderStatus.NEW
    requirements: list[str] = Field(default_factory=list)
    buyer_username: str
    price: float
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    delivered_at: datetime | None = None
    deliverable_paths: list[str] = Field(default_factory=list)
    revision_count: int = 0
    notes: str = ""


class Message(BaseModel):
    """A single message exchanged with a buyer."""

    id: int | None = None
    order_id: str
    direction: Literal["sent", "received"]
    content: str
    timestamp: datetime = Field(default_factory=_utcnow)


class ApiCost(BaseModel):
    """Tracks a single AI API call for cost accounting."""

    id: int | None = None
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    purpose: str
    timestamp: datetime = Field(default_factory=_utcnow)


class Gig(BaseModel):
    """A Fiverr gig listing managed by the system."""

    id: int | None = None
    fiverr_gig_id: str | None = None
    gig_type: GigType
    title: str
    status: str = "active"
    created_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# AI analysis / delivery payloads
# ---------------------------------------------------------------------------

class OrderAnalysis(BaseModel):
    """Structured output from the AI's analysis of order requirements."""

    gig_type: GigType
    requirements: list[str]
    word_count: int | None = None
    row_count: int | None = None
    script_complexity: str | None = None
    needs_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list)


class DeliveryPayload(BaseModel):
    """Data required to deliver an order on Fiverr."""

    message: str
    file_paths: list[str] = Field(default_factory=list)

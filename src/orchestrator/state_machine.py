"""Order lifecycle state machine.

Manages valid transitions between ``OrderStatus`` values and persists state
changes to the database.  The machine enforces the directed graph defined in
the project architecture so that orders can only progress through legal states.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.models.database import Database
from src.models.schemas import Order, OrderStatus
from src.utils.logger import get_logger

log = get_logger(__name__, component="state_machine")

# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------

_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.NEW: {OrderStatus.ANALYZING, OrderStatus.FAILED, OrderStatus.CANCELLED},
    OrderStatus.ANALYZING: {
        OrderStatus.CLARIFYING,
        OrderStatus.IN_PROGRESS,
        OrderStatus.FAILED,
        OrderStatus.CANCELLED,
    },
    OrderStatus.CLARIFYING: {
        OrderStatus.IN_PROGRESS,
        OrderStatus.FAILED,
        OrderStatus.CANCELLED,
    },
    OrderStatus.IN_PROGRESS: {
        OrderStatus.REVIEW,
        OrderStatus.FAILED,
        OrderStatus.CANCELLED,
    },
    OrderStatus.REVIEW: {
        OrderStatus.DELIVERING,
        OrderStatus.IN_PROGRESS,
        OrderStatus.FAILED,
        OrderStatus.CANCELLED,
    },
    OrderStatus.DELIVERING: {
        OrderStatus.DELIVERED,
        OrderStatus.FAILED,
        OrderStatus.CANCELLED,
    },
    OrderStatus.DELIVERED: {
        OrderStatus.COMPLETED,
        OrderStatus.REVISION_REQUESTED,
    },
    OrderStatus.REVISION_REQUESTED: {
        OrderStatus.IN_PROGRESS,
        OrderStatus.FAILED,
        OrderStatus.CANCELLED,
    },
    OrderStatus.COMPLETED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.FAILED: {OrderStatus.NEW},  # allow retry
}


class InvalidTransitionError(Exception):
    """Raised when an illegal state transition is attempted."""


class OrderStateMachine:
    """Manage order lifecycle state transitions.

    Parameters
    ----------
    db:
        A connected ``Database`` instance for persisting state changes.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Transition logic
    # ------------------------------------------------------------------

    def can_transition(self, current: OrderStatus, target: OrderStatus) -> bool:
        """Return ``True`` if *current* -> *target* is a valid transition."""
        return target in _TRANSITIONS.get(current, set())

    async def transition(
        self,
        order_id: str,
        target: OrderStatus,
        *,
        notes: str = "",
        deliverable_paths: list[str] | None = None,
    ) -> None:
        """Transition an order to *target* status.

        Parameters
        ----------
        order_id:
            Primary key of the order in the database.
        target:
            The desired new status.
        notes:
            Optional notes to append to the order record.
        deliverable_paths:
            If provided, overwrite the order's deliverable path list.

        Raises
        ------
        InvalidTransitionError
            If the transition is not allowed by the state graph.
        """
        row = await self._db.fetch_one(
            "SELECT id, status FROM orders WHERE id = ? OR fiverr_order_id = ?",
            (order_id, order_id),
        )
        if row is None:
            raise ValueError(f"Order not found: {order_id}")

        current = OrderStatus(row["status"])
        db_id = row["id"]

        if not self.can_transition(current, target):
            raise InvalidTransitionError(
                f"Cannot transition order {order_id} from "
                f"{current.value} -> {target.value}"
            )

        now = datetime.now(timezone.utc).isoformat()
        updates = ["status = ?", "updated_at = ?"]
        params: list[str | float] = [target.value, now]

        if notes:
            updates.append("notes = notes || ? || char(10)")
            params.append(notes)

        if deliverable_paths is not None:
            updates.append("deliverable_paths = ?")
            params.append(json.dumps(deliverable_paths))

        if target == OrderStatus.DELIVERED:
            updates.append("delivered_at = ?")
            params.append(now)

        if target == OrderStatus.REVISION_REQUESTED:
            updates.append("revision_count = revision_count + 1")

        params.append(db_id)
        sql = f"UPDATE orders SET {', '.join(updates)} WHERE id = ?"
        await self._db.execute(sql, tuple(params))

        log.info(
            "state_transition",
            order_id=order_id,
            from_status=current.value,
            to_status=target.value,
        )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def get_orders_by_status(
        self, *statuses: OrderStatus
    ) -> list[dict]:
        """Return all orders matching any of the given statuses."""
        placeholders = ", ".join("?" for _ in statuses)
        return await self._db.fetch_all(
            f"SELECT * FROM orders WHERE status IN ({placeholders})",
            tuple(s.value for s in statuses),
        )

    async def get_order(self, order_id: str) -> dict | None:
        """Fetch a single order by id or fiverr_order_id."""
        return await self._db.fetch_one(
            "SELECT * FROM orders WHERE id = ? OR fiverr_order_id = ?",
            (order_id, order_id),
        )

    async def build_order_model(self, order_id: str) -> Order | None:
        """Load an order row and return it as a Pydantic ``Order`` model."""
        row = await self.get_order(order_id)
        if row is None:
            return None
        return Order(
            id=row["id"],
            fiverr_order_id=row["fiverr_order_id"],
            gig_type=row["gig_type"],
            status=row["status"],
            requirements=row.get("requirements", []),
            buyer_username=row["buyer_username"],
            price=row["price"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            delivered_at=row.get("delivered_at"),
            deliverable_paths=row.get("deliverable_paths", []),
            revision_count=row.get("revision_count", 0),
            notes=row.get("notes", ""),
        )

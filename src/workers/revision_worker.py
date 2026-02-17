"""Revision worker -- delegates revision requests to the appropriate worker.

``RevisionWorker`` acts as a dispatcher: it receives a revision request for
an existing order, determines which gig-type-specific worker produced the
original deliverable, and delegates the revision to that worker's
:meth:`revise` method.
"""

from __future__ import annotations

from pathlib import Path

from src.ai.client import AIClient
from src.ai.prompts import PromptManager
from src.models.schemas import GigType, Order
from src.utils.file_handler import DeliverableManager
from src.utils.logger import get_logger
from src.workers.base import BaseWorker

log = get_logger(__name__, component="revision_worker")


class RevisionWorker:
    """Dispatch revision requests to the correct gig-type worker.

    Parameters
    ----------
    client:
        An initialised ``AIClient`` (retained for potential direct use).
    prompts:
        A ``PromptManager`` for loading prompt templates.
    file_manager:
        A ``DeliverableManager`` for resolving deliverable file paths.
    workers:
        Mapping from ``GigType`` to the corresponding ``BaseWorker``
        instance that can handle revisions for that type.
    """

    def __init__(
        self,
        client: AIClient,
        prompts: PromptManager,
        file_manager: DeliverableManager,
        workers: dict[GigType, BaseWorker],
    ) -> None:
        self._client = client
        self._prompts = prompts
        self._file_manager = file_manager
        self._workers = workers
        log.info(
            "revision_worker.init",
            registered_types=[gt.value for gt in workers],
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_revision(
        self, order: Order, feedback: str
    ) -> list[Path]:
        """Process a revision request by delegating to the correct worker.

        Parameters
        ----------
        order:
            The order that the revision pertains to.  Its ``gig_type`` field
            determines which worker handles the revision.
        feedback:
            The buyer's revision feedback text.

        Returns
        -------
        list[Path]
            Paths to the revised deliverable file(s).

        Raises
        ------
        ValueError
            If no worker is registered for the order's gig type.
        """
        gig_type = order.gig_type

        log.info(
            "revision_worker.process",
            order_id=order.id,
            gig_type=gig_type.value,
            feedback_length=len(feedback),
        )

        worker = self._workers.get(gig_type)
        if worker is None:
            available = [gt.value for gt in self._workers]
            log.error(
                "revision_worker.no_worker",
                order_id=order.id,
                gig_type=gig_type.value,
                available=available,
            )
            raise ValueError(
                f"No worker registered for gig type '{gig_type.value}'. "
                f"Available types: {available}"
            )

        # Resolve existing deliverable paths from the order metadata or
        # by querying the file manager.
        original_paths: list[Path] = []
        if order.deliverable_paths:
            original_paths = [Path(p) for p in order.deliverable_paths]
        else:
            # Fall back to querying the file manager for any files under
            # this order's directory.
            discovered = self._file_manager.get_deliverables(order.id)
            if discovered:
                original_paths = discovered
                log.info(
                    "revision_worker.discovered_deliverables",
                    order_id=order.id,
                    count=len(discovered),
                )

        if not original_paths:
            log.warning(
                "revision_worker.no_originals",
                order_id=order.id,
                note="Revision will proceed without original content reference",
            )

        try:
            revised_paths = await worker.revise(order, feedback, original_paths)
            log.info(
                "revision_worker.complete",
                order_id=order.id,
                revised_count=len(revised_paths),
                paths=[str(p) for p in revised_paths],
            )
            return revised_paths
        except Exception:
            log.exception(
                "revision_worker.failed",
                order_id=order.id,
                gig_type=gig_type.value,
            )
            raise

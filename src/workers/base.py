"""Abstract base class for gig-type-specific workers.

Every worker follows the same lifecycle:

1. **analyze** -- parse order-specific details (word count, complexity, etc.)
2. **execute** -- produce the deliverable(s) and write them to disk
3. **revise**  -- apply buyer feedback to existing deliverables

The ``process`` template method orchestrates steps 1 and 2.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from src.ai.client import AIClient
from src.ai.prompts import PromptManager
from src.models.schemas import GigType, Order
from src.utils.file_handler import DeliverableManager
from src.utils.logger import get_logger

log = get_logger(__name__, component="worker_base")


class BaseWorker(ABC):
    """Abstract base for all gig workers.

    Parameters
    ----------
    client:
        An initialised ``AIClient`` for Claude completions.
    prompts:
        A ``PromptManager`` for loading prompt templates.
    file_manager:
        A ``DeliverableManager`` for persisting deliverable files.
    """

    def __init__(
        self,
        client: AIClient,
        prompts: PromptManager,
        file_manager: DeliverableManager,
    ) -> None:
        self._client = client
        self._prompts = prompts
        self._file_manager = file_manager

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def gig_type(self) -> GigType:
        """Return the ``GigType`` this worker handles."""

    @abstractmethod
    async def analyze(self, order: Order) -> dict:
        """Analyze order specifics relevant to this worker type.

        Returns a dict of extracted parameters that ``execute`` and
        ``revise`` will consume.
        """

    @abstractmethod
    async def execute(self, order: Order, analysis: dict) -> list[Path]:
        """Generate the deliverable(s) and return their file paths."""

    @abstractmethod
    async def revise(
        self, order: Order, feedback: str, original_paths: list[Path]
    ) -> list[Path]:
        """Revise existing deliverables based on buyer feedback.

        Parameters
        ----------
        order:
            The original order.
        feedback:
            The buyer's revision request text.
        original_paths:
            Paths to the current version of each deliverable.

        Returns
        -------
        list[Path]
            Paths to the revised deliverables.
        """

    # ------------------------------------------------------------------
    # Template method
    # ------------------------------------------------------------------

    async def process(self, order: Order) -> list[Path]:
        """Analyze the order and produce deliverables (template method).

        This is the main entry point that the orchestrator should call.
        It runs ``analyze`` followed by ``execute`` and returns the list
        of generated file paths.

        Returns
        -------
        list[Path]
            Paths to the generated deliverable files.
        """
        log.info(
            "worker.process_start",
            order_id=order.id,
            gig_type=self.gig_type.value,
            buyer=order.buyer_username,
        )

        try:
            analysis = await self.analyze(order)
            log.info(
                "worker.analysis_complete",
                order_id=order.id,
                analysis_keys=list(analysis.keys()),
            )

            paths = await self.execute(order, analysis)
            log.info(
                "worker.execute_complete",
                order_id=order.id,
                deliverable_count=len(paths),
                paths=[str(p) for p in paths],
            )
            return paths

        except Exception:
            log.exception(
                "worker.process_failed",
                order_id=order.id,
                gig_type=self.gig_type.value,
            )
            raise

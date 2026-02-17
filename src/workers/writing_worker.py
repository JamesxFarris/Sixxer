"""Writing worker -- generates blog posts, articles, and other written content.

``WritingWorker`` handles ``GigType.WRITING`` orders.  It extracts topic,
word-count, tone, and keyword parameters from the order, sends them through
the ``writing_task`` prompt template, and saves the result as a ``.docx``
Word document.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.ai.client import AIClient
from src.ai.prompts import PromptManager
from src.models.schemas import GigType, Order
from src.utils.file_handler import DeliverableManager
from src.utils.logger import get_logger
from src.workers.base import BaseWorker

log = get_logger(__name__, component="writing_worker")


class WritingWorker(BaseWorker):
    """Produce written deliverables (blog posts, articles, etc.)."""

    def __init__(
        self,
        client: AIClient,
        prompts: PromptManager,
        file_manager: DeliverableManager,
    ) -> None:
        super().__init__(client, prompts, file_manager)

    # ------------------------------------------------------------------
    # Abstract property
    # ------------------------------------------------------------------

    @property
    def gig_type(self) -> GigType:
        return GigType.WRITING

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    async def analyze(self, order: Order) -> dict[str, Any]:
        """Extract writing-specific parameters from the order.

        Returns
        -------
        dict
            Keys: ``topic``, ``word_count``, ``tone``, ``keywords``,
            ``additional_instructions``.
        """
        requirements_text = "\n".join(order.requirements)

        system = (
            "You are a writing order analyst. Extract the following fields "
            "from the buyer's requirements and return them as a JSON object:\n"
            '- "topic": main topic or title for the article\n'
            '- "word_count": target word count as an integer (default 500 if not specified)\n'
            '- "tone": writing tone such as "professional", "casual", "academic" '
            '(default "professional" if not specified)\n'
            '- "keywords": list of SEO keywords to include (empty list if none specified)\n'
            '- "additional_instructions": any other specific instructions from the buyer\n\n'
            "Return ONLY valid JSON."
        )
        user = (
            f"Gig Title: {order.fiverr_order_id}\n"
            f"Requirements:\n{requirements_text}"
        )

        log.info("writing_worker.analyze", order_id=order.id)

        try:
            data = await self._client.complete_json(
                system=system,
                user=user,
                purpose="writing_analysis",
            )
        except Exception:
            log.exception("writing_worker.analyze_failed", order_id=order.id)
            # Return sensible defaults so the pipeline can continue.
            data = {}

        return {
            "topic": data.get("topic", requirements_text[:100] or "Article"),
            "word_count": int(data.get("word_count", 500)),
            "tone": data.get("tone", "professional"),
            "keywords": data.get("keywords", []),
            "additional_instructions": data.get("additional_instructions", ""),
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self, order: Order, analysis: dict[str, Any]
    ) -> list[Path]:
        """Generate the article and save it as a Word document.

        Returns
        -------
        list[Path]
            Single-element list with the path to the ``.docx`` file.
        """
        log.info(
            "writing_worker.execute",
            order_id=order.id,
            topic=analysis["topic"],
            word_count=analysis["word_count"],
        )

        system = self._prompts.get_system("writing_task")
        user = self._prompts.get_user(
            "writing_task",
            topic=analysis["topic"],
            word_count=analysis["word_count"],
            tone=analysis["tone"],
            keywords=", ".join(analysis["keywords"]) if analysis["keywords"] else "None specified",
            additional_instructions=analysis["additional_instructions"] or "None",
        )

        # Request enough tokens for the target word count (rough estimate:
        # 1 word ~1.3 tokens, plus headroom for formatting).
        estimated_tokens = int(analysis["word_count"] * 1.5) + 512
        max_tokens = min(max(estimated_tokens, 2048), 8192)

        try:
            article_text = await self._client.complete(
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=0.7,
                purpose="writing_execution",
            )
        except Exception:
            log.exception("writing_worker.execute_failed", order_id=order.id)
            raise

        # Determine a meaningful filename.
        safe_topic = (
            analysis["topic"]
            .replace(" ", "_")
            .replace("/", "_")
            .replace("\\", "_")[:50]
        )
        filename = f"{safe_topic}.docx"

        path = self._file_manager.save_docx(
            order_id=order.id,
            filename=filename,
            content=article_text,
            title=analysis["topic"],
        )

        log.info(
            "writing_worker.deliverable_saved",
            order_id=order.id,
            path=str(path),
        )
        return [path]

    # ------------------------------------------------------------------
    # Revision
    # ------------------------------------------------------------------

    async def revise(
        self,
        order: Order,
        feedback: str,
        original_paths: list[Path],
    ) -> list[Path]:
        """Revise the article based on buyer feedback.

        Reads the original ``.docx`` plain-text content, sends it with the
        feedback through the ``revision_task`` prompt, and saves a new file.

        Returns
        -------
        list[Path]
            Single-element list with the path to the revised ``.docx``.
        """
        log.info(
            "writing_worker.revise",
            order_id=order.id,
            feedback_length=len(feedback),
            original_count=len(original_paths),
        )

        # Read the original content from the first deliverable.
        original_content = ""
        if original_paths:
            original_path = original_paths[0]
            try:
                # Read the docx as plain text for the revision prompt.
                from docx import Document as DocxDocument

                doc = DocxDocument(str(original_path))
                paragraphs = [p.text for p in doc.paragraphs]
                original_content = "\n".join(paragraphs)
            except Exception:
                log.warning(
                    "writing_worker.revise_read_failed",
                    path=str(original_path),
                )
                # Fall back to reading as plain text if docx parsing fails.
                try:
                    original_content = original_path.read_text(encoding="utf-8")
                except Exception:
                    original_content = "[Original content could not be read]"

        system = self._prompts.get_system("revision_task")
        user = self._prompts.get_user(
            "revision_task",
            gig_type="writing",
            original_content=original_content,
            revision_feedback=feedback,
            additional_context="\n".join(order.requirements),
        )

        try:
            revised_text = await self._client.complete(
                system=system,
                user=user,
                max_tokens=8192,
                temperature=0.7,
                purpose="writing_revision",
            )
        except Exception:
            log.exception("writing_worker.revise_failed", order_id=order.id)
            raise

        # Save the revised version with a revision suffix.
        revision_num = order.revision_count + 1
        base_name = original_paths[0].stem if original_paths else "article"
        filename = f"{base_name}_rev{revision_num}.docx"

        path = self._file_manager.save_docx(
            order_id=order.id,
            filename=filename,
            content=revised_text,
            title=f"Revised: {base_name}",
        )

        log.info(
            "writing_worker.revision_saved",
            order_id=order.id,
            revision=revision_num,
            path=str(path),
        )
        return [path]

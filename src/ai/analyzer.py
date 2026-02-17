"""AI-powered order analysis and classification.

``OrderAnalyzer`` uses Claude to parse incoming Fiverr order text into
structured ``OrderAnalysis`` objects, classify gig types, and extract
revision feedback.
"""

from __future__ import annotations

from typing import Any

from src.ai.client import AIClient
from src.ai.prompts import PromptManager
from src.models.schemas import GigType, Order, OrderAnalysis
from src.utils.logger import get_logger

log = get_logger(__name__, component="analyzer")


class OrderAnalyzer:
    """Analyze and classify Fiverr orders using Claude.

    Parameters
    ----------
    client:
        An initialised ``AIClient`` for Claude completions.
    prompts:
        A ``PromptManager`` for loading prompt templates.
    """

    def __init__(self, client: AIClient, prompts: PromptManager) -> None:
        self._client = client
        self._prompts = prompts

    # ------------------------------------------------------------------
    # Order analysis
    # ------------------------------------------------------------------

    async def analyze_order(
        self,
        order_text: str,
        gig_title: str,
        package_name: str = "Basic",
        price: float = 0.0,
        buyer_username: str = "buyer",
        attached_files: str = "None",
        order_notes: str = "",
    ) -> OrderAnalysis:
        """Analyse raw order text and return a structured ``OrderAnalysis``.

        The method sends the order details through the ``order_analysis``
        prompt template to Claude, parses the JSON response, and maps it
        onto the ``OrderAnalysis`` Pydantic model.

        Parameters
        ----------
        order_text:
            The buyer's requirements / description text.
        gig_title:
            Title of the Fiverr gig the order belongs to.
        package_name:
            Package tier name (Basic / Standard / Premium).
        price:
            Order price in USD.
        buyer_username:
            Buyer's Fiverr username.
        attached_files:
            Comma-separated list of attached filenames, or ``"None"``.
        order_notes:
            Any additional order notes.

        Returns
        -------
        OrderAnalysis
            Structured analysis result ready for downstream processing.
        """
        system = self._prompts.get_system("order_analysis")
        user = self._prompts.get_user(
            "order_analysis",
            gig_title=gig_title,
            package_name=package_name,
            price=price,
            buyer_username=buyer_username,
            buyer_requirements=order_text,
            attached_files=attached_files,
            order_notes=order_notes,
        )

        log.info(
            "analyzer.analyze_order",
            gig_title=gig_title,
            text_length=len(order_text),
        )

        try:
            data: dict[str, Any] = await self._client.complete_json(
                system=system,
                user=user,
                purpose="order_analysis",
            )
        except Exception:
            log.exception("analyzer.analyze_order_failed", gig_title=gig_title)
            raise

        # Map the AI response onto the OrderAnalysis model, supplying
        # sensible defaults for any fields the model might omit.
        analysis = OrderAnalysis(
            gig_type=self._parse_gig_type(data.get("gig_type", "writing")),
            requirements=data.get("requirements", []),
            word_count=data.get("word_count"),
            row_count=data.get("row_count"),
            script_complexity=data.get("script_complexity"),
            needs_clarification=data.get("needs_clarification", False),
            clarification_questions=data.get("clarification_questions", []),
        )

        log.info(
            "analyzer.analysis_complete",
            gig_type=analysis.gig_type.value,
            requirement_count=len(analysis.requirements),
            needs_clarification=analysis.needs_clarification,
        )
        return analysis

    # ------------------------------------------------------------------
    # Gig-type classification
    # ------------------------------------------------------------------

    async def classify_gig_type(
        self, gig_title: str, requirements: str
    ) -> GigType:
        """Classify which worker type should handle an order.

        Uses a lightweight prompt to determine the appropriate ``GigType``
        based on the gig title and requirements text.

        Parameters
        ----------
        gig_title:
            Title of the Fiverr gig.
        requirements:
            Buyer-supplied requirements as plain text.

        Returns
        -------
        GigType
            The classified gig type (``WRITING``, ``CODING``, or ``DATA_ENTRY``).
        """
        system = (
            "You are a Fiverr order classifier. Given the gig title and "
            "buyer requirements, determine which category this order falls "
            "into. Respond with ONLY one of these values: writing, coding, "
            "data_entry. No explanation, just the category."
        )
        user = (
            f"Gig Title: {gig_title}\n"
            f"Buyer Requirements: {requirements}"
        )

        log.info("analyzer.classify_gig_type", gig_title=gig_title)

        try:
            raw = await self._client.complete(
                system=system,
                user=user,
                max_tokens=20,
                temperature=0.0,
                purpose="gig_classification",
            )
            return self._parse_gig_type(raw.strip().lower())
        except Exception:
            log.exception("analyzer.classify_gig_type_failed")
            raise

    # ------------------------------------------------------------------
    # Revision feedback extraction
    # ------------------------------------------------------------------

    async def extract_revision_feedback(
        self, revision_text: str, original_requirements: str
    ) -> dict[str, Any]:
        """Parse a buyer's revision request into structured feedback.

        Returns
        -------
        dict
            Keys:

            - ``specific_changes`` (list[str]): individual changes requested
            - ``tone`` (str): detected tone of the feedback (e.g. "polite",
              "frustrated", "neutral")
            - ``urgency`` (str): one of "low", "medium", "high"
        """
        system = (
            "You are an expert at understanding revision feedback. Analyze "
            "the buyer's revision request and return a JSON object with:\n"
            '- "specific_changes": a list of individual changes requested\n'
            '- "tone": the detected tone of the feedback '
            '(e.g. "polite", "frustrated", "neutral")\n'
            '- "urgency": one of "low", "medium", "high"\n\n'
            "Return ONLY valid JSON."
        )
        user = (
            f"Original requirements:\n{original_requirements}\n\n"
            f"Revision feedback from buyer:\n{revision_text}"
        )

        log.info(
            "analyzer.extract_revision_feedback",
            feedback_length=len(revision_text),
        )

        try:
            data = await self._client.complete_json(
                system=system,
                user=user,
                purpose="revision_analysis",
            )
        except Exception:
            log.exception("analyzer.extract_revision_feedback_failed")
            raise

        # Ensure the expected keys exist with sensible defaults.
        result: dict[str, Any] = {
            "specific_changes": data.get("specific_changes", []),
            "tone": data.get("tone", "neutral"),
            "urgency": data.get("urgency", "medium"),
        }

        log.info(
            "analyzer.revision_feedback_extracted",
            change_count=len(result["specific_changes"]),
            tone=result["tone"],
            urgency=result["urgency"],
        )
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_gig_type(value: str) -> GigType:
        """Coerce a string into a ``GigType`` enum member.

        Falls back to ``GigType.WRITING`` for unrecognised values so that
        the pipeline always has a valid type to work with.
        """
        value = value.strip().lower().replace("-", "_").replace(" ", "_")
        try:
            return GigType(value)
        except ValueError:
            log.warning(
                "analyzer.unknown_gig_type",
                raw_value=value,
                fallback="writing",
            )
            return GigType.WRITING

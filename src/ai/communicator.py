"""AI-powered buyer communication message generator.

``BuyerCommunicator`` generates professional, contextually appropriate
messages for every stage of the Fiverr order lifecycle -- from initial
acknowledgment through delivery and revisions.
"""

from __future__ import annotations

from src.ai.client import AIClient
from src.ai.prompts import PromptManager
from src.utils.logger import get_logger

log = get_logger(__name__, component="communicator")


class BuyerCommunicator:
    """Generate buyer-facing messages via Claude.

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
    # Acknowledgment
    # ------------------------------------------------------------------

    async def generate_acknowledgment(
        self,
        buyer_name: str,
        order_summary: str,
        gig_type: str = "general",
        package_name: str = "Basic",
        delivery_deadline: str = "as specified",
    ) -> str:
        """Generate a friendly professional message acknowledging a new order.

        Parameters
        ----------
        buyer_name:
            The buyer's display name or username.
        order_summary:
            A brief summary of the order requirements.
        gig_type:
            Type of gig (e.g. "writing", "coding", "data_entry").
        package_name:
            Package tier name.
        delivery_deadline:
            Human-readable delivery deadline string.

        Returns
        -------
        str
            The acknowledgment message ready to send.
        """
        log.info(
            "communicator.generate_acknowledgment",
            buyer=buyer_name,
            gig_type=gig_type,
        )

        try:
            system = self._prompts.get_system("buyer_acknowledgment")
            user = self._prompts.get_user(
                "buyer_acknowledgment",
                buyer_name=buyer_name,
                gig_type=gig_type,
                package_name=package_name,
                requirements_summary=order_summary,
                delivery_deadline=delivery_deadline,
            )
            message = await self._client.complete(
                system=system,
                user=user,
                max_tokens=512,
                temperature=0.7,
                purpose="buyer_acknowledgment",
            )
            return message.strip()
        except Exception:
            log.exception("communicator.acknowledgment_failed", buyer=buyer_name)
            raise

    # ------------------------------------------------------------------
    # Clarification
    # ------------------------------------------------------------------

    async def generate_clarification(
        self,
        buyer_name: str,
        questions: list[str],
        gig_type: str = "general",
        current_requirements: str = "",
    ) -> str:
        """Generate a polite message requesting clarification from the buyer.

        Parameters
        ----------
        buyer_name:
            The buyer's display name.
        questions:
            List of specific questions to ask.
        gig_type:
            Type of gig for context.
        current_requirements:
            What we already know about the order.

        Returns
        -------
        str
            The clarification request message.
        """
        log.info(
            "communicator.generate_clarification",
            buyer=buyer_name,
            question_count=len(questions),
        )

        formatted_questions = "\n".join(
            f"  {i}. {q}" for i, q in enumerate(questions, 1)
        )

        try:
            system = self._prompts.get_system("buyer_clarification")
            user = self._prompts.get_user(
                "buyer_clarification",
                buyer_name=buyer_name,
                gig_type=gig_type,
                current_requirements=current_requirements,
                clarification_questions=formatted_questions,
            )
            message = await self._client.complete(
                system=system,
                user=user,
                max_tokens=512,
                temperature=0.7,
                purpose="buyer_clarification",
            )
            return message.strip()
        except Exception:
            log.exception("communicator.clarification_failed", buyer=buyer_name)
            raise

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def generate_delivery_message(
        self,
        buyer_name: str,
        gig_type: str,
        summary: str,
        files_delivered: str = "",
        special_notes: str = "",
    ) -> str:
        """Generate a professional delivery message describing completed work.

        Parameters
        ----------
        buyer_name:
            The buyer's display name.
        gig_type:
            Type of gig delivered (e.g. "writing", "coding", "data_entry").
        summary:
            Summary of the deliverable content and key features.
        files_delivered:
            Comma-separated list of delivered file names.
        special_notes:
            Any additional notes for the buyer.

        Returns
        -------
        str
            The delivery message ready to send.
        """
        log.info(
            "communicator.generate_delivery_message",
            buyer=buyer_name,
            gig_type=gig_type,
        )

        try:
            system = self._prompts.get_system("buyer_delivery")
            user = self._prompts.get_user(
                "buyer_delivery",
                buyer_name=buyer_name,
                gig_type=gig_type,
                deliverable_summary=summary,
                files_delivered=files_delivered or "See attached files",
                special_notes=special_notes or "None",
            )
            message = await self._client.complete(
                system=system,
                user=user,
                max_tokens=512,
                temperature=0.7,
                purpose="buyer_delivery",
            )
            return message.strip()
        except Exception:
            log.exception("communicator.delivery_message_failed", buyer=buyer_name)
            raise

    # ------------------------------------------------------------------
    # Revision response
    # ------------------------------------------------------------------

    async def generate_revision_response(
        self, buyer_name: str, changes_summary: str
    ) -> str:
        """Generate a message acknowledging a revision and describing changes.

        Parameters
        ----------
        buyer_name:
            The buyer's display name.
        changes_summary:
            Summary of the changes that were made.

        Returns
        -------
        str
            The revision response message.
        """
        log.info(
            "communicator.generate_revision_response",
            buyer=buyer_name,
        )

        system = (
            "You are a professional Fiverr freelancer responding to a revision "
            "request. Write a brief, professional message acknowledging the "
            "buyer's feedback, summarizing the changes you have made, and "
            "inviting them to review the updated deliverable. Keep it concise "
            "(2-4 sentences). Be confident and positive."
        )
        user = (
            f"Buyer name: {buyer_name}\n"
            f"Changes made based on feedback:\n{changes_summary}\n\n"
            "Write the revision delivery message."
        )

        try:
            message = await self._client.complete(
                system=system,
                user=user,
                max_tokens=512,
                temperature=0.7,
                purpose="revision_response",
            )
            return message.strip()
        except Exception:
            log.exception(
                "communicator.revision_response_failed", buyer=buyer_name
            )
            raise

    # ------------------------------------------------------------------
    # Generic reply
    # ------------------------------------------------------------------

    async def generate_generic_reply(
        self,
        buyer_name: str,
        context: str,
        tone: str = "professional",
    ) -> str:
        """Generate a generic reply for miscellaneous buyer messages.

        Parameters
        ----------
        buyer_name:
            The buyer's display name.
        context:
            Context about the conversation and what the buyer said.
        tone:
            Desired tone: ``"professional"``, ``"friendly"``, ``"formal"``.

        Returns
        -------
        str
            The reply message.
        """
        log.info(
            "communicator.generate_generic_reply",
            buyer=buyer_name,
            tone=tone,
        )

        system = (
            f"You are a {tone} Fiverr freelancer replying to a buyer message. "
            "Write a helpful, concise response. Do not use excessive "
            "exclamation marks or emojis. Keep it to 2-4 sentences unless "
            "more detail is genuinely needed."
        )
        user = (
            f"Buyer name: {buyer_name}\n"
            f"Context / buyer message:\n{context}\n\n"
            f"Write a {tone} reply."
        )

        try:
            message = await self._client.complete(
                system=system,
                user=user,
                max_tokens=512,
                temperature=0.7,
                purpose="generic_reply",
            )
            return message.strip()
        except Exception:
            log.exception("communicator.generic_reply_failed", buyer=buyer_name)
            raise

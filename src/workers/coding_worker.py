"""Coding worker -- generates Python scripts and programs.

``CodingWorker`` handles ``GigType.CODING`` orders.  It extracts the
description, requirements, and complexity level from the order, sends
them through the ``coding_task`` prompt template, validates the syntax
of the generated code, and saves it as a ``.py`` file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.ai.client import AIClient
from src.ai.prompts import PromptManager
from src.models.schemas import GigType, Order
from src.utils.file_handler import DeliverableManager
from src.utils.logger import get_logger
from src.workers.base import BaseWorker

log = get_logger(__name__, component="coding_worker")


class CodingWorker(BaseWorker):
    """Produce Python script deliverables."""

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
        return GigType.CODING

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    async def analyze(self, order: Order) -> dict[str, Any]:
        """Extract coding-specific parameters from the order.

        Returns
        -------
        dict
            Keys: ``description``, ``requirements``, ``complexity``,
            ``libraries``, ``additional_instructions``.
        """
        requirements_text = "\n".join(order.requirements)

        system = (
            "You are a coding order analyst. Extract the following fields "
            "from the buyer's requirements and return them as a JSON object:\n"
            '- "description": concise description of what the script should do\n'
            '- "requirements": list of specific functional requirements\n'
            '- "complexity": one of "simple", "moderate", "complex"\n'
            '- "libraries": list of third-party Python libraries likely needed\n'
            '- "additional_instructions": any other specific instructions\n\n'
            "Return ONLY valid JSON."
        )
        user = (
            f"Order ID: {order.fiverr_order_id}\n"
            f"Requirements:\n{requirements_text}"
        )

        log.info("coding_worker.analyze", order_id=order.id)

        try:
            data = await self._client.complete_json(
                system=system,
                user=user,
                purpose="coding_analysis",
            )
        except Exception:
            log.exception("coding_worker.analyze_failed", order_id=order.id)
            data = {}

        return {
            "description": data.get("description", requirements_text[:200] or "Python script"),
            "requirements": data.get("requirements", order.requirements),
            "complexity": data.get("complexity", "moderate"),
            "libraries": data.get("libraries", []),
            "additional_instructions": data.get("additional_instructions", ""),
        }

    # ------------------------------------------------------------------
    # Code extraction helper
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_python_code(raw_response: str) -> str:
        """Extract Python source code from a model response.

        The model may wrap the code in markdown fences (````python ... ````).
        This method strips those fences.  If no fences are found, the raw
        response is returned as-is (it may already be bare code).
        """
        # Match ```python ... ``` or ``` ... ```
        pattern = r"```(?:python)?\s*\n(.*?)```"
        match = re.search(pattern, raw_response, re.DOTALL)
        if match:
            return match.group(1).strip()
        return raw_response.strip()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self, order: Order, analysis: dict[str, Any]
    ) -> list[Path]:
        """Generate a Python script and save it after syntax validation.

        If the first attempt produces code with a syntax error, the worker
        retries once by sending the error back to Claude for correction.

        Returns
        -------
        list[Path]
            Single-element list with the path to the ``.py`` file.
        """
        log.info(
            "coding_worker.execute",
            order_id=order.id,
            complexity=analysis["complexity"],
        )

        system = self._prompts.get_system("coding_task")

        formatted_requirements = "\n".join(
            f"  - {r}" for r in analysis["requirements"]
        )
        user = self._prompts.get_user(
            "coding_task",
            description=analysis["description"],
            requirements=formatted_requirements,
            complexity=analysis["complexity"],
            additional_instructions=analysis["additional_instructions"] or "None",
        )

        max_tokens = 4096 if analysis["complexity"] != "complex" else 8192

        try:
            raw_response = await self._client.complete(
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=0.3,
                purpose="coding_execution",
            )
        except Exception:
            log.exception("coding_worker.execute_api_failed", order_id=order.id)
            raise

        code = self._extract_python_code(raw_response)

        # Validate syntax.  If it fails, retry once with the error message.
        try:
            compile(code, "<generated>", "exec")
        except SyntaxError as first_error:
            log.warning(
                "coding_worker.syntax_error_retry",
                order_id=order.id,
                error=str(first_error),
            )
            code = await self._retry_with_syntax_fix(
                code, first_error, system, analysis, max_tokens
            )

        # Determine a meaningful filename.
        safe_desc = (
            analysis["description"]
            .split(".")[0]
            .replace(" ", "_")
            .replace("/", "_")
            .replace("\\", "_")
            .lower()[:40]
        )
        # Remove non-alphanumeric characters except underscores.
        safe_desc = re.sub(r"[^a-z0-9_]", "", safe_desc) or "script"
        filename = f"{safe_desc}.py"

        try:
            path = self._file_manager.save_python(
                order_id=order.id,
                filename=filename,
                code=code,
            )
        except SyntaxError:
            # save_python validates syntax too; if it still fails after our
            # retry, save as plain text so the deliverable is not lost.
            log.error(
                "coding_worker.syntax_error_persisted",
                order_id=order.id,
            )
            path = self._file_manager.save_text(
                order_id=order.id,
                filename=filename,
                content=code,
            )

        log.info(
            "coding_worker.deliverable_saved",
            order_id=order.id,
            path=str(path),
        )
        return [path]

    async def _retry_with_syntax_fix(
        self,
        broken_code: str,
        error: SyntaxError,
        original_system: str,
        analysis: dict[str, Any],
        max_tokens: int,
    ) -> str:
        """Ask Claude to fix a syntax error in the generated code.

        Returns the corrected code (which may still have issues -- the caller
        should handle that gracefully).
        """
        fix_user = (
            "The following Python code has a syntax error. Please fix it and "
            "return ONLY the corrected Python code with no explanation.\n\n"
            f"Error: {error}\n"
            f"Line {error.lineno}: {error.text}\n\n"
            f"Code:\n```python\n{broken_code}\n```"
        )

        try:
            raw_fix = await self._client.complete(
                system=original_system,
                user=fix_user,
                max_tokens=max_tokens,
                temperature=0.0,
                purpose="coding_syntax_fix",
            )
            fixed_code = self._extract_python_code(raw_fix)
            compile(fixed_code, "<generated_fix>", "exec")
            log.info("coding_worker.syntax_fix_success")
            return fixed_code
        except SyntaxError:
            log.warning("coding_worker.syntax_fix_still_broken")
            return self._extract_python_code(raw_fix)  # type: ignore[possibly-undefined]
        except Exception:
            log.exception("coding_worker.syntax_fix_api_failed")
            return broken_code

    # ------------------------------------------------------------------
    # Revision
    # ------------------------------------------------------------------

    async def revise(
        self,
        order: Order,
        feedback: str,
        original_paths: list[Path],
    ) -> list[Path]:
        """Revise the Python script based on buyer feedback.

        Reads the original code, sends it with the feedback through the
        ``revision_task`` prompt, validates the output, and saves a new file.

        Returns
        -------
        list[Path]
            Single-element list with the path to the revised ``.py`` file.
        """
        log.info(
            "coding_worker.revise",
            order_id=order.id,
            feedback_length=len(feedback),
        )

        # Read the original code.
        original_code = ""
        if original_paths:
            try:
                original_code = original_paths[0].read_text(encoding="utf-8")
            except Exception:
                log.warning(
                    "coding_worker.revise_read_failed",
                    path=str(original_paths[0]),
                )
                original_code = "[Original code could not be read]"

        system = self._prompts.get_system("revision_task")
        user = self._prompts.get_user(
            "revision_task",
            gig_type="coding",
            original_content=original_code,
            revision_feedback=feedback,
            additional_context="\n".join(order.requirements),
        )

        try:
            raw_response = await self._client.complete(
                system=system,
                user=user,
                max_tokens=8192,
                temperature=0.3,
                purpose="coding_revision",
            )
        except Exception:
            log.exception("coding_worker.revise_api_failed", order_id=order.id)
            raise

        revised_code = self._extract_python_code(raw_response)

        # Validate syntax.
        try:
            compile(revised_code, "<revised>", "exec")
        except SyntaxError as err:
            log.warning(
                "coding_worker.revision_syntax_error",
                order_id=order.id,
                error=str(err),
            )
            # Attempt a single fix.
            revised_code = await self._retry_with_syntax_fix(
                revised_code,
                err,
                system,
                {"complexity": "moderate"},
                8192,
            )

        # Save revised file.
        revision_num = order.revision_count + 1
        base_name = original_paths[0].stem if original_paths else "script"
        filename = f"{base_name}_rev{revision_num}.py"

        try:
            path = self._file_manager.save_python(
                order_id=order.id,
                filename=filename,
                code=revised_code,
            )
        except SyntaxError:
            log.error(
                "coding_worker.revision_syntax_error_persisted",
                order_id=order.id,
            )
            path = self._file_manager.save_text(
                order_id=order.id,
                filename=filename,
                content=revised_code,
            )

        log.info(
            "coding_worker.revision_saved",
            order_id=order.id,
            revision=revision_num,
            path=str(path),
        )
        return [path]

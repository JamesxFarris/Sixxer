"""YAML-backed prompt template loader.

``PromptManager`` reads templates from a YAML configuration file and exposes
them with simple placeholder substitution.  Templates are expected to live
under a top-level ``prompts`` key, each containing ``system`` and ``user``
sub-keys.

Usage::

    from src.ai.prompts import PromptManager

    pm = PromptManager()
    system = pm.get_system("order_analysis")
    user   = pm.get_user("order_analysis", gig_title="Blog post", ...)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.utils.logger import get_logger

log = get_logger(__name__, component="prompts")

# Resolve paths relative to the project root so imports work from any cwd.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class PromptManager:
    """Load prompt templates from a YAML file and render them with kwargs.

    Parameters
    ----------
    prompts_path:
        Path to the YAML file.  Relative paths are resolved against the
        project root.
    """

    def __init__(self, prompts_path: str = "config/prompts.yaml") -> None:
        path = Path(prompts_path)
        if not path.is_absolute():
            path = _PROJECT_ROOT / path
        self._path = path
        self._templates: dict[str, dict[str, str]] = self._load(path)
        log.info(
            "prompts.loaded",
            path=str(path),
            template_count=len(self._templates),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load(path: Path) -> dict[str, dict[str, str]]:
        """Read and parse the YAML file, returning the ``prompts`` mapping."""
        if not path.is_file():
            raise FileNotFoundError(f"Prompts file not found: {path}")
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict) or "prompts" not in data:
            raise ValueError(
                f"Prompts file must contain a top-level 'prompts' key: {path}"
            )
        return data["prompts"]

    def _get_template(self, template_name: str) -> dict[str, str]:
        """Return the raw template dict, raising on missing names."""
        if template_name not in self._templates:
            available = ", ".join(sorted(self._templates))
            raise KeyError(
                f"Unknown prompt template '{template_name}'. "
                f"Available templates: {available}"
            )
        return self._templates[template_name]

    @staticmethod
    def _render(template: str, kwargs: dict[str, Any]) -> str:
        """Substitute ``{placeholders}`` in *template* with *kwargs*.

        Uses ``str.format_map`` with a default-dict wrapper so that
        missing placeholders are left as-is rather than raising.  This is
        intentional -- templates may contain placeholders that are only
        relevant in certain contexts.
        """

        class _DefaultDict(dict):  # type: ignore[type-arg]
            """dict subclass that returns the key wrapped in braces for misses."""

            def __missing__(self, key: str) -> str:
                return "{" + key + "}"

        return template.format_map(_DefaultDict(**kwargs))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, template_name: str, **kwargs: Any) -> str:
        """Return the full template (system + user) with placeholders filled.

        This concatenates the system and user portions separated by a blank
        line.  For finer control use :meth:`get_system` / :meth:`get_user`.

        Raises
        ------
        KeyError
            If *template_name* does not exist.
        """
        template = self._get_template(template_name)
        parts: list[str] = []
        if "system" in template:
            parts.append(self._render(template["system"], kwargs).strip())
        if "user" in template:
            parts.append(self._render(template["user"], kwargs).strip())
        return "\n\n".join(parts)

    def get_system(self, template_name: str) -> str:
        """Return the system prompt portion of *template_name*.

        No placeholder substitution is performed on the system prompt
        because it typically contains only static instructions.

        Raises
        ------
        KeyError
            If the template or its ``system`` key does not exist.
        """
        template = self._get_template(template_name)
        if "system" not in template:
            raise KeyError(
                f"Template '{template_name}' does not have a 'system' key"
            )
        return template["system"].strip()

    def get_user(self, template_name: str, **kwargs: Any) -> str:
        """Return the user prompt portion with placeholders substituted.

        Parameters
        ----------
        template_name:
            Name of the prompt template.
        **kwargs:
            Values to substitute for ``{placeholders}`` in the user template.

        Raises
        ------
        KeyError
            If the template or its ``user`` key does not exist.
        """
        template = self._get_template(template_name)
        if "user" not in template:
            raise KeyError(
                f"Template '{template_name}' does not have a 'user' key"
            )
        return self._render(template["user"], kwargs).strip()

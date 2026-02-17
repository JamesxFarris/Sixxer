"""YAML-based selector store with fallback resolution.

Loads CSS / XPath selectors from a YAML configuration file and provides
lookup methods that try primary selectors first, then fall back through
alternatives until one matches on the live page.

The YAML schema is::

    selectors:
      <page>:
        <element>:
          primary: "<css-selector>"
          fallback: "<sel1>, <sel2>, ..."

Both ``primary`` and ``fallback`` values may contain comma-separated
selectors; each segment is treated as an independent candidate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from playwright.async_api import Page

from src.utils.logger import get_logger

log = get_logger(__name__, component="selectors")


class SelectorStore:
    """Read-only store backed by a YAML selector map.

    Parameters
    ----------
    yaml_path:
        Path to the YAML file.  Relative paths are resolved from the
        current working directory.
    """

    def __init__(self, yaml_path: str = "config/selectors.yaml") -> None:
        self._path = Path(yaml_path)
        self._data: dict[str, dict[str, dict[str, str]]] = {}
        self._load()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Parse the YAML file into memory."""
        if not self._path.is_file():
            log.warning("selectors_file_not_found", path=str(self._path))
            return

        with open(self._path, encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}

        # Support both top-level ``selectors:`` wrapper and flat layout.
        if "selectors" in raw and isinstance(raw["selectors"], dict):
            self._data = raw["selectors"]
        else:
            self._data = raw

        log.info(
            "selectors_loaded",
            path=str(self._path),
            pages=list(self._data.keys()),
        )

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def get(self, page: str, element: str) -> str:
        """Return the primary selector for *page*/*element*.

        Raises ``KeyError`` if the page or element is not defined.
        """
        entry = self._data[page][element]
        if isinstance(entry, dict):
            return entry["primary"]
        # If the entry is a plain string (non-dict), return it directly.
        return str(entry)

    def get_all(self, page: str, element: str) -> list[str]:
        """Return a list of all selectors (primary first, then fallbacks).

        Each comma-separated segment inside ``primary`` and ``fallback``
        values is split into its own list entry.
        """
        entry = self._data[page][element]

        selectors: list[str] = []
        if isinstance(entry, dict):
            # Parse primary
            primary_raw = entry.get("primary", "")
            selectors.extend(
                s.strip() for s in primary_raw.split(",") if s.strip()
            )
            # Parse fallback
            fallback_raw = entry.get("fallback", "")
            selectors.extend(
                s.strip() for s in fallback_raw.split(",") if s.strip()
            )
        elif isinstance(entry, list):
            selectors = [str(s).strip() for s in entry if str(s).strip()]
        else:
            selectors = [str(entry).strip()]

        return selectors

    async def find(
        self,
        browser_page: Page,
        page: str,
        element: str,
        timeout: int = 10_000,
    ) -> str | None:
        """Try each selector in order and return the first one that matches.

        Parameters
        ----------
        browser_page:
            The live Playwright ``Page`` to test selectors against.
        page:
            The YAML page key (e.g. ``"login"``).
        element:
            The element key within the page (e.g. ``"username_input"``).
        timeout:
            Per-selector wait timeout in milliseconds.

        Returns
        -------
        str | None
            The first selector that matched, or ``None`` if none did.
        """
        candidates = self.get_all(page, element)
        per_selector_timeout = max(timeout // max(len(candidates), 1), 1_000)

        for selector in candidates:
            try:
                el = await browser_page.wait_for_selector(
                    selector, timeout=per_selector_timeout
                )
                if el is not None:
                    log.debug(
                        "selector_found",
                        page=page,
                        element=element,
                        selector=selector,
                    )
                    return selector
            except Exception:
                log.debug(
                    "selector_miss",
                    page=page,
                    element=element,
                    selector=selector,
                )
                continue

        log.warning(
            "selector_not_found",
            page=page,
            element=element,
            candidates=candidates,
        )
        return None

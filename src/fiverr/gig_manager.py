"""Gig creation and management from YAML templates.

The ``GigManager`` drives Fiverr's multi-step gig creation wizard,
filling in title, category, description, pricing, tags, and delivery
settings from structured template dictionaries.  It also provides
listing and status-toggling for existing gigs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.browser.anti_detect import human_click
from src.browser.engine import BrowserEngine
from src.browser.selectors import SelectorStore
from src.fiverr.navigation import Navigator
from src.models.database import Database
from src.utils.human_timing import between_actions, human_delay, page_load_wait
from src.utils.logger import get_logger

log = get_logger(__name__, component="gig_manager")


class GigManager:
    """Create, list, and manage Fiverr gigs.

    Parameters
    ----------
    engine:
        An already-started ``BrowserEngine``.
    selectors:
        The project-wide ``SelectorStore``.
    navigator:
        A configured ``Navigator`` instance.
    db:
        A connected ``Database`` for gig tracking.
    """

    def __init__(
        self,
        engine: BrowserEngine,
        selectors: SelectorStore,
        navigator: Navigator,
        db: Database,
    ) -> None:
        self._engine = engine
        self._selectors = selectors
        self._navigator = navigator
        self._db = db

    # ------------------------------------------------------------------
    # Gig creation
    # ------------------------------------------------------------------

    async def create_gig(self, template: dict[str, Any]) -> str | None:
        """Create a single gig from a template dictionary.

        The template is expected to have keys matching the structure in
        ``config/gig_templates.yaml`` (``title``, ``category``,
        ``subcategory``, ``description``, ``tags``, ``packages``).

        Parameters
        ----------
        template:
            A gig template dictionary.

        Returns
        -------
        str | None
            The Fiverr gig ID on success, or ``None`` on failure.
        """
        title = template.get("title", "")
        if not title:
            log.error("gig_template_missing_title")
            return None

        log.info("creating_gig", title=title)

        try:
            await self._navigator.goto_gig_creation()
            page = await self._engine.get_page()

            # Step 1: Title
            await self._fill_title(page, title)

            # Step 2: Category / Subcategory
            await self._select_category(
                page,
                template.get("category", ""),
                template.get("subcategory", ""),
            )

            # Step 3: Tags
            await self._add_tags(page, template.get("tags", []))

            # Save and continue to next step
            await self._click_save_and_continue(page)

            # Step 4: Pricing / Packages
            await self._fill_packages(page, template.get("packages", {}))

            # Save and continue
            await self._click_save_and_continue(page)

            # Step 5: Description
            await self._fill_description(page, template.get("description", ""))

            # Save and continue
            await self._click_save_and_continue(page)

            # Step 6: Publish
            gig_id = await self._publish_gig(page)

            if gig_id:
                await self._save_gig_to_db(template, gig_id)
                log.info("gig_created", gig_id=gig_id, title=title)
            else:
                log.warning("gig_creation_uncertain", title=title)

            return gig_id

        except Exception:
            log.error("gig_creation_failed", title=title, exc_info=True)
            await self._engine.screenshot("gig_creation_error")
            return None

    # ------------------------------------------------------------------
    # Batch creation
    # ------------------------------------------------------------------

    async def create_all_gigs(
        self, templates_path: str = "config/gig_templates.yaml"
    ) -> list[str]:
        """Load all templates from a YAML file and create each gig.

        Parameters
        ----------
        templates_path:
            Path to the YAML file containing gig templates.

        Returns
        -------
        list[str]
            Fiverr gig IDs for successfully created gigs.
        """
        path = Path(templates_path)
        if not path.is_file():
            log.error("templates_file_not_found", path=str(path))
            return []

        with open(path, encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}

        # Support both top-level ``gigs:`` wrapper and flat layout
        gig_templates = raw.get("gigs", raw)
        if not isinstance(gig_templates, dict):
            log.error("invalid_templates_structure", path=str(path))
            return []

        created_ids: list[str] = []

        for gig_key, template in gig_templates.items():
            if not isinstance(template, dict):
                log.warning("skipping_non_dict_template", key=gig_key)
                continue

            log.info("creating_gig_from_template", key=gig_key)
            gig_id = await self.create_gig(template)

            if gig_id is not None:
                created_ids.append(gig_id)
            else:
                log.warning("gig_creation_skipped", key=gig_key)

            # Pause between gig creations to avoid rate-limiting
            await asyncio.sleep(human_delay(3.0, 6.0))

        log.info(
            "batch_gig_creation_complete",
            total=len(gig_templates),
            created=len(created_ids),
        )
        return created_ids

    # ------------------------------------------------------------------
    # Gig listing
    # ------------------------------------------------------------------

    async def get_my_gigs(self) -> list[dict[str, str]]:
        """Scrape the seller's gig management page.

        Returns
        -------
        list[dict]
            Each dict has keys: ``title``, ``status``, ``url``,
            ``impressions``, ``clicks``, ``orders``.
        """
        await self._engine.navigate("https://www.fiverr.com/users/manage_gigs")
        await self._navigator.wait_for_page_ready()

        page = await self._engine.get_page()
        gigs: list[dict[str, str]] = []

        # Try multiple selectors for gig cards
        gig_card_selectors = [
            ".gig-card",
            ".gig-row",
            "[data-testid='gig-item']",
            ".manage-gig-item",
            "tr.gig-item",
            ".gig-listing",
        ]

        elements: list = []
        for selector in gig_card_selectors:
            elements = await page.query_selector_all(selector)
            if elements:
                break

        for el in elements:
            try:
                gig: dict[str, str] = {
                    "title": "",
                    "status": "",
                    "url": "",
                    "impressions": "0",
                    "clicks": "0",
                    "orders": "0",
                }

                # Title
                for sel in ("h3", ".gig-title", "a.title", "h4",
                            "[data-testid='gig-title']"):
                    title_el = await el.query_selector(sel)
                    if title_el is not None:
                        text = await title_el.text_content()
                        if text and text.strip():
                            gig["title"] = text.strip()
                            break

                # URL
                link_el = await el.query_selector("a[href*='/gigs/']")
                if link_el is None:
                    link_el = await el.query_selector("a[href]")
                if link_el is not None:
                    href = await link_el.get_attribute("href") or ""
                    if href:
                        if href.startswith("/"):
                            href = f"https://www.fiverr.com{href}"
                        gig["url"] = href

                # Status
                for sel in (".gig-status", ".status-badge", "span[class*='status']",
                            "[data-testid='gig-status']"):
                    status_el = await el.query_selector(sel)
                    if status_el is not None:
                        text = await status_el.text_content()
                        if text and text.strip():
                            gig["status"] = text.strip().lower()
                            break

                # Analytics: impressions, clicks, orders
                stat_selectors = [
                    (".impressions", "impressions"),
                    (".clicks", "clicks"),
                    (".orders-count", "orders"),
                ]
                for sel, key in stat_selectors:
                    stat_el = await el.query_selector(sel)
                    if stat_el is not None:
                        text = await stat_el.text_content()
                        if text:
                            digits = "".join(
                                ch for ch in text.strip() if ch.isdigit()
                            )
                            if digits:
                                gig[key] = digits

                # Fallback: try generic stat columns
                stat_cells = await el.query_selector_all(
                    "td.stat, .analytics-cell, .stat-value"
                )
                stat_keys = ["impressions", "clicks", "orders"]
                for idx, cell in enumerate(stat_cells):
                    if idx >= len(stat_keys):
                        break
                    text = await cell.text_content()
                    if text:
                        digits = "".join(
                            ch for ch in text.strip() if ch.isdigit()
                        )
                        if digits:
                            gig[stat_keys[idx]] = digits

                if gig["title"]:
                    gigs.append(gig)

            except Exception:
                log.warning("gig_parse_error", exc_info=True)
                continue

        log.info("gigs_listed", count=len(gigs))
        return gigs

    # ------------------------------------------------------------------
    # Status toggling
    # ------------------------------------------------------------------

    async def update_gig_status(self, gig_id: str, active: bool) -> None:
        """Activate or deactivate a gig.

        Parameters
        ----------
        gig_id:
            The Fiverr gig identifier.
        active:
            ``True`` to activate, ``False`` to pause/deactivate.
        """
        url = f"https://www.fiverr.com/manage_gigs/{gig_id}"
        await self._engine.navigate(url)
        await self._navigator.wait_for_page_ready()

        page = await self._engine.get_page()
        target_action = "activate" if active else "pause"

        # Look for a toggle or status-change button
        toggle_selectors = [
            f"button:has-text('{target_action.capitalize()}')",
            f"a:has-text('{target_action.capitalize()}')",
            ".gig-status-toggle",
            "[data-testid='gig-toggle']",
            f"button[data-action='{target_action}']",
        ]

        for selector in toggle_selectors:
            try:
                el = await page.wait_for_selector(selector, timeout=4_000)
                if el is not None:
                    await asyncio.sleep(human_delay(0.3, 0.8))
                    await human_click(page, selector)
                    await asyncio.sleep(between_actions())

                    # Handle confirmation dialog
                    confirm_selectors = [
                        "button:has-text('Confirm')",
                        "button:has-text('Yes')",
                        "button:has-text('OK')",
                    ]
                    for confirm_sel in confirm_selectors:
                        try:
                            confirm_el = await page.wait_for_selector(
                                confirm_sel, timeout=2_000
                            )
                            if confirm_el is not None:
                                await confirm_el.click()
                                await asyncio.sleep(between_actions())
                                break
                        except Exception:
                            continue

                    log.info(
                        "gig_status_updated",
                        gig_id=gig_id,
                        active=active,
                    )

                    # Update in DB
                    status = "active" if active else "paused"
                    await self._db.execute(
                        "UPDATE gigs SET status = ? WHERE fiverr_gig_id = ?",
                        (status, gig_id),
                    )
                    return
            except Exception:
                continue

        log.error(
            "gig_status_toggle_not_found",
            gig_id=gig_id,
            target=target_action,
        )
        await self._engine.screenshot("gig_status_toggle_missing")

    # ------------------------------------------------------------------
    # Internal: wizard step helpers
    # ------------------------------------------------------------------

    async def _fill_title(self, page: object, title: str) -> None:
        """Fill in the gig title input."""
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        selector = await self._selectors.find(
            pw_page, "gig_creation", "title_input"
        )
        if selector is None:
            log.warning("gig_title_input_not_found_using_fallback")
            selector = "input[name='title'], input#title, input[placeholder*='title']"

        await self._engine.type_text(selector, title)
        await asyncio.sleep(between_actions())
        log.debug("gig_title_filled", title=title[:60])

    async def _select_category(
        self, page: object, category: str, subcategory: str
    ) -> None:
        """Select the category and subcategory dropdowns."""
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        # -- Category -------------------------------------------------------
        if category:
            cat_selector = await self._selectors.find(
                pw_page, "gig_creation", "category_select"
            )
            if cat_selector is not None:
                await self._select_option_by_text(pw_page, cat_selector, category)
                await asyncio.sleep(human_delay(1.0, 2.0))
            else:
                log.warning("category_selector_not_found")

        # -- Subcategory ----------------------------------------------------
        if subcategory:
            subcat_selector = await self._selectors.find(
                pw_page, "gig_creation", "subcategory_select"
            )
            if subcat_selector is not None:
                await self._select_option_by_text(
                    pw_page, subcat_selector, subcategory
                )
                await asyncio.sleep(between_actions())
            else:
                log.warning("subcategory_selector_not_found")

    async def _add_tags(self, page: object, tags: list[str]) -> None:
        """Add tags to the gig."""
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        if not tags:
            return

        tag_selector = await self._selectors.find(
            pw_page, "gig_creation", "tags_input"
        )
        if tag_selector is None:
            log.warning("tags_input_not_found")
            return

        for tag in tags:
            try:
                await self._engine.type_text(tag_selector, tag, clear_first=True)
                await asyncio.sleep(human_delay(0.3, 0.8))
                # Press Enter or comma to confirm the tag
                await pw_page.keyboard.press("Enter")
                await asyncio.sleep(human_delay(0.3, 0.6))
                log.debug("tag_added", tag=tag)
            except Exception:
                log.warning("tag_add_failed", tag=tag, exc_info=True)

    async def _fill_packages(
        self, page: object, packages: dict[str, Any]
    ) -> None:
        """Fill in package/pricing information.

        The ``packages`` dict has keys ``basic``, ``standard``, ``premium``,
        each containing ``name``, ``price``, ``description``,
        ``delivery_days``, etc.
        """
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        if not packages:
            return

        # Fiverr's pricing page typically has columns for each tier.
        # We try both column-index-based and name-based approaches.
        tier_order = ["basic", "standard", "premium"]

        for idx, tier_key in enumerate(tier_order):
            tier_data = packages.get(tier_key)
            if tier_data is None:
                continue

            column_idx = idx + 1  # 1-indexed columns

            # -- Package name -----------------------------------------------
            name = tier_data.get("name", "")
            if name:
                name_selectors = [
                    f".package-col:nth-child({column_idx}) input[name*='name']",
                    f"input[name*='{tier_key}'][name*='name']",
                    f".tier-{tier_key} input.package-name",
                    f"[data-tier='{tier_key}'] input.name",
                ]
                for sel in name_selectors:
                    try:
                        el = await pw_page.wait_for_selector(sel, timeout=2_000)
                        if el is not None:
                            await self._engine.type_text(sel, name)
                            break
                    except Exception:
                        continue

            # -- Package description ----------------------------------------
            desc = tier_data.get("description", "")
            if desc:
                desc_selectors = [
                    f".package-col:nth-child({column_idx}) textarea",
                    f"textarea[name*='{tier_key}'][name*='desc']",
                    f".tier-{tier_key} textarea",
                ]
                for sel in desc_selectors:
                    try:
                        el = await pw_page.wait_for_selector(sel, timeout=2_000)
                        if el is not None:
                            await self._engine.type_text(sel, desc)
                            break
                    except Exception:
                        continue

            # -- Price ------------------------------------------------------
            price = str(tier_data.get("price", ""))
            if price:
                price_selectors = [
                    f".package-col:nth-child({column_idx}) input[name*='price']",
                    f"input[name*='{tier_key}'][name*='price']",
                    f".tier-{tier_key} input.price",
                ]

                # Also try the YAML-configured price input
                try:
                    yaml_sel = self._selectors.get("gig_creation", "price_input")
                    price_selectors.insert(0, yaml_sel)
                except KeyError:
                    pass

                for sel in price_selectors:
                    try:
                        el = await pw_page.wait_for_selector(sel, timeout=2_000)
                        if el is not None:
                            await self._engine.type_text(sel, price)
                            break
                    except Exception:
                        continue

            # -- Delivery days ----------------------------------------------
            days = str(tier_data.get("delivery_days", ""))
            if days:
                days_selectors = [
                    f".package-col:nth-child({column_idx}) select[name*='delivery']",
                    f"select[name*='{tier_key}'][name*='delivery']",
                    f".tier-{tier_key} select.delivery-time",
                ]

                try:
                    yaml_sel = self._selectors.get(
                        "gig_creation", "delivery_days"
                    )
                    days_selectors.insert(0, yaml_sel)
                except KeyError:
                    pass

                for sel in days_selectors:
                    try:
                        el = await pw_page.wait_for_selector(sel, timeout=2_000)
                        if el is not None:
                            tag = await el.evaluate(
                                "el => el.tagName.toLowerCase()"
                            )
                            if tag == "select":
                                await el.select_option(value=days)
                            else:
                                await self._engine.type_text(sel, days)
                            break
                    except Exception:
                        continue

            # -- Revisions --------------------------------------------------
            revisions = tier_data.get("revisions")
            if revisions is not None:
                rev_str = "Unlimited" if revisions == -1 else str(revisions)
                rev_selectors = [
                    f".package-col:nth-child({column_idx}) select[name*='revision']",
                    f"select[name*='{tier_key}'][name*='revision']",
                    f".tier-{tier_key} select.revisions",
                ]
                for sel in rev_selectors:
                    try:
                        el = await pw_page.wait_for_selector(sel, timeout=2_000)
                        if el is not None:
                            await el.select_option(label=rev_str)
                            break
                    except Exception:
                        continue

            await asyncio.sleep(human_delay(0.5, 1.0))
            log.debug("package_filled", tier=tier_key)

    async def _fill_description(self, page: object, description: str) -> None:
        """Fill in the gig description."""
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        if not description:
            return

        desc_selector = await self._selectors.find(
            pw_page, "gig_creation", "description_editor"
        )

        if desc_selector is not None:
            try:
                # Some editors use contenteditable divs instead of textareas
                el = await pw_page.wait_for_selector(desc_selector, timeout=5_000)
                if el is not None:
                    tag = await el.evaluate("el => el.tagName.toLowerCase()")
                    is_editable = await el.get_attribute("contenteditable")

                    if tag == "textarea":
                        await self._engine.type_text(desc_selector, description)
                    elif is_editable == "true":
                        await el.click()
                        await asyncio.sleep(human_delay(0.3, 0.6))
                        await pw_page.keyboard.type(description, delay=30)
                    else:
                        await self._engine.type_text(desc_selector, description)

                    log.debug("description_filled", length=len(description))
                    return
            except Exception:
                log.warning("description_fill_primary_failed", exc_info=True)

        # Fallback: try generic description inputs
        fallbacks = [
            "textarea[name='description']",
            ".ql-editor",
            "[contenteditable='true']",
            "textarea.description",
        ]
        for sel in fallbacks:
            try:
                el = await pw_page.wait_for_selector(sel, timeout=3_000)
                if el is not None:
                    is_editable = await el.get_attribute("contenteditable")
                    if is_editable == "true":
                        await el.click()
                        await asyncio.sleep(human_delay(0.3, 0.6))
                        await pw_page.keyboard.type(description, delay=30)
                    else:
                        await self._engine.type_text(sel, description)
                    log.debug("description_filled_fallback", selector=sel)
                    return
            except Exception:
                continue

        log.warning("description_input_not_found")

    async def _click_save_and_continue(self, page: object) -> None:
        """Click the save/continue button between wizard steps."""
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        save_selectors = [
            "button:has-text('Save & Continue')",
            "button:has-text('Save and Continue')",
            "button:has-text('Continue')",
            "button:has-text('Next')",
            "[data-testid='save-continue']",
        ]

        # Also try the YAML-configured save button
        try:
            yaml_save = self._selectors.get("gig_creation", "save_button")
            save_selectors.insert(0, yaml_save)
        except KeyError:
            pass

        for selector in save_selectors:
            try:
                el = await pw_page.wait_for_selector(selector, timeout=5_000)
                if el is not None:
                    await asyncio.sleep(human_delay(0.5, 1.0))
                    await human_click(pw_page, selector)
                    await asyncio.sleep(page_load_wait())
                    log.debug("save_and_continue_clicked")
                    return
            except Exception:
                continue

        log.warning("save_continue_button_not_found")

    async def _publish_gig(self, page: object) -> str | None:
        """Click publish and extract the new gig ID.

        Returns the Fiverr gig ID or ``None``.
        """
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        publish_selectors = [
            "button:has-text('Publish')",
            "button:has-text('Publish Gig')",
            "[data-testid='publish-gig']",
        ]

        try:
            yaml_pub = self._selectors.get("gig_creation", "publish_button")
            publish_selectors.insert(0, yaml_pub)
        except KeyError:
            pass

        for selector in publish_selectors:
            try:
                el = await pw_page.wait_for_selector(selector, timeout=5_000)
                if el is not None:
                    await asyncio.sleep(human_delay(0.5, 1.5))
                    await human_click(pw_page, selector)
                    await asyncio.sleep(page_load_wait())
                    break
            except Exception:
                continue

        # Extract gig ID from the resulting URL
        await asyncio.sleep(human_delay(2.0, 4.0))
        current_url = pw_page.url
        gig_id = self._extract_gig_id_from_url(current_url)

        if gig_id is None:
            # Try to find the gig ID on the confirmation page
            gig_id = await self._extract_gig_id_from_page(pw_page)

        return gig_id

    # ------------------------------------------------------------------
    # Internal: utility
    # ------------------------------------------------------------------

    async def _select_option_by_text(
        self, page: object, selector: str, text: str
    ) -> None:
        """Select a ``<select>`` option whose label contains *text*."""
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        try:
            el = await pw_page.wait_for_selector(selector, timeout=5_000)
            if el is None:
                return

            # Try exact label match first, then partial
            try:
                await el.select_option(label=text)
                return
            except Exception:
                pass

            # Fallback: find option with partial text match
            options = await el.query_selector_all("option")
            for option in options:
                label = await option.text_content()
                if label and text.lower() in label.lower():
                    value = await option.get_attribute("value")
                    if value is not None:
                        await el.select_option(value=value)
                        return

            log.warning(
                "select_option_not_found",
                selector=selector,
                text=text,
            )
        except Exception:
            log.warning(
                "select_option_failed",
                selector=selector,
                text=text,
                exc_info=True,
            )

    @staticmethod
    def _extract_gig_id_from_url(url: str) -> str | None:
        """Parse a gig ID from a Fiverr URL.

        Examples of URL patterns:
        - ``https://www.fiverr.com/manage_gigs/123456``
        - ``https://www.fiverr.com/gigs/123456/edit``
        """
        for segment_prefix in ("/manage_gigs/", "/gigs/"):
            if segment_prefix in url:
                after = url.split(segment_prefix, 1)[1]
                gig_id = after.split("/")[0].split("?")[0]
                if gig_id and gig_id.isdigit():
                    return gig_id
        return None

    async def _extract_gig_id_from_page(self, page: object) -> str | None:
        """Try to scrape the gig ID from a confirmation / success page."""
        from playwright.async_api import Page as PwPage

        pw_page: PwPage = page  # type: ignore[assignment]

        # Look for gig ID text or links
        id_selectors = [
            ".gig-id",
            "[data-testid='gig-id']",
            "a[href*='/gigs/']",
            ".success-gig-link",
        ]

        for selector in id_selectors:
            try:
                el = await pw_page.wait_for_selector(selector, timeout=3_000)
                if el is not None:
                    href = await el.get_attribute("href")
                    if href:
                        gig_id = self._extract_gig_id_from_url(href)
                        if gig_id:
                            return gig_id

                    text = await el.text_content()
                    if text:
                        digits = "".join(ch for ch in text if ch.isdigit())
                        if digits:
                            return digits
            except Exception:
                continue

        return None

    async def _save_gig_to_db(
        self, template: dict[str, Any], gig_id: str
    ) -> None:
        """Persist a newly created gig in the database."""
        now = datetime.now(timezone.utc).isoformat()
        title = template.get("title", "")

        # Infer gig type from title keywords
        gig_type = "writing"
        lower_title = title.lower()
        if any(kw in lower_title for kw in ("python", "script", "code", "automation")):
            gig_type = "coding"
        elif any(kw in lower_title for kw in ("data entry", "spreadsheet", "excel")):
            gig_type = "data_entry"

        try:
            await self._db.execute(
                "INSERT INTO gigs (fiverr_gig_id, gig_type, title, status, created_at) "
                "VALUES (?, ?, ?, 'active', ?)",
                (gig_id, gig_type, title, now),
            )
            log.debug("gig_saved_to_db", gig_id=gig_id, title=title[:60])
        except Exception:
            log.warning("gig_db_save_failed", gig_id=gig_id, exc_info=True)

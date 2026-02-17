"""Human-like browser behaviour simulation for anti-detection.

Every function in this module accepts a Playwright ``Page`` and performs
mouse movements, scrolls, or idle pauses that mimic a real person
interacting with the site.  This makes automated sessions harder to
fingerprint as bots.
"""

from __future__ import annotations

import asyncio
import math
import random

from playwright.async_api import Page

from src.utils.human_timing import between_actions, human_delay, reading_delay
from src.utils.logger import get_logger

log = get_logger(__name__, component="anti_detect")


async def mouse_jitter(page: Page, x: int, y: int) -> None:
    """Move the mouse to (*x*, *y*) with slight random offsets.

    The final position will be within +/-5 px of the target, simulating
    the imprecision of a real hand on a mouse.
    """
    offset_x = random.randint(-5, 5)
    offset_y = random.randint(-5, 5)
    target_x = max(0, x + offset_x)
    target_y = max(0, y + offset_y)
    await page.mouse.move(target_x, target_y, steps=random.randint(5, 15))
    await asyncio.sleep(human_delay(0.05, 0.15))


async def random_scroll(page: Page) -> None:
    """Scroll the page by a random amount in a random direction.

    Occasionally scrolls up to simulate a user re-reading content.
    """
    direction = random.choices(["down", "up"], weights=[0.8, 0.2], k=1)[0]
    distance = random.randint(100, 500)
    if direction == "up":
        distance = -distance

    await page.mouse.wheel(0, distance)
    await asyncio.sleep(human_delay(0.3, 1.0))
    log.debug("random_scroll", direction=direction, distance=abs(distance))


async def human_click(page: Page, selector: str) -> None:
    """Click an element with a human-like curved mouse approach.

    The mouse travels from its current position to the element's centre
    (with a slight random offset) along an arc, then clicks after a
    brief pause.
    """
    element = await page.wait_for_selector(selector, timeout=10_000)
    if element is None:
        log.warning("human_click_element_not_found", selector=selector)
        return

    box = await element.bounding_box()
    if box is None:
        # Element has no visual representation; fall back to direct click.
        await element.click()
        return

    # Target with slight randomness within the bounding box
    target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

    # Curved mouse movement using a Bezier-like multi-step path
    await _curved_mouse_move(page, target_x, target_y)

    # Short pause before clicking, like a person aiming
    await asyncio.sleep(human_delay(0.05, 0.2))
    await page.mouse.click(target_x, target_y)
    await asyncio.sleep(human_delay(0.1, 0.4))

    log.debug("human_click", selector=selector, x=round(target_x), y=round(target_y))


async def simulate_reading(page: Page, duration: float | None = None) -> None:
    """Simulate a user reading the current page.

    Performs a series of small scrolls and idle pauses.  If *duration* is
    not supplied, one is estimated from the page's visible text length.
    """
    if duration is None:
        text = await page.inner_text("body")
        duration = reading_delay(len(text))

    elapsed = 0.0
    while elapsed < duration:
        action = random.choice(["scroll", "idle", "idle", "mouse"])
        if action == "scroll":
            await random_scroll(page)
            step = human_delay(0.5, 1.5)
        elif action == "mouse":
            await random_mouse_movement(page)
            step = human_delay(0.3, 0.8)
        else:
            step = human_delay(1.0, 3.0)
            await asyncio.sleep(step)
        elapsed += step

    log.debug("simulate_reading_done", duration_secs=round(elapsed, 1))


async def random_mouse_movement(page: Page) -> None:
    """Move the mouse to a random position within the viewport."""
    viewport = page.viewport_size
    if viewport is None:
        viewport = {"width": 1920, "height": 1080}

    target_x = random.randint(50, viewport["width"] - 50)
    target_y = random.randint(50, viewport["height"] - 50)

    await _curved_mouse_move(page, target_x, target_y)
    await asyncio.sleep(human_delay(0.1, 0.5))

    log.debug(
        "random_mouse_movement",
        target_x=target_x,
        target_y=target_y,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _curved_mouse_move(
    page: Page,
    target_x: float,
    target_y: float,
    steps: int | None = None,
) -> None:
    """Move the mouse along a slightly curved path to (*target_x*, *target_y*).

    The curve is achieved by introducing a random control point and
    interpolating a quadratic Bezier curve.
    """
    # Estimate current mouse position (default to centre of viewport)
    viewport = page.viewport_size or {"width": 1920, "height": 1080}
    # Playwright does not expose the current mouse coords, so we pick a
    # reasonable "last known" origin near the viewport centre.
    start_x = viewport["width"] / 2.0 + random.uniform(-100, 100)
    start_y = viewport["height"] / 2.0 + random.uniform(-100, 100)

    # Compute distance to decide number of steps
    dist = math.hypot(target_x - start_x, target_y - start_y)
    if steps is None:
        steps = max(10, int(dist / 15))

    # Control point for quadratic Bezier (offset from midpoint)
    mid_x = (start_x + target_x) / 2.0 + random.uniform(-80, 80)
    mid_y = (start_y + target_y) / 2.0 + random.uniform(-80, 80)

    for i in range(1, steps + 1):
        t = i / steps
        inv = 1 - t
        # Quadratic Bezier: B(t) = (1-t)^2 P0 + 2(1-t)t P1 + t^2 P2
        x = inv * inv * start_x + 2 * inv * t * mid_x + t * t * target_x
        y = inv * inv * start_y + 2 * inv * t * mid_y + t * t * target_y
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.005, 0.02))

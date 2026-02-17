"""Gaussian delay generators for anti-detection timing.

Every public function returns a ``float`` representing seconds.  The
delays are drawn from a Gaussian (normal) distribution and clipped to a
hard ``[min, max]`` range so that no single sample can be suspiciously
fast or unreasonably slow.

Usage example::

    import asyncio
    from src.utils.human_timing import typing_delay, between_actions

    await asyncio.sleep(between_actions())

    for char in text:
        await page.keyboard.press(char)
        await asyncio.sleep(typing_delay())
"""

from __future__ import annotations

import random


def _clipped_gauss(mean: float, std: float, lo: float, hi: float) -> float:
    """Return a Gaussian sample clipped to *[lo, hi]*."""
    return max(lo, min(hi, random.gauss(mean, std)))


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def human_delay(min_sec: float = 0.5, max_sec: float = 2.0) -> float:
    """General-purpose human-like delay in seconds.

    The mean is placed at the midpoint of *[min_sec, max_sec]* and the
    standard deviation is set to one-quarter of the range so that ~95 %
    of unclipped samples already fall within the bounds.

    Parameters
    ----------
    min_sec:
        Lower bound (inclusive).
    max_sec:
        Upper bound (inclusive).
    """
    mean = (min_sec + max_sec) / 2.0
    std = (max_sec - min_sec) / 4.0
    return _clipped_gauss(mean, std, min_sec, max_sec)


def typing_delay() -> float:
    """Delay between individual keystrokes.

    Mimics a moderately fast human typist (~75 WPM).

    Returns
    -------
    float
        Seconds in the range [0.03, 0.20].
    """
    return _clipped_gauss(mean=0.08, std=0.03, lo=0.03, hi=0.20)


def reading_delay(text_length: int) -> float:
    """Estimated time a human would spend reading *text_length* characters.

    The base rate assumes 200-250 words per minute with an average word
    length of five characters.  Gaussian noise is added on top.

    Parameters
    ----------
    text_length:
        Number of characters in the text to "read".

    Returns
    -------
    float
        Seconds (minimum 0.5 s).
    """
    avg_word_len = 5
    words = max(text_length, 1) / avg_word_len
    # Pick a reading speed between 200 and 250 wpm (gaussian centred at 225)
    wpm = _clipped_gauss(mean=225.0, std=15.0, lo=200.0, hi=250.0)
    base_seconds = (words / wpm) * 60.0
    # Add noise proportional to the base time
    noise = random.gauss(0, base_seconds * 0.1)
    return max(0.5, base_seconds + noise)


def page_load_wait() -> float:
    """Delay after a page navigation to simulate "looking at the page".

    Returns
    -------
    float
        Seconds in the range [1.0, 4.0].
    """
    return _clipped_gauss(mean=2.0, std=0.5, lo=1.0, hi=4.0)


def between_actions() -> float:
    """Delay between two distinct UI actions (click, scroll, etc.).

    Returns
    -------
    float
        Seconds in the range [0.5, 3.0].
    """
    return _clipped_gauss(mean=1.5, std=0.5, lo=0.5, hi=3.0)


def poll_interval(min_minutes: int = 3, max_minutes: int = 5) -> float:
    """Seconds to wait between successive polling cycles.

    Parameters
    ----------
    min_minutes:
        Lower bound in whole minutes.
    max_minutes:
        Upper bound in whole minutes.

    Returns
    -------
    float
        Seconds (not minutes) in the range
        ``[min_minutes * 60, max_minutes * 60]``.
    """
    lo = min_minutes * 60.0
    hi = max_minutes * 60.0
    mean = (lo + hi) / 2.0
    std = (hi - lo) / 4.0
    return _clipped_gauss(mean, std, lo, hi)

"""Adaptive polling: how long to wait before polling a bookmaker again.

Simple, deterministic tiers based on how soon the *next* match at that bookmaker starts:

* a match starts within ``soon_hours``   -> poll faster  (``soon_factor`` x the configured interval)
* nothing starts within ``far_hours``    -> poll slower  (``far_factor`` x the configured interval)
* otherwise                              -> the configured interval

Always clamped to ``[min_interval, max_interval]``. The 1-request-per-second-per-site ceiling of the HTTP
client still applies on top of this, and failing polls keep their exponential backoff (this only shapes
the delay after a *successful* poll). Nothing here evades or hammers anything: the fastest tier is still
one poll every ``min_interval`` seconds (default 30 s) per bookmaker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


@dataclass(frozen=True)
class AdaptiveSettings:
    enabled: bool = True
    soon_hours: float = 3.0
    soon_factor: float = 0.5
    far_hours: float = 24.0
    far_factor: float = 2.0
    min_interval: float = 30.0
    max_interval: float = 300.0


def soonest_start(starts: Iterable[datetime], now: datetime) -> timedelta | None:
    """Time until the earliest kick-off still in the future (None if there is none)."""
    upcoming = [s - now for s in starts if s > now]
    return min(upcoming) if upcoming else None


def adaptive_interval(base: float, soonest: timedelta | None, settings: AdaptiveSettings) -> tuple[float, str]:
    """Return (seconds to wait, short reason). ``base`` is the configured poll interval."""
    if not settings.enabled:
        return base, "adaptive polling off"
    if soonest is not None and soonest <= timedelta(hours=settings.soon_hours):
        factor, reason = settings.soon_factor, f"a match starts within {settings.soon_hours:g}h"
    elif soonest is None or soonest > timedelta(hours=settings.far_hours):
        factor, reason = settings.far_factor, f"nothing starts within {settings.far_hours:g}h"
    else:
        factor, reason = 1.0, "normal"
    if factor < 1.0:  # faster: respect the floor, but never end up slower than the configured interval
        seconds = max(base * factor, min(settings.min_interval, base))
    elif factor > 1.0:  # slower: respect the ceiling, but never end up faster than the configured interval
        seconds = min(base * factor, max(settings.max_interval, base))
    else:
        seconds = base
    return seconds, reason

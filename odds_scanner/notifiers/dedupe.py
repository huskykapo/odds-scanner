from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DedupeCache:
    """Remembers keys for ``ttl`` so the same alert is not sent again within that window."""

    def __init__(self, ttl: timedelta, clock: Callable[[], datetime] = _utcnow) -> None:
        self._ttl = ttl
        self._clock = clock
        self._seen: dict[str, datetime] = {}

    def _prune(self, now: datetime) -> None:
        for key in [k for k, t in self._seen.items() if now - t >= self._ttl]:
            del self._seen[key]

    def is_duplicate(self, key: str) -> bool:
        self._prune(self._clock())
        return key in self._seen

    def remember(self, key: str) -> None:
        now = self._clock()
        self._prune(now)
        self._seen[key] = now

    def __len__(self) -> int:
        self._prune(self._clock())
        return len(self._seen)

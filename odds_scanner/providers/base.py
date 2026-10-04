"""The data-source interface. Implement :class:`OddsProvider` to add a new odds source."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence

from odds_scanner.models import Event, QuotaInfo


@dataclass(frozen=True)
class FetchResult:
    events: list[Event] = field(default_factory=list)
    quota: QuotaInfo | None = None  # None when the source has no quota concept


class OddsProvider(ABC):
    """Source of upcoming events with per-bookmaker odds (decimal format)."""

    name: str = "provider"

    @abstractmethod
    def fetch_odds(
        self,
        sport: str,
        *,
        regions: Sequence[str],
        markets: Sequence[str],
        bookmakers: Sequence[str] = (),
    ) -> FetchResult:
        """Return upcoming events for ``sport``.

        Raises :class:`~odds_scanner.errors.ProviderError` (or a subclass) on failure.
        """

    def close(self) -> None:  # pragma: no cover - optional hook
        """Release resources (HTTP sessions, files)."""

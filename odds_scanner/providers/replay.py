"""Offline provider that replays events from a JSON file - no network or API key needed."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from odds_scanner.errors import ProviderError
from odds_scanner.models import BookmakerOdds, Event, MarketOdds
from odds_scanner.providers.base import FetchResult, OddsProvider
from odds_scanner.providers.parsing import parse_events

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReplayProvider(OddsProvider):
    """Serves events from a file in The Odds API's response format.

    The file is either a JSON list of events (served for any sport whose key matches the
    event's ``sport_key``) or an object mapping sport keys to such lists.

    With ``rebase_timestamps`` every ``last_update`` and ``commence_time`` is shifted so the
    newest ``last_update`` in the file equals "now". That makes a fixed sample file look
    fresh to the staleness filter on every run, while keeping all relative ages intact.
    """

    name = "replay"

    def __init__(
        self,
        path: str | Path,
        *,
        rebase_timestamps: bool = False,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._path = Path(path)
        self._rebase = rebase_timestamps
        self._clock = clock
        try:
            self._raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProviderError(f"cannot read replay file {self._path}: {exc.strerror or exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(f"replay file {self._path} is not valid JSON: {exc}") from exc
        if not isinstance(self._raw, (list, dict)):
            raise ProviderError(f"replay file {self._path} must contain a JSON list or object")

    def sport_keys(self) -> list[str]:
        """Sport keys present in the file, in first-seen order."""
        if isinstance(self._raw, dict):
            return list(self._raw)
        keys = (e.get("sport_key") for e in self._raw if isinstance(e, dict))
        return list(dict.fromkeys(k for k in keys if isinstance(k, str)))

    def fetch_odds(
        self,
        sport: str,
        *,
        regions: Sequence[str],
        markets: Sequence[str],
        bookmakers: Sequence[str] = (),
    ) -> FetchResult:
        if isinstance(self._raw, dict):
            raw_events = self._raw.get(sport, [])
        else:
            raw_events = [e for e in self._raw if isinstance(e, dict) and e.get("sport_key") == sport]
        events = parse_events(raw_events)
        if self._rebase:
            events = self._rebased(events)
        log.debug("replay: %d event(s) for %s", len(events), sport)
        return FetchResult(events=events, quota=None)

    def _rebased(self, events: list[Event]) -> list[Event]:
        stamps = [
            t
            for e in events
            for b in e.bookmakers
            for t in [b.last_update, *(m.last_update for m in b.markets)]
            if t is not None
        ]
        if not stamps:
            return events
        shift: timedelta = self._clock() - max(stamps)

        def sh(t: datetime | None) -> datetime | None:
            return t + shift if t is not None else None

        return [
            Event(
                id=e.id,
                sport_key=e.sport_key,
                sport_title=e.sport_title,
                commence_time=e.commence_time + shift,
                home_team=e.home_team,
                away_team=e.away_team,
                bookmakers=tuple(
                    BookmakerOdds(
                        key=b.key,
                        title=b.title,
                        last_update=sh(b.last_update),
                        markets=tuple(
                            MarketOdds(key=m.key, outcomes=m.outcomes, last_update=sh(m.last_update))
                            for m in b.markets
                        ),
                    )
                    for b in e.bookmakers
                ),
            )
            for e in events
        ]

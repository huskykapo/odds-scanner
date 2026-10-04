"""Parse The Odds API v4 JSON (also used for replay files) into :mod:`odds_scanner.models`."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Iterable

from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome

log = logging.getLogger(__name__)


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime (naive input is taken as UTC)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"timestamp must be a string, got {value!r}")
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _parse_outcome(raw: dict[str, Any]) -> Outcome | None:
    try:
        price = float(raw["price"])
        name = str(raw["name"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(price):
        return None
    point = raw.get("point")
    return Outcome(name=name, price=price, point=float(point) if point is not None else None)


def _parse_market(raw: dict[str, Any]) -> MarketOdds:
    outcomes = tuple(o for o in (_parse_outcome(r) for r in raw.get("outcomes") or []) if o is not None)
    return MarketOdds(key=str(raw["key"]), outcomes=outcomes, last_update=parse_datetime(raw.get("last_update")))


def _parse_bookmaker(raw: dict[str, Any]) -> BookmakerOdds:
    return BookmakerOdds(
        key=str(raw["key"]),
        title=str(raw.get("title") or raw["key"]),
        markets=tuple(_parse_market(m) for m in raw.get("markets") or []),
        last_update=parse_datetime(raw.get("last_update")),
    )


def parse_event(raw: dict[str, Any]) -> Event:
    """Parse one event. Raises ``ValueError``/``KeyError``/``TypeError`` if it is malformed."""
    commence = parse_datetime(raw["commence_time"])
    if commence is None:
        raise ValueError("commence_time is null")
    return Event(
        id=str(raw["id"]),
        sport_key=str(raw["sport_key"]),
        sport_title=str(raw.get("sport_title") or raw["sport_key"]),
        commence_time=commence,
        home_team=str(raw["home_team"]),
        away_team=str(raw["away_team"]),
        bookmakers=tuple(_parse_bookmaker(b) for b in raw.get("bookmakers") or []),
    )


def parse_events(raw_events: Iterable[Any]) -> list[Event]:
    """Parse a list of events, logging and skipping malformed entries instead of failing the batch."""
    events: list[Event] = []
    for raw in raw_events:
        try:
            events.append(parse_event(raw))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            ident = raw.get("id") if isinstance(raw, dict) else None
            log.warning("skipping malformed event %r: %s: %s", ident, type(exc).__name__, exc)
    return events

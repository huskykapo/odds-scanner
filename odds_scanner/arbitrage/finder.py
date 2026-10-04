"""Find arbitrages in a batch of events.

For every event, market and line the finder takes the best (highest) decimal odds per
outcome across bookmakers, and reports the combination when the inverse odds sum to < 1
and the legs come from at least two different bookmakers.

Safeguards against false positives (all of them matter - false arbs are the norm):

* stale prices (old ``last_update``) and events that have already started are ignored;
* totals/spreads are grouped by line, so "Over 220.5" is never paired with "Under 224.5";
* a bookmaker only counts for a market if it quotes *every* outcome seen for it - a book
  that omits the Draw of a 1X2 market (suspended) must not turn it into a fake two-way;
* odds <= 1 or non-finite are discarded as bad data;
* implausibly large profits (``max_profit_percent``) are discarded as bad data.
"""

from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from odds_scanner.arbitrage.calculator import EPSILON, calculate_stakes, inverse_sum
from odds_scanner.config import TWO_OR_THREE_WAY_MARKETS, TWO_WAY_MARKETS
from odds_scanner.models import ArbLeg, Arbitrage, Event, MarketOdds

log = logging.getLogger(__name__)

DRAW = "Draw"


@dataclass(frozen=True)
class FinderSettings:
    bankroll: float = 1000.0
    min_profit_percent: float = 1.0
    max_profit_percent: float | None = 25.0
    stale_after: timedelta = timedelta(seconds=300)
    stake_rounding: float = 1.0
    bookmakers: frozenset[str] = field(default_factory=frozenset)  # whitelist; empty = all


# (market key, line) -> bookmaker key -> (title, {outcome name: decimal odds})
_Quotes = dict[tuple[str, float | None], dict[str, tuple[str, dict[str, float]]]]


def _line_for(market_key: str, name: str, point: float | None, event: Event) -> tuple[bool, float | None]:
    """Return (usable, line) for an outcome.

    Totals share one line. For spreads the line is the *home* team's handicap, so
    "home -5.5" and "away +5.5" land in the same group.
    """
    if market_key == "spreads":
        if point is None:
            return False, None
        if name == event.home_team:
            return True, round(point, 3)
        if name == event.away_team:
            return True, round(-point, 3)
        return False, None
    if market_key == "totals":
        return (point is not None), (round(point, 3) if point is not None else None)
    return True, None


def _market_time(market: MarketOdds, bookmaker_time: datetime | None) -> datetime | None:
    return market.last_update or bookmaker_time


def collect_quotes(event: Event, settings: FinderSettings, now: datetime) -> _Quotes:
    """Gather usable, fresh prices grouped by (market, line) and bookmaker."""
    quotes: _Quotes = {}
    for bm in event.bookmakers:
        if settings.bookmakers and bm.key not in settings.bookmakers:
            continue
        for market in bm.markets:
            if market.key not in TWO_OR_THREE_WAY_MARKETS | TWO_WAY_MARKETS:
                continue
            updated = _market_time(market, bm.last_update)
            if updated is None or now - updated > settings.stale_after:
                log.debug("%s: ignoring stale/undated %s odds from %s", event.id, market.key, bm.key)
                continue
            by_line: dict[float | None, dict[str, float]] = {}
            for o in market.outcomes:
                if not math.isfinite(o.price) or o.price <= 1.0:
                    log.debug("%s: ignoring invalid price %r from %s", event.id, o.price, bm.key)
                    continue
                usable, line = _line_for(market.key, o.name, o.point, event)
                if not usable:
                    continue
                prices = by_line.setdefault(line, {})
                # A duplicate outcome in one book is ambiguous; keep the less favourable price.
                prices[o.name] = min(o.price, prices[o.name]) if o.name in prices else o.price
            for line, prices in by_line.items():
                quotes.setdefault((market.key, line), {})[bm.key] = (bm.title, prices)
    return quotes


def _order_outcomes(names: Iterable[str], event: Event) -> list[str]:
    """Stable, readable order: home, draw, away (otherwise alphabetical, e.g. Over before Under)."""
    names = set(names)
    ordered = [n for n in (event.home_team, DRAW, event.away_team) if n in names]
    return ordered + sorted(names - set(ordered))


def _best_legs(
    names: list[str], books: dict[str, tuple[str, dict[str, float]]]
) -> list[tuple[str, str, str, float]] | None:
    """Pick (outcome, bookmaker key, title, odds) per outcome: the highest odds, spanning >=2 bookmakers.

    On tied best odds, prefers a combination that uses more than one bookmaker.
    Returns None if every best combination comes from a single bookmaker.
    """
    candidates: list[list[tuple[str, str, str, float]]] = []
    for name in names:
        best = max(prices[name] for _, prices in books.values())
        candidates.append(
            [(name, key, title, best) for key, (title, prices) in sorted(books.items()) if prices[name] == best]
        )
    for combo in itertools.product(*candidates):
        if len({leg[1] for leg in combo}) >= 2:
            return list(combo)
    return None


def find_arbitrages(
    events: Iterable[Event], settings: FinderSettings, *, now: datetime | None = None
) -> list[Arbitrage]:
    """Return arbitrages across ``events``, best (post-rounding) profit % first."""
    now = now or datetime.now(timezone.utc)
    found: list[Arbitrage] = []
    for event in events:
        if event.commence_time <= now:
            log.debug("%s: already started, skipping", event.id)
            continue
        for (market_key, line), books in collect_quotes(event, settings, now).items():
            arb = _arb_for_group(event, market_key, line, books, settings, now)
            if arb is not None:
                found.append(arb)
    found.sort(key=lambda a: (-a.realized_profit_percent, a.event_name, a.market))
    return found


def _arb_for_group(
    event: Event,
    market_key: str,
    line: float | None,
    books: dict[str, tuple[str, dict[str, float]]],
    settings: FinderSettings,
    now: datetime,
) -> Arbitrage | None:
    names = sorted({n for _, prices in books.values() for n in prices})
    allowed = (2, 3) if market_key in TWO_OR_THREE_WAY_MARKETS else (2,)
    if len(names) not in allowed:
        log.debug("%s/%s: %d distinct outcomes %s, not a complete market; skipping", event.id, market_key, len(names), names)
        return None

    # Only bookmakers quoting every outcome count (guards against suspended/omitted selections).
    complete = {k: v for k, v in books.items() if set(v[1]) == set(names)}
    if len(complete) < 2:
        return None

    ordered = _order_outcomes(names, event)
    legs = _best_legs(ordered, complete)
    if legs is None:
        log.debug("%s/%s: best odds all from one bookmaker, skipping", event.id, market_key)
        return None

    odds = [leg[3] for leg in legs]
    inv = inverse_sum(odds)
    if inv >= 1.0 - EPSILON:
        return None
    theoretical = (1.0 / inv - 1.0) * 100.0
    if theoretical < settings.min_profit_percent:
        return None
    if settings.max_profit_percent is not None and theoretical > settings.max_profit_percent:
        log.warning(
            "%s/%s: %.1f%% profit exceeds max_profit_percent (%.1f%%); treating as bad data",
            event.id, market_key, theoretical, settings.max_profit_percent,
        )
        return None

    plan = calculate_stakes(odds, settings.bankroll, settings.stake_rounding)
    if plan.guaranteed_profit <= 0:
        log.debug("%s/%s: no profit left after rounding stakes to %s", event.id, market_key, settings.stake_rounding)
        return None

    return Arbitrage(
        event_id=event.id,
        sport_key=event.sport_key,
        event_name=event.name,
        commence_time=event.commence_time,
        market=market_key,
        line=line,
        legs=tuple(
            ArbLeg(outcome=name, bookmaker_key=key, bookmaker_title=title, odds=o, stake=stake, payout=payout)
            for (name, key, title, o), stake, payout in zip(legs, plan.stakes, plan.payouts)
        ),
        inverse_sum=inv,
        profit_percent=theoretical,
        bankroll=settings.bankroll,
        total_stake=plan.total_stake,
        guaranteed_profit=plan.guaranteed_profit,
        realized_profit_percent=plan.profit_percent,
        found_at=now,
    )

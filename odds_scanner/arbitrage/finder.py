"""Find arbitrages in a batch of events.

For every event, market and line the finder takes the best (highest) decimal odds per
outcome across bookmakers, and reports the combination when the inverse odds sum to < 1
and the legs come from at least two different bookmakers.

Safeguards against false positives (all of them matter - false arbs are the norm):

* stale prices (old ``last_update``) and events that have already started are ignored;
* totals/spreads are grouped by line, so "Over 220.5" is never paired with "Under 224.5";
* markets are grouped by period too (``h2h_3_way@reg`` vs ``h2h_3_way``), so a regulation-time
  result is never combined with a full-time / incl.-overtime one;
* a bookmaker only counts for a market if it quotes *every* outcome seen for it - a book
  that omits the Draw of a 1X2 market (suspended) must not turn it into a fake two-way;
* odds <= 1 or non-finite are discarded as bad data;
* implausibly large profits (``max_profit_percent``) are discarded as bad data, and profits
  above ``verify_above_percent`` are flagged "verify manually".

Double chance: each bookmaker's ``1X`` is paired with the ``2`` of a complete 1X2 market (same
period), ``X2`` with ``1`` and ``12`` with ``X``. These pairs are exhaustive by construction, so
here a bookmaker may contribute just one side.

Per-bookmaker stake fees / win taxes are applied before comparing prices (see
:func:`odds_scanner.markets.effective_odds`), and stakes are rounded to each bookmaker's step
and minimum stake before the profit is recomputed.
"""

from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

from odds_scanner.arbitrage.calculator import EPSILON, calculate_stakes, inverse_sum
from odds_scanner.config import TWO_OR_THREE_WAY_MARKETS, TWO_WAY_MARKETS
from odds_scanner.markets import (
    AWAY,
    DC_CODES,
    DC_PAIRS,
    DOUBLE_CHANCE,
    DRAW as DRAW_CODE,
    H2H_3_WAY,
    HOME,
    effective_odds,
    split_period,
    with_period,
)
from odds_scanner.models import ArbLeg, Arbitrage, Event, MarketOdds

log = logging.getLogger(__name__)

DRAW = "Draw"

PAIR_MARKETS = frozenset(DC_PAIRS)  # derived double-chance pairs: partial bookmakers allowed
_FINDER_MARKETS = TWO_OR_THREE_WAY_MARKETS | TWO_WAY_MARKETS


@dataclass(frozen=True)
class BookRule:
    """Per-bookmaker money rules (all optional)."""

    stake_step: float | None = None  # round stakes to this; None = FinderSettings.stake_rounding
    min_stake: float = 0.0
    stake_fee: float = 0.0  # percent of the stake kept by the bookmaker
    win_tax: float = 0.0  # percent of net winnings withheld


@dataclass(frozen=True)
class FinderSettings:
    bankroll: float = 1000.0
    min_profit_percent: float = 1.0
    max_profit_percent: float | None = 25.0
    stale_after: timedelta = timedelta(seconds=300)
    stake_rounding: float = 1.0
    bookmakers: frozenset[str] = field(default_factory=frozenset)  # whitelist; empty = all
    verify_above_percent: float | None = None  # flag (not drop) arbs above this profit
    book_rules: Mapping[str, BookRule] = field(default_factory=dict)

    def rule(self, bookmaker_key: str) -> BookRule:
        return self.book_rules.get(bookmaker_key) or BookRule()


@dataclass
class _Quote:
    """One bookmaker's prices for one (market, line) group."""

    title: str
    prices: dict[str, float]  # outcome -> quoted odds
    effective: dict[str, float]  # outcome -> odds after fee/tax (what is compared)
    updated: datetime | None = None
    url: str | None = None
    event_name: str | None = None


# (market key, line) -> bookmaker key -> quote
_Quotes = dict[tuple[str, float | None], dict[str, _Quote]]


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
        rule = settings.rule(bm.key)
        own: _Quotes = {}
        for market in bm.markets:
            base, _ = split_period(market.key)
            if base not in _FINDER_MARKETS and base != DOUBLE_CHANCE:
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
                usable, line = _line_for(base, o.name, o.point, event)
                if not usable:
                    continue
                prices = by_line.setdefault(line, {})
                # A duplicate outcome in one book is ambiguous; keep the less favourable price.
                prices[o.name] = min(o.price, prices[o.name]) if o.name in prices else o.price
            for line, prices in by_line.items():
                eff = {n: effective_odds(p, rule.stake_fee, rule.win_tax) for n, p in prices.items()}
                own[(market.key, line)] = _Quote(bm.title, prices, eff, updated, bm.url, bm.event_name)
        _add_double_chance_pairs(own)
        for group, quote in own.items():
            if split_period(group[0])[0] == DOUBLE_CHANCE:
                continue  # only used through the derived pairs
            quotes.setdefault(group, {})[bm.key] = quote
    return quotes


def _add_double_chance_pairs(own: _Quotes) -> None:
    """Derive "1X vs 2"-style two-way groups from one bookmaker's 1X2 and double-chance quotes."""
    for (key, line), quote in list(own.items()):
        base, period = split_period(key)
        if base == DOUBLE_CHANCE:
            sources = [(quote, set(DC_CODES))]
        elif base == H2H_3_WAY and set(quote.prices) == {HOME, DRAW_CODE, AWAY}:
            sources = [(quote, {HOME, DRAW_CODE, AWAY})]  # only a complete 1X2 lends single outcomes
        else:
            continue
        for src, allowed in sources:
            for pair_key, pair in DC_PAIRS.items():
                group = (with_period(pair_key, period), line)
                for name in pair:
                    if name in allowed and name in src.prices:
                        target = own.setdefault(group, _Quote(src.title, {}, {}, src.updated, src.url, src.event_name))
                        target.prices[name] = src.prices[name]
                        target.effective[name] = src.effective[name]
                        if src.updated and (target.updated is None or src.updated < target.updated):
                            target.updated = src.updated  # report the older of the two prices


_CODE_ORDER = (HOME, DRAW_CODE, AWAY, "1X", "X2", "12", "Over", "Under")


def _order_outcomes(names: Iterable[str], event: Event) -> list[str]:
    """Stable, readable order: home, draw, away (otherwise alphabetical, e.g. Over before Under)."""
    names = set(names)
    ordered = [n for n in (event.home_team, DRAW, event.away_team) if n in names]
    ordered += [n for n in _CODE_ORDER if n in names and n not in ordered]
    return ordered + sorted(names - set(ordered))


def _best_legs(names: list[str], books: dict[str, _Quote]) -> list[tuple[str, str]] | None:
    """Pick (outcome, bookmaker key) per outcome: the highest effective odds, spanning >=2 bookmakers.

    On tied best odds, prefers a combination that uses more than one bookmaker.
    Returns None if every best combination comes from a single bookmaker.
    """
    candidates: list[list[tuple[str, str]]] = []
    for name in names:
        quoted = {k: q.effective[name] for k, q in books.items() if name in q.effective}
        best = max(quoted.values())
        candidates.append([(name, k) for k in sorted(quoted) if quoted[k] == best])
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
    books: dict[str, _Quote],
    settings: FinderSettings,
    now: datetime,
) -> Arbitrage | None:
    base, _ = split_period(market_key)
    names = sorted({n for q in books.values() for n in q.prices})
    if base in PAIR_MARKETS:
        if set(names) != set(DC_PAIRS[base]):
            return None
        complete = books  # each side alone is a real price; the pair itself is exhaustive
    else:
        allowed = (2, 3) if base in TWO_OR_THREE_WAY_MARKETS else (2,)
        if len(names) not in allowed:
            log.debug("%s/%s: %d distinct outcomes %s, not a complete market; skipping", event.id, market_key, len(names), names)
            return None
        # Only bookmakers quoting every outcome count (guards against suspended/omitted selections).
        complete = {k: q for k, q in books.items() if set(q.prices) == set(names)}
    if len(complete) < 2:
        return None

    ordered = _order_outcomes(names, event)
    picks = _best_legs(ordered, complete)
    if picks is None:
        log.debug("%s/%s: best odds all from one bookmaker, skipping", event.id, market_key)
        return None

    odds = [complete[k].effective[name] for name, k in picks]
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

    rules = [settings.rule(k) for _, k in picks]
    plan = calculate_stakes(
        odds,
        settings.bankroll,
        [r.stake_step or settings.stake_rounding for r in rules],
        min_stakes=[r.min_stake for r in rules],
    )
    if plan.guaranteed_profit <= 0:
        log.debug("%s/%s: no profit left after rounding stakes", event.id, market_key)
        return None

    legs = []
    for (name, key), stake, payout in zip(picks, plan.stakes, plan.payouts):
        q = complete[key]
        quoted, eff = q.prices[name], q.effective[name]
        legs.append(
            ArbLeg(
                outcome=name, bookmaker_key=key, bookmaker_title=q.title, odds=quoted, stake=stake, payout=payout,
                effective_odds=None if eff == quoted else eff, odds_updated=q.updated, url=q.url,
                event_name=q.event_name,
            )
        )
    verify = settings.verify_above_percent is not None and theoretical > settings.verify_above_percent
    return Arbitrage(
        event_id=event.id,
        sport_key=event.sport_key,
        event_name=event.name,
        commence_time=event.commence_time,
        market=market_key,
        line=line,
        legs=tuple(legs),
        inverse_sum=inv,
        profit_percent=theoretical,
        bankroll=settings.bankroll,
        total_stake=plan.total_stake,
        guaranteed_profit=plan.guaranteed_profit,
        realized_profit_percent=plan.profit_percent,
        found_at=now,
        verify_manually=verify,
        home_team=event.home_team,
        away_team=event.away_team,
    )


__all__ = ["BookRule", "FinderSettings", "collect_quotes", "find_arbitrages"]

"""Plain data types shared by providers, the arbitrage engine and the sinks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


# --------------------------------------------------------------------------- odds data
@dataclass(frozen=True)
class Outcome:
    """One priced selection, e.g. ``Outcome("Over", 1.95, point=2.5)``."""

    name: str
    price: float  # decimal odds
    point: float | None = None  # line for totals/spreads


@dataclass(frozen=True)
class MarketOdds:
    key: str  # "h2h", "totals", "spreads", ...
    outcomes: tuple[Outcome, ...]
    last_update: datetime | None = None


@dataclass(frozen=True)
class BookmakerOdds:
    key: str
    title: str
    markets: tuple[MarketOdds, ...]
    last_update: datetime | None = None
    url: str | None = None  # direct link to the match on the bookmaker's site, when known
    event_name: str | None = None  # the bookmaker's own spelling of the match (after merging)
    event_id: str | None = None  # the provider's own event id (e.g. "doxxbet:80003378") after merging


@dataclass(frozen=True)
class Event:
    id: str
    sport_key: str
    sport_title: str
    commence_time: datetime
    home_team: str
    away_team: str
    bookmakers: tuple[BookmakerOdds, ...]
    betradar_id: str | None = None  # Betradar (Sportradar) match id, digits only, when the source has one

    @property
    def name(self) -> str:
        return f"{self.home_team} vs {self.away_team}"


@dataclass(frozen=True)
class QuotaInfo:
    """Request-quota state reported by a provider (any field may be unknown)."""

    remaining: int | None = None
    used: int | None = None
    last_cost: int | None = None


# --------------------------------------------------------------------------- results
@dataclass(frozen=True)
class ArbLeg:
    """One bet of an arbitrage: back ``outcome`` at ``bookmaker`` for ``stake``."""

    outcome: str
    bookmaker_key: str
    bookmaker_title: str
    odds: float
    stake: float
    payout: float  # stake * effective odds
    effective_odds: float | None = None  # odds after the bookmaker's stake fee / win tax (None = same as odds)
    odds_updated: datetime | None = None  # when this price was fetched
    url: str | None = None  # direct link to the match at this bookmaker, when known
    event_name: str | None = None  # the bookmaker's own name for the match


@dataclass(frozen=True)
class Arbitrage:
    event_id: str
    sport_key: str
    event_name: str
    commence_time: datetime
    market: str
    line: float | None  # totals line / home-team handicap; None for h2h
    legs: tuple[ArbLeg, ...]
    inverse_sum: float
    profit_percent: float  # theoretical, before stake rounding
    bankroll: float
    total_stake: float  # after rounding; may differ slightly from bankroll
    guaranteed_profit: float  # worst case, after rounding
    realized_profit_percent: float  # guaranteed_profit / total_stake, after rounding
    found_at: datetime
    verify_manually: bool = False  # suspiciously high profit: probably a pricing error or stale odds
    home_team: str | None = None
    away_team: str | None = None
    # Filled in by the opportunity registry (see opportunities.py); empty for a raw finder result.
    confidence: int | None = None  # 0-100, deterministic heuristic
    confidence_label: str = ""  # HIGH / MEDIUM / LOW
    status: str = ""  # PENDING / VERIFIED / FAILED

    @property
    def bookmakers(self) -> tuple[str, ...]:
        return tuple(leg.bookmaker_title for leg in self.legs)

    @property
    def fingerprint(self) -> str:
        """The opportunity: event + market + line + which bookmaker is used for which outcome.

        Odds are NOT part of it, so +2.84 % -> +2.90 % -> +2.75 % is one opportunity. A different
        bookmaker for any outcome is different bet instructions, hence a different opportunity.
        """
        return f"{self.identity}/" + "|".join(f"{leg.outcome}@{leg.bookmaker_key}" for leg in self.legs)

    @property
    def odds_age_seconds(self) -> float | None:
        """Age of the OLDEST leg price when the arb was found (None if a price has no timestamp)."""
        stamps = [leg.odds_updated for leg in self.legs]
        if not stamps or any(t is None for t in stamps):
            return None
        return max(0.0, (self.found_at - min(stamps)).total_seconds())  # type: ignore[type-var]

    @property
    def push_possible(self) -> bool:
        """Whole-number totals/spreads lines can be refunded ("push"), voiding the guarantee."""
        from odds_scanner.markets import PUSH_MARKETS

        base = self.market.partition("@")[0]
        return base in PUSH_MARKETS and self.line is not None and float(self.line).is_integer()

    @property
    def identity(self) -> str:
        """Same opportunity regardless of the exact prices (event, market, line)."""
        return f"{self.event_id}/{self.market}/{self.line}"

    @property
    def dedupe_key(self) -> str:
        """Identity of this opportunity: same event/market/line, bookmakers and odds."""
        legs = "|".join(f"{leg.outcome}@{leg.bookmaker_key}:{leg.odds:.3f}" for leg in self.legs)
        return f"{self.event_id}/{self.market}/{self.line}/{legs}"


@dataclass(frozen=True)
class NearMissLeg:
    """Best price for one outcome of a near miss (no stake: nothing is to be bet)."""

    outcome: str
    bookmaker_key: str
    bookmaker_title: str
    odds: float
    effective_odds: float | None = None  # after the bookmaker's stake fee / win tax (None = same as odds)
    odds_updated: datetime | None = None
    url: str | None = None


@dataclass(frozen=True)
class NearMiss:
    """The best cross-bookmaker combination for a market that is *not* a profitable arbitrage.

    ``profit_percent`` is what the arbitrage maths would give: negative means that backing every
    outcome at these prices loses that % of the total stake; between 0 and ``min_profit_percent``
    it is a real but too-small arbitrage. The closer to 0, the closer the prices are to an arb.
    """

    event_id: str
    sport_key: str
    event_name: str
    commence_time: datetime
    market: str
    line: float | None
    legs: tuple[NearMissLeg, ...]
    inverse_sum: float
    profit_percent: float
    found_at: datetime
    home_team: str | None = None
    away_team: str | None = None

    @property
    def identity(self) -> str:
        return f"{self.event_id}/{self.market}/{self.line}"

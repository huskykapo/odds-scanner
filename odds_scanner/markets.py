"""Market keys, periods, outcome codes and labels shared by providers, matcher, finder and UI.

Market keys
-----------
``h2h``            two-way winner (tennis; or The Odds API ``h2h``, which includes overtime)
``h2h_3_way``      1X2 three-way result, outcomes ``1`` / ``X`` / ``2``
``double_chance``  outcomes ``1X`` / ``12`` / ``X2``. Never an arb on its own: the finder pairs
                   each double chance with the opposite 1X2 outcome (1X vs 2, X2 vs 1, 12 vs X)
``totals``         ``Over`` / ``Under`` with the line in ``Outcome.point``

Periods
-------
A key may carry a period suffix: ``h2h_3_way@reg`` is the **regulation-time** result (hockey and
basketball "Zápas" at Slovak bookmakers - a draw is possible). Keys with different periods are
different markets for the finder, so regulation time is never combined with full time including
overtime.
"""

from __future__ import annotations

import math

H2H = "h2h"
H2H_3_WAY = "h2h_3_way"
DOUBLE_CHANCE = "double_chance"
TOTALS = "totals"

REGULATION = "reg"

# Outcome codes used by the Slovak providers (and by merged events).
HOME, DRAW, AWAY = "1", "X", "2"
OVER, UNDER = "Over", "Under"
DC_CODES = ("1X", "12", "X2")

# Derived two-way markets: (double chance code, the 1X2 outcome it does not cover).
DC_PAIRS = {
    "dc_1x_2": ("1X", AWAY),
    "dc_x2_1": ("X2", HOME),
    "dc_12_x": ("12", DRAW),
}

SPORTS = ("football", "hockey", "basketball", "tennis")
# Sports whose Slovak "Zápas" 1X2 is decided in regulation time (overtime not included).
REGULATION_SPORTS = frozenset({"hockey", "basketball"})
# Sports without a draw: the main market is a two-way winner.
TWO_WAY_SPORTS = frozenset({"tennis"})

_ODDS_API_PREFIX = {"soccer": "football", "icehockey": "hockey", "basketball": "basketball", "tennis": "tennis"}

# Prices at or below this are treated as unusable (suspended books often show 1.00/1.01).
MIN_ODDS = 1.01


def with_period(key: str, period: str | None) -> str:
    return f"{key}@{period}" if period else key


def split_period(key: str) -> tuple[str, str | None]:
    base, _, period = key.partition("@")
    return base, (period or None)


def period_for(sport: str) -> str | None:
    """Period of the main result market a Slovak bookmaker offers for ``sport``."""
    return REGULATION if sport in REGULATION_SPORTS else None


def sport_family(sport_key: str) -> str:
    """``soccer_epl`` -> ``football``; Slovak provider keys are returned unchanged."""
    if sport_key in SPORTS:
        return sport_key
    return _ODDS_API_PREFIX.get(sport_key.split("_", 1)[0], sport_key)


def usable_price(value: object) -> float | None:
    """Return the price as float, or None when it is missing, invalid or <= ``MIN_ODDS``."""
    if isinstance(value, bool):
        return None
    try:
        price = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= MIN_ODDS:
        return None
    return price


def effective_odds(odds: float, stake_fee_percent: float = 0.0, win_tax_percent: float = 0.0) -> float:
    """Decimal odds you really get after a stake fee and a tax on net winnings.

    With stake ``S``: the bet placed is ``S * (1 - fee)``, it pays ``S * (1 - fee) * odds``, and
    the tax takes ``tax`` of the net win (payout minus ``S``).
    """
    gross = odds * (1.0 - stake_fee_percent / 100.0)
    if gross <= 1.0:
        return gross
    return 1.0 + (gross - 1.0) * (1.0 - win_tax_percent / 100.0)


_BASE_LABELS = {
    H2H: "Winner",
    H2H_3_WAY: "1X2",
    DOUBLE_CHANCE: "Double chance",
    "dc_1x_2": "1X vs 2",
    "dc_x2_1": "X2 vs 1",
    "dc_12_x": "12 vs X",
}


def market_title(key: str, line: float | None = None) -> str:
    """Human label: ``h2h_3_way@reg`` -> ``1X2 (regulation time)``, totals -> ``Over/Under 2.5``."""
    base, period = split_period(key)
    if base == TOTALS and line is not None:
        label = f"Over/Under {line:g}"
    elif base == "spreads" and line is not None:
        label = f"spreads (home {line:+g})"
    else:
        label = _BASE_LABELS.get(base, base)
    if period == REGULATION:
        label += " (regulation time)"
    elif period:
        label += f" ({period})"
    return label


def outcome_title(outcome: str, home: str | None, away: str | None, line: float | None = None) -> str:
    """``1`` -> ``1 (Košice)``, ``X`` -> ``X (draw)``, ``Over`` -> ``Over 2.5``."""
    if outcome == HOME and home:
        return f"1 ({home})"
    if outcome == AWAY and away:
        return f"2 ({away})"
    if outcome == DRAW:
        return "X (draw)"
    if outcome in (OVER, UNDER) and line is not None:
        return f"{outcome} {line:g}"
    return outcome

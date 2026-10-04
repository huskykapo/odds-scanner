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
# Markets read from the match-detail pages (and MONACObet's list). Lines are the HOME side's.
TEAM_TOTALS_HOME = "team_totals_home"  # Over/Under, goals of the home team
TEAM_TOTALS_AWAY = "team_totals_away"
BTTS = "btts"  # both teams to score: Yes/No
ODD_EVEN = "odd_even"  # number of goals: Odd/Even
DRAW_NO_BET = "draw_no_bet"  # 1/2, stake returned on a draw
HANDICAP = "handicap"  # two-way goal handicap: 1/2, line = home handicap (-1.5 = home gives 1.5)
HANDICAP_3WAY = "handicap_3way"  # European handicap "0:1": 1/X/2, line = home goals minus away start
FIRST_GOAL = "first_goal"  # who scores first: 1 / X (no goal) / 2
MOST_CORNERS = "corners_1x2"  # who takes more corners: 1/X/2

REGULATION = "reg"
FIRST_HALF = "1h"
SECOND_HALF = "2h"

# Outcome sets the finder accepts per base market (they must be exhaustive and exclusive).
LINE_MARKETS = frozenset({TOTALS, TEAM_TOTALS_HOME, TEAM_TOTALS_AWAY, HANDICAP, HANDICAP_3WAY})
EXTRA_TWO_WAY = frozenset({TEAM_TOTALS_HOME, TEAM_TOTALS_AWAY, BTTS, ODD_EVEN, DRAW_NO_BET, HANDICAP})
THREE_WAY_ONLY = frozenset({HANDICAP_3WAY, FIRST_GOAL, MOST_CORNERS})
PUSH_MARKETS = frozenset({TOTALS, "spreads", TEAM_TOTALS_HOME, TEAM_TOTALS_AWAY, HANDICAP})  # whole lines can refund
YES, NO, ODD, EVEN = "Yes", "No", "Odd", "Even"

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
    BTTS: "Both teams to score",
    ODD_EVEN: "Odd/even goals",
    DRAW_NO_BET: "Draw no bet",
    FIRST_GOAL: "First goal",
    MOST_CORNERS: "Most corners",
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
    elif base == TEAM_TOTALS_HOME and line is not None:
        label = f"Home team goals over/under {line:g}"
    elif base == TEAM_TOTALS_AWAY and line is not None:
        label = f"Away team goals over/under {line:g}"
    elif base == HANDICAP and line is not None:
        label = f"Handicap (home {line:+g})"
    elif base == HANDICAP_3WAY and line is not None:
        label = f"3-way handicap (home {line:+g})"
    elif base == "spreads" and line is not None:
        label = f"spreads (home {line:+g})"
    else:
        label = _BASE_LABELS.get(base, base)
    if period == REGULATION:
        label += " (regulation time)"
    elif period == FIRST_HALF:
        label += " (1st half)"
    elif period == SECOND_HALF:
        label += " (2nd half)"
    elif period:
        label += f" ({period})"
    return label


def outcome_title(
    outcome: str, home: str | None, away: str | None, line: float | None = None, market: str | None = None
) -> str:
    """``1`` -> ``1 (Košice)``, ``X`` -> ``X (draw)``, ``Over`` -> ``Over 2.5``."""
    base = split_period(market)[0] if market else None
    if base == HANDICAP and line is not None and outcome in (HOME, AWAY):
        team, hcp = (home, line) if outcome == HOME else (away, -line)
        return f"{outcome} ({team or ('home' if outcome == HOME else 'away')} {hcp:+g})"
    if base == FIRST_GOAL and outcome == DRAW:
        return "X (no goal)"
    if outcome == HOME and home:
        return f"1 ({home})"
    if outcome == AWAY and away:
        return f"2 ({away})"
    if outcome == DRAW:
        return "X (draw)"
    if outcome in (OVER, UNDER) and line is not None:
        return f"{outcome} {line:g}"
    return outcome

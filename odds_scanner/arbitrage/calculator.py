"""Pure arbitrage math: detection, profit percentage and stake splitting.

All odds are *decimal* odds (payout per unit staked, stake included).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

# Float tolerance: three-way odds of exactly 3.0 must not look like an arb
# because 3 * (1/3) rounds to 0.9999999999999999.
EPSILON = 1e-12


class InvalidOddsError(ValueError):
    """Odds are not usable for arbitrage maths (<= 1, NaN, infinite, not a number)."""


def validate_odds(odds: Sequence[float]) -> None:
    """Raise unless ``odds`` describes at least two outcomes, each with finite decimal odds > 1."""
    if len(odds) < 2:
        raise InvalidOddsError(f"an arbitrage needs at least two outcomes, got {len(odds)}")
    for o in odds:
        if isinstance(o, bool) or not isinstance(o, (int, float)):
            raise InvalidOddsError(f"odds must be numbers, got {o!r}")
        if not math.isfinite(o) or o <= 1.0:
            raise InvalidOddsError(f"decimal odds must be finite and greater than 1, got {o!r}")


def inverse_sum(odds: Sequence[float]) -> float:
    """Sum of implied probabilities, ``sum(1/odds)``. Below 1 means an arbitrage exists."""
    validate_odds(odds)
    return math.fsum(1.0 / o for o in odds)


def is_arbitrage(odds: Sequence[float]) -> bool:
    """True if backing every outcome at these odds guarantees a profit."""
    return inverse_sum(odds) < 1.0 - EPSILON


def profit_percent(odds: Sequence[float]) -> float:
    """Guaranteed profit as % of total stake: ``(1 / sum(1/odds) - 1) * 100`` (negative if no arb)."""
    return (1.0 / inverse_sum(odds) - 1.0) * 100.0


@dataclass(frozen=True)
class StakePlan:
    """Stakes for one arbitrage, as actually placed (i.e. after rounding)."""

    odds: tuple[float, ...]
    stakes: tuple[float, ...]
    payouts: tuple[float, ...]  # stake * odds, per outcome
    total_stake: float
    profits: tuple[float, ...]  # payout - total_stake, per outcome
    guaranteed_profit: float  # worst case: min(profits)
    profit_percent: float  # guaranteed_profit / total_stake * 100


def _round_to_unit(value: float, unit: float) -> float:
    """Round ``value`` half-up to a multiple of ``unit`` without float noise (e.g. 0.01)."""
    d_unit = Decimal(str(unit))
    steps = (Decimal(str(value)) / d_unit).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(steps * d_unit)


def calculate_stakes(odds: Sequence[float], bankroll: float, rounding: float = 0.01) -> StakePlan:
    """Split ``bankroll`` over the outcomes so every outcome pays (almost) the same.

    Ideal stake: ``bankroll * (1/odds_i) / sum(1/odds)``. Stakes are then rounded to a
    multiple of ``rounding`` (a bookmaker-friendly unit such as 1.0 or 0.5) and payouts,
    total stake and profit are **recalculated from the rounded stakes**, so the returned
    numbers describe what you would really place. Rounding can shrink the total stake
    below the bankroll and can push the profit down, even negative for tiny bankrolls.

    This works for any valid odds; it does not require that an arbitrage exists
    (``guaranteed_profit`` is then <= 0).
    """
    if isinstance(bankroll, bool) or not isinstance(bankroll, (int, float)) or not math.isfinite(bankroll) or bankroll <= 0:
        raise ValueError(f"bankroll must be a positive finite number, got {bankroll!r}")
    if not isinstance(rounding, (int, float)) or not math.isfinite(rounding) or rounding <= 0:
        raise ValueError(f"rounding unit must be a positive finite number, got {rounding!r}")

    total_inv = inverse_sum(odds)  # validates odds
    stakes = tuple(_round_to_unit(bankroll * (1.0 / o) / total_inv, rounding) for o in odds)
    payouts = tuple(round(s * o, 6) for s, o in zip(stakes, odds))
    total_stake = round(math.fsum(stakes), 6)
    profits = tuple(round(p - total_stake, 6) for p in payouts)
    guaranteed = min(profits)
    pct = guaranteed / total_stake * 100.0 if total_stake > 0 else 0.0
    return StakePlan(
        odds=tuple(float(o) for o in odds),
        stakes=stakes,
        payouts=payouts,
        total_stake=total_stake,
        profits=profits,
        guaranteed_profit=guaranteed,
        profit_percent=pct,
    )

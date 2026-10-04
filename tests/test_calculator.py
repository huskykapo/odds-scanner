import math

import pytest

from odds_scanner.arbitrage.calculator import (
    InvalidOddsError,
    calculate_stakes,
    inverse_sum,
    is_arbitrage,
    profit_percent,
)


# ------------------------------------------------------------------ detection / profit
def test_two_way_arbitrage_detected():
    odds = [2.10, 2.05]
    assert inverse_sum(odds) == pytest.approx(1 / 2.10 + 1 / 2.05)
    assert is_arbitrage(odds)
    assert profit_percent(odds) == pytest.approx((1 / (1 / 2.10 + 1 / 2.05) - 1) * 100)
    assert profit_percent(odds) == pytest.approx(3.735, abs=0.001)


def test_two_way_no_arbitrage():
    assert not is_arbitrage([1.90, 1.90])
    assert profit_percent([1.90, 1.90]) < 0


def test_equal_odds_two_way_even_money_is_not_arbitrage():
    # 1/2 + 1/2 == 1 exactly: break-even, not an arb.
    assert inverse_sum([2.0, 2.0]) == 1.0
    assert not is_arbitrage([2.0, 2.0])
    assert profit_percent([2.0, 2.0]) == 0.0


def test_equal_odds_three_way_float_noise_is_not_arbitrage():
    # 3 * (1/3) is 0.9999999999999999 in floating point; must not be reported as an arb.
    assert not is_arbitrage([3.0, 3.0, 3.0])


def test_three_way_arbitrage():
    odds = [2.5, 3.6, 3.4]
    assert inverse_sum(odds) == pytest.approx(0.4 + 1 / 3.6 + 1 / 3.4)
    assert is_arbitrage(odds)
    assert profit_percent(odds) == pytest.approx(2.892, abs=0.001)


def test_three_way_no_arbitrage():
    assert not is_arbitrage([2.0, 3.2, 3.8])  # typical bookmaker with margin


def test_arbitrage_just_below_threshold():
    assert is_arbitrage([2.0, 2.0001])
    assert not is_arbitrage([2.0, 1.9999])


# ------------------------------------------------------------------ invalid input
@pytest.mark.parametrize("bad", [1.0, 0.5, 0.0, -2.0, math.nan, math.inf, -math.inf])
def test_invalid_odds_rejected(bad):
    with pytest.raises(InvalidOddsError):
        inverse_sum([2.0, bad])
    with pytest.raises(InvalidOddsError):
        is_arbitrage([bad, 2.0])
    with pytest.raises(InvalidOddsError):
        calculate_stakes([2.0, bad], 100)


@pytest.mark.parametrize("bad", ["2.0", None, True])
def test_non_numeric_odds_rejected(bad):
    with pytest.raises(InvalidOddsError):
        inverse_sum([2.0, bad])


@pytest.mark.parametrize("odds", [[], [2.5]])
def test_missing_outcomes_rejected(odds):
    """A lone outcome (the other side is missing) can never be an arbitrage."""
    with pytest.raises(InvalidOddsError, match="at least two"):
        is_arbitrage(odds)
    with pytest.raises(InvalidOddsError):
        calculate_stakes(odds, 100)


# ------------------------------------------------------------------ stake calculator
def test_stakes_sum_to_bankroll_and_equalise_payout_when_unrounded():
    odds = [2.10, 2.05]
    plan = calculate_stakes(odds, 1000, rounding=0.0001)
    assert plan.total_stake == pytest.approx(1000, abs=0.001)
    assert plan.payouts[0] == pytest.approx(plan.payouts[1], abs=0.2)
    s = 1 / 2.10 + 1 / 2.05
    assert plan.stakes[0] == pytest.approx(1000 * (1 / 2.10) / s, abs=1e-3)
    assert plan.profit_percent == pytest.approx(profit_percent(odds), abs=0.01)
    assert plan.guaranteed_profit == pytest.approx(1000 * (1 / s - 1), abs=0.2)


def test_stakes_rounded_to_whole_units_and_profit_recalculated():
    odds = [2.10, 2.05]
    plan = calculate_stakes(odds, 1000, rounding=1.0)
    assert all(float(s).is_integer() for s in plan.stakes)
    # Everything is recomputed from the *rounded* stakes.
    assert plan.total_stake == sum(plan.stakes)
    assert plan.payouts == tuple(round(s * o, 6) for s, o in zip(plan.stakes, odds))
    assert plan.profits == tuple(round(p - plan.total_stake, 6) for p in plan.payouts)
    assert plan.guaranteed_profit == min(plan.profits)
    assert plan.profit_percent == pytest.approx(plan.guaranteed_profit / plan.total_stake * 100)
    # Rounding costs at most a little versus the theoretical profit.
    assert plan.profit_percent == pytest.approx(profit_percent(odds), abs=0.2)


def test_rounding_to_half_units():
    plan = calculate_stakes([2.10, 2.05], 1000, rounding=0.5)
    assert all((s * 2).is_integer() for s in plan.stakes)


def test_default_rounding_is_cents():
    plan = calculate_stakes([2.10, 2.05], 123.45)
    assert all(round(s, 2) == s for s in plan.stakes)


def test_three_way_stakes():
    odds = [2.5, 3.6, 3.4]
    plan = calculate_stakes(odds, 1000, rounding=1.0)
    assert len(plan.stakes) == 3
    assert plan.total_stake == pytest.approx(1000, abs=2)
    assert plan.guaranteed_profit > 0
    assert min(plan.payouts) > plan.total_stake
    # higher odds -> smaller stake: 2.5 > 3.4 > 3.6
    assert plan.stakes[0] > plan.stakes[2] > plan.stakes[1] > 0


def test_stakes_without_arbitrage_show_a_loss():
    plan = calculate_stakes([1.90, 1.90], 100, rounding=1.0)
    assert plan.guaranteed_profit < 0
    assert plan.profit_percent < 0


def test_tiny_bankroll_rounding_to_zero_stake_is_reported_honestly():
    # 1.05 / 30 -> second stake rounds to 0; no hedge means a guaranteed-looking "profit" is gone.
    plan = calculate_stakes([1.05, 30.0], 5, rounding=10.0)
    assert plan.stakes[1] == 0.0
    assert plan.guaranteed_profit <= 0


def test_rounding_half_up_not_bankers():
    plan = calculate_stakes([2.0, 2.0], 5, rounding=1.0)  # ideal 2.5 / 2.5
    assert plan.stakes == (3.0, 3.0)


@pytest.mark.parametrize("bankroll", [0, -10, math.nan, math.inf, "100", None, True])
def test_invalid_bankroll_rejected(bankroll):
    with pytest.raises(ValueError, match="bankroll"):
        calculate_stakes([2.1, 2.1], bankroll)


@pytest.mark.parametrize("unit", [0, -1, math.nan, math.inf])
def test_invalid_rounding_unit_rejected(unit):
    with pytest.raises(ValueError, match="rounding"):
        calculate_stakes([2.1, 2.1], 100, rounding=unit)

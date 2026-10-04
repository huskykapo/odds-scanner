"""Finder behaviour specific to the Slovak markets: periods, double chance, fees, per-book stakes."""

from datetime import timedelta

import pytest

from odds_scanner.arbitrage import BookRule, FinderSettings, calculate_stakes, find_arbitrages
from odds_scanner.markets import effective_odds, market_title, outcome_title
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome
from tests.conftest import NOW

FRESH = NOW - timedelta(seconds=20)
SETTINGS = FinderSettings(bankroll=100, min_profit_percent=0.5, stake_rounding=0.5, max_profit_percent=25)


def book(key, *markets):
    return BookmakerOdds(
        key=key, title=key.title(), last_update=FRESH,
        markets=tuple(MarketOdds(k, tuple(Outcome(n, p, pt) for n, p, pt in outs), FRESH) for k, outs in markets),
        url=f"https://{key}.example/match",
    )


def three_way(key, home, draw, away, market="h2h_3_way"):
    return (market, [("1", home, None), ("X", draw, None), ("2", away, None)])


def event(*books, sport="football"):
    return Event("ev", sport, sport, NOW + timedelta(hours=3), "Home", "Away", tuple(books))


def run(ev, settings=SETTINGS):
    return find_arbitrages([ev], settings, now=NOW)


# ------------------------------------------------------------------ regulation time vs full time
def test_regulation_time_1x2_is_never_combined_with_full_time_markets():
    # a: hockey 1X2 in regulation time. b: 1X2 "incl. overtime" (hypothetical) and a two-way winner incl. OT.
    # Taken together the best prices would be a 10 %+ "arb" - but they are different bets.
    a = book("a", three_way("a", 2.6, 4.6, 2.6, "h2h_3_way@reg"))
    b = book("b", three_way("b", 3.0, 5.5, 3.0, "h2h_3_way"), ("h2h", [("1", 2.4, None), ("2", 2.4, None)]))
    assert run(event(a, b, sport="hockey")) == []


def test_regulation_time_1x2_arbs_with_other_regulation_time_quotes():
    a = book("a", three_way("a", 2.9, 4.0, 2.4, "h2h_3_way@reg"))
    b = book("b", three_way("b", 2.4, 4.6, 2.9, "h2h_3_way@reg"))
    (arb,) = run(event(a, b, sport="hockey"))
    assert arb.market == "h2h_3_way@reg"
    assert market_title(arb.market) == "1X2 (regulation time)"
    assert [(l.outcome, l.bookmaker_key) for l in arb.legs] == [("1", "a"), ("X", "b"), ("2", "b")]


def test_single_book_double_chance_arb_is_ignored():
    # 1X 1.30 + 2 6.0 at the same book would be an "arb", but both legs come from one bookmaker.
    a = book("a", three_way("a", 1.5, 4.0, 6.0), ("double_chance", [("1X", 1.30, None)]))
    b = book("b", three_way("b", 1.5, 4.0, 4.6))
    assert run(event(a, b)) == []


def test_double_chance_cross_book():
    # 1X at a = 1.40, 2 at b = 4.0 -> 1/1.4 + 1/4 = 0.964 -> 3.7 %
    a = book("a", three_way("a", 1.9, 3.5, 3.2), ("double_chance", [("1X", 1.40, None)]))
    b = book("b", three_way("b", 1.8, 3.4, 4.0))
    arbs = {a.market: a for a in run(event(a, b))}
    arb = arbs["dc_1x_2"]
    assert {(l.outcome, l.bookmaker_key, l.odds) for l in arb.legs} == {("1X", "a", 1.40), ("2", "b", 4.0)}
    assert arb.profit_percent == pytest.approx((1 / (1 / 1.4 + 1 / 4.0) - 1) * 100)
    assert market_title(arb.market) == "1X vs 2"


def test_double_chance_never_mixes_periods():
    a = book("a", ("double_chance@reg", [("1X", 1.40, None)]), three_way("a", 1.9, 3.5, 3.2, "h2h_3_way@reg"))
    b = book("b", three_way("b", 1.8, 3.4, 4.0))  # full time
    assert [x for x in run(event(a, b, sport="hockey")) if x.market.startswith("dc_")] == []


def test_incomplete_1x2_does_not_lend_outcomes_to_double_chance_pairs():
    a = book("a", ("double_chance", [("1X", 1.40, None)]))
    b = book("b", ("h2h_3_way", [("1", 1.8, None), ("2", 4.0, None)]))  # draw suspended
    assert run(event(a, b)) == []


# ------------------------------------------------------------------ fees and per-bookmaker stakes
def test_effective_odds():
    assert effective_odds(2.0) == 2.0
    assert effective_odds(2.0, stake_fee_percent=5) == pytest.approx(1.9)
    assert effective_odds(2.0, win_tax_percent=10) == pytest.approx(1.9)  # 1 + 1.0 * 0.9


def test_stake_fee_applied_before_comparing_kills_marginal_arb():
    a = book("a", ("h2h", [("1", 2.1, None), ("2", 1.9, None)]))
    b = book("b", ("h2h", [("1", 1.9, None), ("2", 2.1, None)]))
    ev = event(a, b, sport="tennis")
    assert len(run(ev)) == 1  # 1/2.1*2 = 0.952 -> 5 %
    fee = FinderSettings(**{**SETTINGS.__dict__, "book_rules": {"a": BookRule(stake_fee=10)}})
    assert run(ev, fee) == []
    (arb,) = run(ev, FinderSettings(**{**SETTINGS.__dict__, "book_rules": {"a": BookRule(stake_fee=1)}}))
    leg_a = next(l for l in arb.legs if l.bookmaker_key == "a")
    assert leg_a.odds == 2.1 and leg_a.effective_odds == pytest.approx(2.079)
    assert leg_a.payout == pytest.approx(leg_a.stake * 2.079)


def test_per_bookmaker_step_and_minimum_stake():
    plan = calculate_stakes([2.1, 2.1], 10, [0.5, 1.0], min_stakes=[0, 7])
    assert plan.stakes == (5.0, 7.0)
    assert plan.guaranteed_profit == pytest.approx(5 * 2.1 - 12)  # negative: minimum stake killed it


def test_rounding_that_kills_profit_drops_the_arb():
    a = book("a", ("h2h", [("1", 2.03, None), ("2", 1.9, None)]))
    b = book("b", ("h2h", [("1", 1.9, None), ("2", 2.03, None)]))
    ev = event(a, b, sport="tennis")
    assert len(run(ev)) == 1
    tiny = FinderSettings(**{**SETTINGS.__dict__, "bankroll": 3, "book_rules": {"a": BookRule(stake_step=1.0)}})
    assert run(ev, tiny) == []


def test_verify_manually_flag_and_leg_metadata():
    a = book("a", ("h2h", [("1", 2.4, None), ("2", 1.9, None)]))
    b = book("b", ("h2h", [("1", 1.9, None), ("2", 2.4, None)]))
    (arb,) = run(event(a, b, sport="tennis"), FinderSettings(**{**SETTINGS.__dict__, "verify_above_percent": 10}))
    assert arb.verify_manually  # 20 %: flagged, not dropped (only > max_profit_percent is dropped)
    assert arb.legs[0].url == "https://a.example/match" and arb.legs[0].odds_updated == FRESH


def test_outcome_titles():
    assert outcome_title("1", "Košice", "Komárno") == "1 (Košice)"
    assert outcome_title("X", "a", "b") == "X (draw)"
    assert outcome_title("Over", None, None, 2.5) == "Over 2.5"
    assert outcome_title("1X", "a", "b") == "1X"

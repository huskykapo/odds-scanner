"""Near misses: the closest cross-bookmaker combinations that are not profitable arbs."""

from dataclasses import replace
from datetime import timedelta

import pytest

from odds_scanner.arbitrage import FinderSettings, analyze_events, find_arbitrages
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome
from tests.conftest import NOW

SETTINGS = FinderSettings(bankroll=1000, min_profit_percent=1.0, stake_rounding=1.0)
FRESH = NOW - timedelta(seconds=30)


def mk_event(books, *, market="h2h", start=NOW + timedelta(hours=5), updated=None, eid="e1"):
    updated = updated or {}
    bms = tuple(
        BookmakerOdds(
            key=k, title=k.upper(), last_update=updated.get(k, FRESH),
            markets=(MarketOdds(key=market, last_update=updated.get(k, FRESH), outcomes=tuple(Outcome(*o) for o in outs)),),
        )
        for k, outs in books.items()
    )
    return Event(eid, "football", "Football", start, "H", "A", bms)


def run(events, floor=3.0, settings=SETTINGS):
    return analyze_events(events, settings, now=NOW, near_miss_floor=floor)


def profit(*odds):
    return (1 / sum(1 / o for o in odds) - 1) * 100


def test_losing_combination_inside_floor_is_a_near_miss_with_negative_profit():
    # best H 2.00 (a), best A 1.90 (b): inverse sum 1.0263 -> -2.56 %
    ev = mk_event({"a": [("H", 2.0), ("A", 1.8)], "b": [("H", 1.9), ("A", 1.9)]})
    arbs, near = run([ev])
    assert arbs == []
    (m,) = near
    assert m.profit_percent == pytest.approx(profit(2.0, 1.9)) and m.profit_percent < 0
    assert [(l.outcome, l.bookmaker_key, l.odds) for l in m.legs] == [("H", "a", 2.0), ("A", "b", 1.9)]
    assert m.inverse_sum == pytest.approx(1 / 2.0 + 1 / 1.9)
    assert m.identity == "e1/h2h/None" and m.found_at == NOW


def test_floor_limits_how_bad_a_near_miss_can_be():
    ev = mk_event({"a": [("H", 2.0), ("A", 1.8)], "b": [("H", 1.9), ("A", 1.9)]})  # -2.56 %
    assert len(run([ev], floor=3.0)[1]) == 1
    assert run([ev], floor=2.0)[1] == []


def test_profitable_but_below_threshold_arb_is_a_near_miss_not_an_arb():
    ev = mk_event({"a": [("H", 2.0), ("A", 1.8)], "b": [("H", 1.8), ("A", 2.01)]})
    p = profit(2.0, 2.01)  # +0.25 %: a real arb, but under the 1 % alert threshold
    assert 0 < p < SETTINGS.min_profit_percent
    arbs, near = run([ev])
    assert arbs == [] and [m.profit_percent for m in near] == [pytest.approx(p)]


def test_real_arb_is_reported_as_an_arb_only():
    ev = mk_event({"a": [("H", 2.2), ("A", 1.5)], "b": [("H", 1.5), ("A", 2.2)]})
    arbs, near = run([ev])
    assert len(arbs) == 1 and near == []


def test_arb_rejected_as_bad_data_is_not_a_near_miss():
    # ~ +66 % "arb": above max_profit_percent, so it is bad data, not a near miss
    ev = mk_event({"a": [("H", 3.0), ("A", 1.2)], "b": [("H", 1.2), ("A", 3.0)]})
    arbs, near = run([ev], settings=replace(SETTINGS, max_profit_percent=25.0))
    assert arbs == [] and near == []


def test_near_miss_obeys_data_quality_rules():
    books = {"a": [("H", 2.0), ("A", 1.8)], "b": [("H", 1.9), ("A", 1.9)]}
    assert run([mk_event(books, updated={"b": NOW - timedelta(hours=1)})])[1] == []  # stale price
    assert run([mk_event(books, start=NOW - timedelta(minutes=5))])[1] == []  # already started
    assert run([mk_event({"a": [("H", 2.0), ("A", 2.0)]})])[1] == []  # one bookmaker only
    assert run([mk_event({"a": [("H", 2.0), ("A", 2.0)], "b": [("H", 1.5), ("A", 1.5)]})])[1] == []  # best odds all from a
    # b omits the Draw: it must not pair with a's three-way market
    three = {"a": [("H", 3.0), ("Draw", 3.0), ("A", 3.0)], "b": [("H", 3.2), ("A", 3.2)]}
    assert run([mk_event(three)])[1] == []


def test_three_way_near_miss():
    ev = mk_event({"a": [("H", 2.9), ("Draw", 3.2), ("A", 2.2)], "b": [("H", 2.5), ("Draw", 3.1), ("A", 2.8)]})
    (m,) = run([ev])[1]
    assert [l.outcome for l in m.legs] == ["H", "Draw", "A"]
    assert m.profit_percent == pytest.approx(profit(2.9, 3.2, 2.8))


def test_disabled_by_default_and_find_arbitrages_unchanged():
    ev = mk_event({"a": [("H", 2.0), ("A", 1.8)], "b": [("H", 1.9), ("A", 1.9)]})
    assert analyze_events([ev], SETTINGS, now=NOW)[1] == []  # no floor -> no near misses computed
    assert find_arbitrages([ev], SETTINGS, now=NOW) == []


def test_sorted_closest_first_across_events():
    far = mk_event({"a": [("H", 1.95), ("A", 1.7)], "b": [("H", 1.7), ("A", 1.95)]}, eid="far")
    close = mk_event({"a": [("H", 2.0), ("A", 1.8)], "b": [("H", 1.8), ("A", 2.0)]}, eid="close")
    near = run([far, close], floor=10)[1]
    assert [m.event_id for m in near] == ["close", "far"]

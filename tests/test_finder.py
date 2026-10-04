from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from odds_scanner.arbitrage import FinderSettings, find_arbitrages, profit_percent
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome
from tests.conftest import NOW

SETTINGS = FinderSettings(bankroll=1000, min_profit_percent=1.0, stake_rounding=1.0)
FRESH = NOW - timedelta(seconds=30)


def run(events, settings=SETTINGS):
    return find_arbitrages(events, settings, now=NOW)


def by_event(arbs):
    return {(a.event_id, a.market, a.line): a for a in arbs}


# ------------------------------------------------------------------ sample data end-to-end
def test_sample_data_finds_exactly_the_real_arbs(sample_events):
    arbs = by_event(run(sample_events.values()))
    assert set(arbs) == {
        ("evt_football_arb", "h2h", None),
        ("evt_tennis_arb", "h2h", None),
        ("evt_nba_totals", "totals", 220.5),
        ("evt_nba_spreads", "spreads", -5.5),
    }


def test_results_sorted_by_profit_descending(sample_events):
    arbs = run(sample_events.values())
    pcts = [a.realized_profit_percent for a in arbs]
    assert pcts == sorted(pcts, reverse=True)
    assert [a.event_id for a in arbs][-2:] == ["evt_football_arb", "evt_nba_totals"]  # 2.89 %, 2.44 %


def test_three_way_football_arb_details(sample_events):
    arb = by_event(run(sample_events.values()))[("evt_football_arb", "h2h", None)]
    assert [(l.outcome, l.bookmaker_key, l.odds) for l in arb.legs] == [
        ("Northgate FC", "alphabet", 2.50),
        ("Draw", "betaplay", 3.60),
        ("Riverside United", "gammaodds", 3.40),
    ]
    assert arb.inverse_sum == pytest.approx(0.4 + 1 / 3.6 + 1 / 3.4)
    assert arb.profit_percent == pytest.approx(profit_percent([2.5, 3.6, 3.4]))
    assert arb.profit_percent == pytest.approx(2.892, abs=0.001)
    # stakes are whole units, payouts/profit recomputed from them
    assert all(l.stake == int(l.stake) for l in arb.legs)
    assert arb.total_stake == sum(l.stake for l in arb.legs)
    assert arb.guaranteed_profit == pytest.approx(min(l.payout for l in arb.legs) - arb.total_stake)
    assert arb.realized_profit_percent == pytest.approx(arb.guaranteed_profit / arb.total_stake * 100)
    assert arb.realized_profit_percent == pytest.approx(arb.profit_percent, abs=0.3)
    assert arb.found_at == NOW


def test_two_way_tennis_arb_details(sample_events):
    arb = by_event(run(sample_events.values()))[("evt_tennis_arb", "h2h", None)]
    assert [(l.outcome, l.bookmaker_key, l.odds) for l in arb.legs] == [
        ("Jan Kovac", "alphabet", 2.10),
        ("Luca Moretti", "betaplay", 2.05),
    ]
    assert arb.profit_percent == pytest.approx(3.735, abs=0.001)


def test_totals_not_paired_across_different_lines(sample_events):
    arbs = run(sample_events.values())
    totals = [a for a in arbs if a.market == "totals"]
    assert [a.line for a in totals] == [220.5]  # 224.5 has no arb; Over 224.5 + Under 220.5 is not one
    assert {l.outcome: l.odds for l in totals[0].legs} == {"Over": 2.00, "Under": 2.10}


def test_spreads_grouped_by_home_handicap(sample_events):
    arb = by_event(run(sample_events.values()))[("evt_nba_spreads", "spreads", -5.5)]
    assert {l.outcome: (l.bookmaker_key, l.odds) for l in arb.legs} == {
        "Capital Bears": ("alphabet", 2.05),
        "Delta Kings": ("betaplay", 2.10),
    }
    assert ("evt_nba_spreads", "spreads", -6.5) not in by_event(run(sample_events.values()))


@pytest.mark.parametrize(
    "event_id",
    [
        "evt_football_noarb",
        "evt_football_missing_draw",  # draw-less book must not create a fake two-way arb
        "evt_football_stale",  # arb exists only with 30-minute-old prices
        "evt_football_started",  # match already kicked off
        "evt_tennis_same_book",  # all best odds from one bookmaker
    ],
)
def test_traps_are_not_reported(sample_events, event_id):
    assert event_id not in {a.event_id for a in run(sample_events.values())}


def test_stale_arb_appears_when_staleness_limit_is_relaxed(sample_events):
    relaxed = replace(SETTINGS, stale_after=timedelta(hours=1))
    ids = {a.event_id for a in run(sample_events.values(), relaxed)}
    assert "evt_football_stale" in ids  # proves the staleness filter is what removed it


def test_started_event_reported_if_clock_is_earlier(sample_events):
    # Earlier "now" makes the prices look 2h old, so relax staleness to isolate the kickoff check.
    arbs = find_arbitrages(sample_events.values(), replace(SETTINGS, stale_after=timedelta(days=1)), now=NOW - timedelta(hours=2))
    assert "evt_football_started" in {a.event_id for a in arbs}


# ------------------------------------------------------------------ thresholds & whitelist
def test_min_profit_threshold(sample_events):
    arbs = run(sample_events.values(), replace(SETTINGS, min_profit_percent=3.0))
    assert {a.event_id for a in arbs} == {"evt_tennis_arb", "evt_nba_spreads"}  # 3.73 % each; totals 2.44 % cut
    assert run(sample_events.values(), replace(SETTINGS, min_profit_percent=50.0)) == []


def test_max_profit_percent_filters_suspected_bad_data(sample_events):
    assert {a.event_id for a in run(sample_events.values(), replace(SETTINGS, max_profit_percent=3.0))} == {
        "evt_football_arb", "evt_nba_totals"}
    assert run(sample_events.values(), replace(SETTINGS, max_profit_percent=None))  # disabled


def test_bookmaker_whitelist(sample_events):
    only_ab = replace(SETTINGS, bookmakers=frozenset({"alphabet", "betaplay"}))
    ids = {a.event_id for a in run(sample_events.values(), only_ab)}
    assert "evt_football_arb" not in ids  # needs gammaodds' 3.40
    assert {"evt_tennis_arb", "evt_nba_totals", "evt_nba_spreads"} <= ids
    assert run(sample_events.values(), replace(SETTINGS, bookmakers=frozenset({"alphabet"}))) == []


def test_tiny_bankroll_where_rounding_destroys_profit_is_dropped(sample_events):
    # With a 3-unit bankroll and whole-unit stakes, no hedge survives rounding.
    arbs = run(sample_events.values(), replace(SETTINGS, bankroll=3))
    assert all(a.guaranteed_profit > 0 for a in arbs)


def test_arbitrage_dedupe_key_changes_with_odds(sample_events):
    a = by_event(run(sample_events.values()))[("evt_tennis_arb", "h2h", None)]
    assert a.dedupe_key == a.dedupe_key
    other = replace(a, legs=(replace(a.legs[0], odds=2.12), a.legs[1]))
    assert other.dedupe_key != a.dedupe_key


def test_push_possible_flag_for_whole_number_lines(sample_events):
    a = by_event(run(sample_events.values()))[("evt_nba_totals", "totals", 220.5)]
    assert not a.push_possible
    assert replace(a, line=220.0).push_possible
    h2h = by_event(run(sample_events.values()))[("evt_tennis_arb", "h2h", None)]
    assert not h2h.push_possible


# ------------------------------------------------------------------ synthetic edge cases
def mk_event(books, *, market="h2h", start=NOW + timedelta(hours=5), home="H", away="A"):
    bms = tuple(
        BookmakerOdds(key=k, title=k.upper(), last_update=FRESH,
                      markets=(MarketOdds(key=market, last_update=FRESH,
                                          outcomes=tuple(Outcome(*o) for o in outs)),))
        for k, outs in books.items()
    )
    return Event("e1", "sport", "Sport", start, home, away, bms)


def test_tied_best_odds_prefers_cross_bookmaker_combination():
    ev = mk_event({"a": [("H", 2.1), ("A", 1.5)], "b": [("H", 2.1), ("A", 1.5)], "c": [("H", 1.5), ("A", 2.1)]})
    (arb,) = run([ev])
    assert {l.bookmaker_key for l in arb.legs} == {"a", "c"}  # tie on H between a/b; a chosen deterministically


def test_same_bookmaker_arb_ignored_unless_a_tie_allows_a_cross_bookmaker_split():
    # b ties a's best H price, so the arb can legitimately be placed across a and b ...
    ev = mk_event({"a": [("H", 2.2), ("A", 2.2)], "b": [("H", 2.2), ("A", 1.5)]})
    (arb,) = run([ev])
    assert {l.bookmaker_key for l in arb.legs} == {"a", "b"}
    # ... but if b is strictly worse on both sides, every best price is a's alone: ignored.
    ev2 = mk_event({"a": [("H", 2.2), ("A", 2.2)], "b": [("H", 1.8), ("A", 1.5)]})
    assert run([ev2]) == []


def test_equal_even_odds_two_way_is_not_an_arb():
    assert run([mk_event({"a": [("H", 2.0), ("A", 1.5)], "b": [("H", 1.5), ("A", 2.0)]})], replace(SETTINGS, min_profit_percent=0)) == []


def test_odds_at_or_below_one_are_discarded_as_bad_data():
    ev = mk_event({"a": [("H", 1.0), ("A", 50.0)], "b": [("H", 0.5), ("A", 1.2)]})
    assert run([ev]) == []  # the books quote invalid prices -> incomplete -> excluded


def test_invalid_price_from_one_book_cannot_hide_behind_other_books():
    ev = mk_event({"a": [("H", 2.3), ("A", 1.4)], "b": [("H", 1.4), ("A", 2.3)], "c": [("H", 9.0), ("A", 1.0)]})
    (arb,) = run([ev])
    assert {l.bookmaker_key for l in arb.legs} == {"a", "b"}  # c's 9.0 is ignored: its A price is invalid


def test_missing_outcome_in_two_way_market():
    ev = mk_event({"a": [("H", 2.5)], "b": [("A", 2.5)]})  # each book quotes one side only
    assert run([ev]) == []


def test_single_bookmaker_event_never_arbs():
    assert run([mk_event({"a": [("H", 3.0), ("A", 3.0)]})]) == []


def test_unknown_or_extra_outcomes_skip_market():
    ev = mk_event({"a": [("H", 3.0), ("A", 3.0), ("Draw", 3.0), ("Void", 3.0)], "b": [("H", 3.0), ("A", 3.0), ("Draw", 3.0), ("Void", 3.0)]})
    assert run([ev]) == []


def test_three_way_with_all_three_from_two_books():
    ev = mk_event({"a": [("H", 3.3), ("Draw", 3.0), ("A", 3.0)], "b": [("H", 3.0), ("Draw", 3.4), ("A", 3.4)]})
    (arb,) = run([ev])
    assert [l.outcome for l in arb.legs] == ["H", "Draw", "A"]
    assert arb.inverse_sum == pytest.approx(1 / 3.3 + 1 / 3.4 + 1 / 3.4)


def test_undated_and_stale_quotes_are_ignored():
    ev = mk_event({"a": [("H", 2.3), ("A", 1.4)], "b": [("H", 1.4), ("A", 2.3)]})
    undated = Event(**{**ev.__dict__, "bookmakers": tuple(
        BookmakerOdds(b.key, b.title, tuple(MarketOdds(m.key, m.outcomes, None) for m in b.markets), None)
        for b in ev.bookmakers)})
    assert run([undated]) == []
    assert len(run([ev])) == 1


def test_market_level_timestamp_overrides_bookmaker_timestamp():
    ev = mk_event({"a": [("H", 2.3), ("A", 1.4)], "b": [("H", 1.4), ("A", 2.3)]})
    b = ev.bookmakers[1]
    old_market = MarketOdds(b.markets[0].key, b.markets[0].outcomes, NOW - timedelta(hours=1))
    ev2 = Event(**{**ev.__dict__, "bookmakers": (ev.bookmakers[0], BookmakerOdds(b.key, b.title, (old_market,), FRESH))})
    assert run([ev2]) == []


def test_unsupported_market_ignored():
    assert run([mk_event({"a": [("H", 2.3), ("A", 1.4)], "b": [("H", 1.4), ("A", 2.3)]}, market="h2h_lay")]) == []


def test_totals_without_point_ignored():
    ev = mk_event({"a": [("Over", 2.2, None), ("Under", 1.5, None)], "b": [("Over", 1.5, None), ("Under", 2.2, None)]}, market="totals")
    assert run([ev]) == []


def test_no_events_and_default_now():
    assert find_arbitrages([], SETTINGS) == []
    future = datetime.now(timezone.utc) + timedelta(days=1)
    fresh = datetime.now(timezone.utc)
    bm = lambda k, h, a: BookmakerOdds(k, k, (MarketOdds("h2h", (Outcome("H", h), Outcome("A", a)), fresh),), fresh)
    ev = Event("x", "s", "S", future, "H", "A", (bm("a", 2.3, 1.4), bm("b", 1.4, 2.3)))
    assert len(find_arbitrages([ev], SETTINGS)) == 1  # uses real clock

"""Match pages (over/under, handicaps, BTTS, halves ...): parsers on real captures, mapping safety, engine."""

from datetime import datetime, timedelta, timezone

import pytest

from odds_scanner.arbitrage import FinderSettings, find_arbitrages
from odds_scanner.arbitrage.finder import analyze_events
from odds_scanner.engine import DetailSettings, LiveEngine, Source
from odds_scanner.errors import BlockedError
from odds_scanner.markets import market_title, outcome_title
from odds_scanner.matching import match_events
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome
from odds_scanner.providers.base import FetchResult, OddsProvider
from odds_scanner.providers.sk import DoxxbetProvider, MonacobetProvider
from odds_scanner.providers.sk.tipos import parse_detail_markets
from tests.conftest import load_sk

AT = datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc)  # capture time; kick-off 16:30 UTC


def by_key(markets):
    out = {}
    for m in markets:
        out.setdefault(m.key, {})[m.outcomes[0].point] = {o.name: o.price for o in m.outcomes}
    return out


@pytest.fixture(scope="module")
def doxx():
    return by_key(DoxxbetProvider.parse_detail(load_sk("doxxbet_detail.json"), "football", AT))


@pytest.fixture(scope="module")
def synot():
    return by_key(parse_detail_markets(load_sk("synot_detail.json"), "football", AT, 3771734))


# ------------------------------------------------------------------ parsers (real data, same match)
def test_doxxbet_match_page(doxx):
    assert doxx["totals"][2.5] == {"Over": 2.13, "Under": 1.73}
    assert doxx["btts"][None] == {"Yes": 1.85, "No": 1.93}
    assert doxx["handicap"][-1.5] == {"1": 4.8, "2": 1.17}  # "Handicap góly -1.5/+1.5": home -1.5
    assert doxx["handicap_3way"][-1.0] == {"1": 5.0, "X": 4.2, "2": 1.53}  # "0:1"
    assert doxx["team_totals_home"][1.5] == {"Over": 2.45, "Under": 1.53}
    assert doxx["h2h_3_way@1h"][None] == {"1": 3.15, "X": 2.13, "2": 3.7}
    assert doxx["double_chance@1h"][None] == {"1X": 1.27, "X2": 1.35, "12": 1.7}
    assert doxx["first_goal"][None] == {"1": 1.93, "X": 9.5, "2": 2.15}
    assert not any(k.startswith(("scorer", "cards")) for k in doxx)  # goalscorer / cards ignored
    assert len(doxx) >= 20


def test_synot_match_page(synot):
    assert synot["btts"][None] == {"Yes": 1.8, "No": 1.87}
    assert synot["handicap"][-1.5] == {"1": 4.68, "2": 1.17}  # "Tím 1 (-1.5)" / "Tím 2 (+1.5)"
    assert synot["handicap_3way"][-1.0] == {"1": 4.99, "X": 4.19, "2": 1.54}  # "Handicap 0:1"
    assert synot["team_totals_home"][1.5] == {"Under": 1.5, "Over": 2.41}
    assert synot["h2h_3_way@1h"][None] == {"1": 3.06, "X": 2.07, "2": 3.58}
    assert 0.75 not in synot["totals"] and 1.25 not in synot.get("handicap", {})  # quarter lines skipped
    assert synot["corners_1x2"][None] == {"1": 1.69, "X": 7.76, "2": 2.56}


def test_every_mapped_market_agrees_between_bookmakers(doxx, synot):
    """Same match, same market and line: prices must be close. A swapped mapping (over/under,
    home/away, handicap sign) would make the combined prices look like a large fake arbitrage."""
    compared = 0
    for key, lines in synot.items():
        for line, prices in lines.items():
            other = doxx.get(key, {}).get(line)
            if not other or set(other) != set(prices) or len(prices) < 2:
                continue
            best = {o: max(prices[o], other[o]) for o in prices}
            margin = 1 / sum(1 / p for p in best.values()) - 1
            assert margin < 0.03, (key, line, prices, other)  # combined, still a bookmaker margin
            compared += 1
    assert compared >= 25


def test_monacobet_team_goal_codes_are_under_first():
    payload = {"esMatches": [{
        "id": 1, "home": "A", "away": "B", "kickOffTime": 1791660000000, "brMatchId": 9, "live": False, "blocked": False,
        "betMap": {"1": {"NULL": {"ov": 2.0}}, "2": {"NULL": {"ov": 3.4}}, "3": {"NULL": {"ov": 3.6}},
                   "355": {"total=1.5": {"ov": 1.5, "sv": "total=1.5"}}, "356": {"total=1.5": {"ov": 2.4, "sv": "total=1.5"}},
                   "272": {"NULL": {"ov": 1.8}}, "273": {"NULL": {"ov": 1.9}},
                   "4": {"NULL": {"ov": 3.0}}, "5": {"NULL": {"ov": 2.1}}, "6": {"NULL": {"ov": 3.6}}}}]}
    (ev,) = MonacobetProvider.parse(payload, "football", AT)
    m = by_key(ev.bookmakers[0].markets)
    assert m["team_totals_home"][1.5] == {"Under": 1.5, "Over": 2.4}
    assert m["btts"][None] == {"Yes": 1.8, "No": 1.9} and m["h2h_3_way@1h"][None] == {"1": 3.0, "X": 2.1, "2": 3.6}
    (hockey,) = MonacobetProvider.parse(payload, "hockey", AT)
    assert {mk.key for mk in hockey.bookmakers[0].markets} == {"h2h_3_way@reg"}  # extras: football only


# ------------------------------------------------------------------ finder on the new markets
def book(key, *markets):
    return BookmakerOdds(key, key.title(), tuple(MarketOdds(k, tuple(Outcome(n, p, pt) for n, p, pt in outs), AT) for k, outs in markets), AT)


def ev(*books):
    return Event("e", "football", "Football", AT + timedelta(hours=2), "Home", "Away", tuple(books))


S = FinderSettings(bankroll=100, min_profit_percent=0.5, stake_rounding=0.5)


def test_btts_and_handicap_arbs_and_labels():
    a = book("a", ("btts", [("Yes", 2.1, None), ("No", 1.8, None)]), ("handicap", [("1", 2.2, -0.5), ("2", 1.7, -0.5)]))
    b = book("b", ("btts", [("Yes", 1.8, None), ("No", 2.1, None)]), ("handicap", [("1", 1.7, -0.5), ("2", 2.2, -0.5)]))
    arbs = {x.market: x for x in find_arbitrages([ev(a, b)], S, now=AT)}
    assert set(arbs) == {"btts", "handicap"}
    h = arbs["handicap"]
    assert h.line == -0.5 and market_title(h.market, h.line) == "Handicap (home -0.5)"
    assert [outcome_title(l.outcome, "Home", "Away", h.line, h.market) for l in h.legs] == ["1 (Home -0.5)", "2 (Away +0.5)"]


def test_handicap_lines_and_periods_never_mix():
    a = book("a", ("handicap", [("1", 2.6, -1.5), ("2", 1.5, -1.5)]), ("totals@1h", [("Over", 2.6, 1.5), ("Under", 1.5, 1.5)]))
    b = book("b", ("handicap", [("1", 1.5, 1.5), ("2", 2.6, 1.5)]), ("totals", [("Over", 1.5, 1.5), ("Under", 2.6, 1.5)]))
    assert find_arbitrages([ev(a, b)], S, now=AT) == []


def test_three_way_handicap_needs_all_three_outcomes():
    a = book("a", ("handicap_3way", [("1", 5.0, -1.0), ("2", 1.9, -1.0)]))  # draw missing
    b = book("b", ("handicap_3way", [("1", 4.0, -1.0), ("X", 4.2, -1.0), ("2", 1.5, -1.0)]))
    assert find_arbitrages([ev(a, b)], S, now=AT) == []


def test_whole_line_handicap_flags_push_risk():
    a = book("a", ("handicap", [("1", 2.2, -1.0), ("2", 1.7, -1.0)]))
    b = book("b", ("handicap", [("1", 1.7, -1.0), ("2", 2.2, -1.0)]))
    (arb,) = find_arbitrages([ev(a, b)], S, now=AT)
    assert arb.push_possible


# ------------------------------------------------------------------ engine: fetching match pages
class ListOnly(OddsProvider):
    def __init__(self, key, events):
        self.events, self.key = events, key

    def fetch_odds(self, sport, **kw):
        return FetchResult(events=list(self.events))


class WithPages(ListOnly):
    def __init__(self, key, events, pages, fail=None):
        super().__init__(key, events)
        self.pages, self.fail, self.calls = pages, fail, []

    def fetch_detail(self, native_id, sport):
        self.calls.append(native_id)
        if self.fail:
            raise self.fail
        return self.pages.get(native_id, [])


def single(key, eid, home, away, start, br, *markets):
    return Event(f"{key}:{eid}", "football", "Football", start, home, away,
                 (BookmakerOdds(key, key.title(), tuple(markets), AT, event_name=f"{home} - {away}"),), br)


def three_way(p1, px, p2):
    return MarketOdds("h2h_3_way", (Outcome("1", p1), Outcome("X", px), Outcome("2", p2)), AT)


def test_engine_fetches_match_pages_for_shared_matches_and_finds_detail_arbs():
    soon, late = AT + timedelta(hours=3), AT + timedelta(days=3)
    mono = ListOnly("monacobet", [
        single("monacobet", 1, "Las Palmas", "Valladolid", soon, "72477464", three_way(2.45, 3.18, 2.97),
               MarketOdds("btts", (Outcome("Yes", 2.2), Outcome("No", 1.6)), AT)),
        single("monacobet", 2, "Far", "Away", late, "1", three_way(2.0, 3.3, 3.8)),
    ])
    doxx_events = [
        single("doxxbet", 78181526, "Las Palmas", "Valladolid", soon, "72477464", three_way(2.5, 3.2, 3.0)),
        single("doxxbet", 99, "Far", "Away", late, "1", three_way(2.0, 3.3, 3.8)),  # 3 days away: no page
        single("doxxbet", 77, "Only", "Here", soon, "555", three_way(2.0, 3.3, 3.8)),  # one bookmaker: no page
    ]
    page = [MarketOdds("btts", (Outcome("Yes", 1.6), Outcome("No", 2.3)), AT)]
    doxx = WithPages("doxxbet", doxx_events, {"78181526": page})
    eng = LiveEngine([Source("monacobet", "MONACObet", mono, ["football"], 120), Source("doxxbet", "DOXXbet", doxx, ["football"], 60)],
                     S, detail_settings=DetailSettings(enabled=True), clock=lambda: AT)
    arbs = eng.run_once()
    assert doxx.calls == ["78181526"]
    (arb,) = [a for a in arbs if a.market == "btts"]  # 1/2.2 + 1/2.3 = 0.889 -> 12.4 %
    assert {(l.outcome, l.bookmaker_key) for l in arb.legs} == {("Yes", "monacobet"), ("No", "doxxbet")}
    assert eng.states["doxxbet"].details == 1
    assert eng.snapshot()["providers"][1]["details"] == 1


def test_match_page_refresh_and_blocking():
    soon = AT + timedelta(hours=3)
    clock = {"now": AT}
    mono = ListOnly("monacobet", [single("monacobet", 1, "A", "B", soon, "7", three_way(2.0, 3.3, 3.8))])
    doxx = WithPages("doxxbet", [single("doxxbet", 5, "A", "B", soon, "7", three_way(2.0, 3.3, 3.8))], {"5": []})
    src = Source("doxxbet", "DOXXbet", doxx, ["football"], 60)
    eng = LiveEngine([Source("monacobet", "MONACObet", mono, ["football"], 120), src], S,
                     detail_settings=DetailSettings(enabled=True, refresh=timedelta(seconds=240)), clock=lambda: clock["now"])
    eng.run_once()
    assert eng._next_detail("doxxbet", clock["now"]) is None  # just fetched
    clock["now"] = AT + timedelta(seconds=241)
    assert eng._next_detail("doxxbet", clock["now"]) == ("doxxbet:5", "football")
    doxx.fail = BlockedError("DOXXbet: HTTP 403")
    assert eng.fetch_detail(src, "doxxbet:5", "football") is None
    assert eng.states["doxxbet"].status == "blocked"


def test_real_captures_produce_no_fake_arbs():
    """DOXXbet and Synot match pages of the same match, merged like the app does: no arb appears."""
    d = DoxxbetProvider.parse_detail(load_sk("doxxbet_detail.json"), "football", AT)
    s = parse_detail_markets(load_sk("synot_detail.json"), "football", AT, 3771734)
    start = AT + timedelta(minutes=30)
    e1 = single("doxxbet", 78181526, "Las Palmas", "Real Valladolid", start, "72477464", *d)
    e2 = single("synot", 3771734, "UD Las Palmas", "Real Valladolid", start, "72477464", *s)
    merged, _ = match_events([e1, e2])
    arbs, near = analyze_events(merged, FinderSettings(bankroll=100, min_profit_percent=0.0, stake_rounding=0.01), now=AT,
                                near_miss_floor=50)
    assert arbs == []
    assert max(n.profit_percent for n in near) < 0
    from odds_scanner.arbitrage.finder import collect_quotes

    both = {key for (key, _), books in collect_quotes(merged[0], FinderSettings(stale_after=timedelta(hours=1)), AT).items()
            if len(books) >= 2}
    assert len(both) >= 25  # this many market types really are quoted by both bookmakers
    assert {"btts", "totals@2h", "handicap_3way@1h", "first_goal", "corners_1x2", "dc_1x_2@1h"} <= both

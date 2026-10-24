"""OddsPapi provider (Stake / Roobet / MyStake): parsing, request budget, key safety. All offline.

The answers below follow the structure in the vendor's public docs (bookmakerOdds -> markets -> outcomes
-> players["0"].price); no real answer has been seen yet, so these pin the documented format.
"""

import json
from datetime import date, timedelta

import pytest

from odds_scanner.errors import AuthenticationError, ConfigError, ProviderError, QuotaExhaustedError, RateLimitError
from odds_scanner.markets import H2H, H2H_3_WAY
from odds_scanner.providers.oddspapi import OddsPapiProvider, RequestBudget, fixture_names, outcome_codes, parse_odds
from tests.conftest import NOW, FakeResponse, FakeSession

KEY = "secret-key-123"
START = (NOW + timedelta(hours=5)).isoformat().replace("+00:00", "Z")


def book(prices, *, active=True, market="101", first=101, link=None):
    outcomes = {str(first + i): {"players": {"0": {"price": p, "active": True}}} for i, p in enumerate(prices)}
    node = {"bookmakerIsActive": active, "markets": {market: {"outcomes": outcomes}}}
    if link:
        node["fixturePath"] = link
    return node


def fixture(fid="id100", *, names=True, status=0, books=None, start=START):
    fx = {"fixtureId": fid, "participant1Id": 1, "participant2Id": 2, "sportId": 10, "tournamentId": 17,
          "statusId": status, "hasOdds": True, "startTime": start,
          "bookmakerOdds": books if books is not None else {"stake": book([2.1, 3.4, 3.6]), "roobet": book([2.0, 3.5, 3.9])}}
    if names:
        fx.update(participant1Name="Arsenal", participant2Name="Chelsea")
    return fx


# ------------------------------------------------------------------ parsing
def test_parses_documented_structure_into_one_event_with_each_bookmaker():
    (ev,), nameless = parse_odds([fixture()], sport="football", market_id=101, fetched_at=NOW)
    assert nameless == 0
    assert (ev.id, ev.home_team, ev.away_team, ev.sport_key) == ("oddspapi:id100", "Arsenal", "Chelsea", "football")
    assert [b.key for b in ev.bookmakers] == ["stake", "roobet"]
    stake = ev.bookmakers[0]
    assert stake.title == "Stake" and stake.url.startswith("https://stake.com")
    (market,) = stake.markets
    assert market.key == H2H_3_WAY
    assert [(o.name, o.price) for o in market.outcomes] == [("1", 2.1), ("X", 3.4), ("2", 3.6)]


def test_wrapped_answer_and_deep_link_are_accepted():
    books = {"mystake": book([2.0, 3.0, 4.0], link="https://mystake.com/sport/123")}
    (ev,), _ = parse_odds({"data": [fixture(books=books)]}, sport="football", market_id=101, fetched_at=NOW)
    assert ev.bookmakers[0].url == "https://mystake.com/sport/123" and ev.bookmakers[0].title == "MyStake"


def test_skips_live_fixtures_inactive_books_inactive_prices_and_incomplete_markets():
    broken = book([2.0, 3.0, 4.0])
    broken["markets"]["101"]["outcomes"]["102"]["players"]["0"]["active"] = False  # suspended draw
    books = {"stake": book([2.0, 3.0, 4.0], active=False), "roobet": broken, "mystake": book([1.01, 3.0, 4.0])}
    events, _ = parse_odds([fixture(books=books), fixture("live", status=1)], sport="football", market_id=101, fetched_at=NOW)
    assert events == []  # a 1X2 with a missing / unusable outcome is never turned into a fake two-way


def test_names_come_from_the_fixture_list_when_the_odds_answer_has_none():
    data = [fixture(names=False)]
    events, nameless = parse_odds(data, sport="football", market_id=101, fetched_at=NOW)
    assert events == [] and nameless == 1
    names = fixture_names([{"fixtureId": "id100", "participant1Name": "Arsenal", "participant2Name": "Chelsea"}])
    (ev,), nameless = parse_odds(data, sport="football", market_id=101, fetched_at=NOW, names=names)
    assert nameless == 0 and ev.name == "Arsenal vs Chelsea"


def test_only_requested_bookmakers_are_kept_and_two_way_sports_map_two_outcomes():
    events, _ = parse_odds([fixture()], sport="football", market_id=101, fetched_at=NOW, bookmakers=["roobet"])
    assert [b.key for b in events[0].bookmakers] == ["roobet"]
    assert outcome_codes("tennis", 121) == {"121": "1", "122": "2"}
    tennis = fixture(books={"stake": book([1.8, 2.1], market="121", first=121)})
    (ev,), _ = parse_odds([tennis], sport="tennis", market_id=121, fetched_at=NOW)
    assert ev.bookmakers[0].markets[0].key == H2H


# ------------------------------------------------------------------ provider
def provider(tmp_path, *queue, budget=240, **options):
    session = FakeSession(*queue)
    p = OddsPapiProvider(options={"monthly_budget": budget, "usage_file": str(tmp_path / "usage.json"), **options},
                         session=session, environ={"ODDSPAPI_API_KEY": KEY}, clock=lambda: NOW, sleep=lambda s: None)
    return p, session


def test_one_request_per_poll_with_tournaments_and_bookmakers(tmp_path):
    p, session = provider(tmp_path, FakeResponse(200, [fixture()]))
    result = p.fetch_odds("football")
    assert len(result.events) == 1
    ((url, params),) = session.calls
    assert url.endswith("/v4/odds-by-tournaments")
    assert params["bookmakers"] == "stake,roobet,mystake" and params["tournamentIds"] == "17,8,23,35,34,7,679"
    assert params["apiKey"] == KEY and params["oddsFormat"] == "decimal"
    assert p.budget.used() == 1


def test_more_than_three_bookmakers_are_split_and_joined(tmp_path):
    first = FakeResponse(200, [fixture(books={"stake": book([2.1, 3.4, 3.6])})])
    second = FakeResponse(200, [fixture(books={"pinnacle": book([2.0, 3.5, 3.9])})])
    p, session = provider(tmp_path, first, second, bookmakers=["stake", "roobet", "mystake", "pinnacle"])
    (ev,) = p.fetch_odds("football").events
    assert [c[1]["bookmakers"] for c in session.calls] == ["stake,roobet,mystake", "pinnacle"]
    assert [b.key for b in ev.bookmakers] == ["stake", "pinnacle"]


def test_missing_names_trigger_one_fixture_lookup_then_it_is_cached(tmp_path):
    names = [{"fixtureId": "id100", "participant1Name": "Arsenal", "participant2Name": "Chelsea"}]
    nameless = FakeResponse(200, [fixture(names=False), fixture("id200", names=False)])
    p, session = provider(tmp_path, nameless, FakeResponse(200, names), nameless)
    assert len(p.fetch_odds("football").events) == 1  # id200 is still unknown: skipped, not guessed
    assert session.calls[1][0].endswith("/v4/fixtures") and session.calls[1][1]["sportId"] == 10
    p.fetch_odds("football")
    assert len(session.calls) == 3  # no second lookup within names_refresh_hours


def test_budget_stops_the_source_and_survives_restarts(tmp_path):
    p, _ = provider(tmp_path, FakeResponse(200, []), FakeResponse(200, []), budget=2)
    p.fetch_odds("football")
    p.fetch_odds("football")
    with pytest.raises(QuotaExhaustedError):
        p.fetch_odds("football")
    again, session = provider(tmp_path, budget=2)  # a restart reads the saved count
    with pytest.raises(QuotaExhaustedError):
        again.fetch_odds("football")
    assert session.calls == []


def test_budget_resets_each_month(tmp_path):
    day = [date(2026, 10, 31)]
    budget = RequestBudget(tmp_path / "u.json", 5, today=lambda: day[0])
    budget.spend(5)
    assert budget.remaining() == 0
    day[0] = date(2026, 11, 1)
    assert budget.remaining() == 5
    assert json.loads((tmp_path / "u.json").read_text()) == {"2026-10": 5}


def test_errors_never_contain_the_key(tmp_path):
    p, _ = provider(tmp_path, FakeResponse(401, text=f"invalid key {KEY}"))
    with pytest.raises(AuthenticationError) as auth:
        p.fetch_odds("football")
    assert KEY not in str(auth.value) and "***" in str(auth.value)
    p, _ = provider(tmp_path, FakeResponse(429, headers={"Retry-After": "60"}))
    with pytest.raises(RateLimitError) as limited:
        p.fetch_odds("football")
    assert limited.value.retry_after == 60
    p, _ = provider(tmp_path, FakeResponse(400, text=f"bad parameter for {KEY}"))
    with pytest.raises(ProviderError) as bad:
        p.fetch_odds("football")
    assert KEY not in str(bad.value)


def test_server_errors_are_retried_and_counted(tmp_path):
    p, session = provider(tmp_path, FakeResponse(503), FakeResponse(200, [fixture()]))
    assert len(p.fetch_odds("football").events) == 1
    assert len(session.calls) == 2 and p.budget.used() == 2


def test_needs_a_key_and_a_configured_sport(tmp_path):
    with pytest.raises(ConfigError, match="ODDSPAPI_API_KEY"):
        OddsPapiProvider(environ={})
    p, _ = provider(tmp_path)
    assert p.supports("football") and not p.supports("basketball")
    with pytest.raises(ProviderError):
        p.fetch_odds("basketball")


def test_config_wiring_builds_a_metered_multi_bookmaker_source(monkeypatch, tmp_path):
    from odds_scanner import cli
    from odds_scanner.config import config_from_dict

    monkeypatch.setenv("ODDSPAPI_API_KEY", KEY)
    off = {name: {"enabled": False} for name in ("monacobet", "doxxbet", "nike", "tipos", "synot")}
    cfg = config_from_dict({"sports": ["football", "hockey"], "providers": {
        **off, "oddspapi": {"enabled": True, "sports": ["football", "hockey"], "options": {"usage_file": str(tmp_path / "u.json")}}}})
    (src,) = cli.build_sources(cfg)
    assert src.key == "oddspapi" and src.metered and src.poll_interval == 1200
    assert src.sports == ["football"] and src.skipped_sports == ["hockey"]
    assert src.title == "OddsPapi (Stake, Roobet, MyStake)"
    assert src.homepages["roobet"].startswith("https://roobet.com")


def test_league_list_helper_shows_ids_and_marks_configured_ones(tmp_path):
    from odds_scanner.providers.oddspapi import list_tournaments

    rows = [{"tournamentId": 17, "tournamentName": "Premier League", "categoryName": "England", "futureFixtures": 300},
            {"tournamentId": 211, "tournamentName": "Niké liga", "categoryName": "Slovakia", "futureFixtures": 90, "upcomingFixtures": 6}]
    p, session = provider(tmp_path, FakeResponse(200, rows))
    lines = []
    assert list_tournaments(p, out=lines.append) == 0
    assert session.calls[0][0].endswith("/v4/tournaments") and session.calls[0][1]["sportId"] == 10
    assert "17*" in lines[1] and "Premier League" in lines[1]
    assert "211 " in lines[2] and "Slovakia - Niké liga" in lines[2] and "96" in lines[2]
    assert "239 of 240" in "\n".join(lines)

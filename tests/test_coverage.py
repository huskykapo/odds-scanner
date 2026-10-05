import json

import requests

from odds_scanner import coverage as c
from odds_scanner import diagnostics as d
from tests.test_diagnostics import Resp

KEY = "TOPSECRETKEY"


class Session:
    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def render(report):
    return d.render(report)


# ------------------------------------------------------------------ name extraction
def test_bookmaker_names_are_found_in_any_json_shape():
    shapes = [
        [{"name": "Stake"}, {"name": "Roobet"}],
        {"data": [{"bookmakerName": "Tipsport SK", "id": 1}]},
        {"bookmakers": {"pinnacle": {"title": "Pinnacle"}}},
        ["Chance", "Fortuna"],
    ]
    names = set().union(*(c.bookmaker_names(s) for s in shapes))
    assert {"Stake", "Roobet", "Tipsport SK", "Pinnacle", "Chance", "Fortuna"} <= names


def test_match_targets_is_case_insensitive_and_reports_misses():
    found = c.match_targets({"STAKE.com", "Tipsport-Sk", "bet365"})
    assert found["Stake"] == ["STAKE.com"] and found["Tipsport"] == ["Tipsport-Sk"]
    assert found["Roobet"] == [] and found["Chance"] == []


# ------------------------------------------------------------------ OddsPapi
def test_oddspapi_without_key_explains_how_and_makes_no_request():
    s = Session()
    r = c.check_oddspapi(environ={}, session=s)
    assert r.status == "NO_KEY" and s.calls == []
    assert "ODDSPAPI_API_KEY" in r.error and "$env:" in r.error


def test_oddspapi_reports_found_and_missing_bookmakers_without_printing_the_key():
    s = Session(Resp(200, body=[{"name": "Stake"}, {"name": "Pinnacle"}, {"name": "Bet365"}]))
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s)
    text = render(r)
    assert r.status == "OK" and "FOUND: Stake" in text and "Roobet    not in the list" in text
    assert "bookmakers visible to this key: 3" in text and "listed is not the same as" in text
    assert KEY not in text
    assert s.calls[0][1] == {"apiKey": KEY}  # sent to the service, never shown


NOSLEEP = lambda x: None  # noqa: E731


def test_oddspapi_errors_never_leak_the_key():
    leaky = requests.ConnectionError(f"failed: https://api.oddspapi.io/v4/bookmakers?apiKey={KEY}")
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(leaky, leaky, leaky), sleep=NOSLEEP)
    assert r.status == "UNREACHABLE" and KEY not in render(r) and "ConnectionError" in r.error and "tried 3 times" in r.error
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(Resp(401, text="no", ctype="text/html")), sleep=NOSLEEP)
    assert r.status == "AUTH_FAILED" and KEY not in render(r)
    assert c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(Resp(429, text="x", ctype="text/html")), sleep=NOSLEEP).status == "RATE_LIMITED"
    five = lambda: Resp(500, text="x", ctype="text/html")  # noqa: E731
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(five(), five(), five()), sleep=NOSLEEP)
    assert r.status == "ERROR" and "HTTP 500" in r.error and "tried 3 times" in r.error
    assert c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(Resp(200, text="<html>", ctype="text/html")), sleep=NOSLEEP).status == "ERROR"


def test_transient_failures_are_retried_politely_and_4xx_never():
    sleeps = []
    s = Session(Resp(500, text="x", ctype="text/html"), requests.ConnectionError("x"), LIST)
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s, sleep=sleeps.append)
    assert r.status == "OK" and len(s.calls) == 3 and sleeps == [5.0, 10.0]  # two retries, spaced out
    assert "(requests sent: 3)" in render(r)
    s = Session(Resp(401, text="x", ctype="text/html"))
    c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s, sleep=sleeps.append)
    assert len(s.calls) == 1  # an answer, not a hiccup: no retry
    five = Resp(500, text="x", ctype="text/html")
    s = Session(five, five, five, five)
    c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s, sleep=NOSLEEP)
    assert len(s.calls) == 3  # never more than 2 retries


# ------------------------------------------------------------------ SportMonks (paginated)
def test_sportmonks_follows_pages_until_no_more_and_spaces_requests():
    page1 = {"data": [{"id": 1, "name": "Bet365"}, {"id": 2, "name": "Tipsport-Sk"}], "pagination": {"has_more": True}}
    page2 = {"data": [{"id": 3, "name": "Stake"}], "pagination": {"has_more": False}}
    s = Session(Resp(200, body=page1), Resp(200, body=page2))
    sleeps = []
    r = c.check_sportmonks(environ={"SPORTMONKS_API_TOKEN": KEY}, session=s, sleep=sleeps.append)
    text = render(r)
    assert [call[1]["page"] for call in s.calls] == [1, 2] and sleeps == [c.MIN_INTERVAL]
    assert "FOUND: Tipsport-Sk" in text and "FOUND: Stake" in text and "Chance    not in the list" in text
    assert "(requests sent: 2)" in text and KEY not in text


def test_sportmonks_stops_when_a_page_adds_nothing_new_or_pagination_is_missing():
    same = {"data": [{"name": "Bet365"}], "pagination": {"has_more": True}}
    s = Session(Resp(200, body=same), Resp(200, body=same))
    c.check_sportmonks(environ={"SPORTMONKS_API_TOKEN": KEY}, session=s, sleep=lambda x: None)
    assert len(s.calls) == 2  # second page added nothing -> stop (no endless loop)
    s = Session(Resp(200, body={"data": [{"name": "Bet365"}]}))
    c.check_sportmonks(environ={"SPORTMONKS_API_TOKEN": KEY}, session=s, sleep=lambda x: None)
    assert len(s.calls) == 1


def test_sportmonks_page_cap(monkeypatch):
    monkeypatch.setattr(c, "MAX_PAGES", 3)
    items = [Resp(200, body={"data": [{"name": f"Book{i}"}], "pagination": {"has_more": True}}) for i in range(5)]
    s = Session(*items)
    r = c.check_sportmonks(environ={"SPORTMONKS_API_TOKEN": KEY}, session=s, sleep=lambda x: None)
    assert len(s.calls) == 3 and "stopped after 3 pages" in render(r)


def test_sportmonks_no_token_and_errors():
    assert c.check_sportmonks(environ={}, session=Session()).status == "NO_KEY"
    r = c.check_sportmonks(environ={"SPORTMONKS_API_TOKEN": KEY}, session=Session(Resp(403, text="x", ctype="text/html")))
    assert r.status == "AUTH_FAILED" and KEY not in render(r)
    leaky = requests.ConnectionError(f"https://x?api_token={KEY}")
    assert KEY not in render(c.check_sportmonks(environ={"SPORTMONKS_API_TOKEN": KEY}, session=Session(leaky)))


# ------------------------------------------------------------------ command wiring
def test_run_target_names_and_all_excludes_aggregators(monkeypatch):
    monkeypatch.delenv("ODDSPAPI_API_KEY", raising=False)
    lines = []
    assert d.run("oddspapi", out=lines.append) == 1 and "NO_KEY" in "\n".join(lines)
    assert {"oddspapi", "sportmonks"} <= set(d.available_targets())
    called = []
    monkeypatch.setattr(c, "check_oddspapi", lambda: called.append("o"))
    monkeypatch.setattr(d, "check_provider", lambda name, **kw: d.BookmakerReport(name, "x", "OK"))
    monkeypatch.setattr(d, "diagnose_site", lambda site, **kw: d.SiteReport(site, verdict="INCONCLUSIVE"))
    d.run("all", out=lambda s: None)
    assert called == []  # "all" never spends aggregator quota


# ------------------------------------------------------------------ OddsPapi: one real sample
from datetime import date


LIST = Resp(200, body=[{"name": "roobet"}, {"name": "stake"}, {"name": "pinnacle"}, {"name": "bet365"}])
FIXTURES = Resp(200, body={"data": [{"fixtureId": "id100", "participant1": "A"}, {"fixtureId": "id101"}]})
ODDS = Resp(200, body={"fixtureId": "id100", "bookmakerOdds": {"pinnacle": {"markets": {"101": {}, "104": {}}}, "stake": {"markets": {"101": {}}}}})


def test_sample_fetches_one_fixture_saves_files_and_reports_per_bookmaker(tmp_path):
    s = Session(LIST, FIXTURES, ODDS)
    sleeps = []
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s, save_dir=tmp_path, sleep=sleeps.append, today=date(2026, 10, 5))
    text = render(r)
    assert "(requests sent: 3)" in text and sleeps == [c.MIN_INTERVAL, c.MIN_INTERVAL]
    fixtures_call, odds_call = s.calls[1], s.calls[2]
    assert fixtures_call[0].endswith("/v4/fixtures")
    assert fixtures_call[1] == {"sportId": 10, "from": "2026-10-05", "to": "2026-10-08", "hasOdds": "true", "statusId": 0, "apiKey": KEY}
    assert odds_call[1]["fixtureId"] == "id100" and odds_call[1]["bookmakers"] == "roobet,stake,pinnacle" and odds_call[1]["oddsFormat"] == "decimal"
    assert "fixtures with odds in the next 3 days (football): 2" in text
    assert "pinnacle  odds returned (2 markets)" in text and "stake     odds returned (1 markets)" in text
    assert "roobet    NO ODDS returned for this fixture on this plan" in text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["oddspapi_fixtures.json", "oddspapi_odds.json"]
    assert all(KEY not in p.read_text() for p in tmp_path.iterdir()) and KEY not in text


def test_sample_is_skipped_without_save_dir_and_errors_are_reported_without_the_key(tmp_path):
    s = Session(LIST)
    c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s, sleep=lambda x: None)
    assert len(s.calls) == 1  # no --save -> only the free list request
    leaky = requests.ConnectionError(f"https://api.oddspapi.io/v4/fixtures?apiKey={KEY}")
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(LIST, leaky, leaky, leaky), save_dir=tmp_path, sleep=lambda x: None)
    assert "sample failed: no HTTP answer (ConnectionError)" in render(r) and KEY not in render(r)
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(LIST, FIXTURES, Resp(429, text="x", ctype="text/html")), save_dir=tmp_path, sleep=lambda x: None)
    assert "sample failed: HTTP 429" in render(r)


def test_sample_unrecognised_shapes_ask_for_the_file(tmp_path):
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(LIST, Resp(200, body={"nothing": []})), save_dir=tmp_path, sleep=lambda x: None)
    assert "no fixture id found" in render(r)
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(LIST, FIXTURES, Resp(200, body={"x": 1})), save_dir=tmp_path, sleep=lambda x: None)
    assert "structure was not recognised" in render(r)


def test_find_key():
    assert c.find_key({"a": [{"b": {"fixtureId": "z"}}]}, "fixtureId") == "z" and c.find_key({"a": 1}, "fixtureId") is None


def test_mystake_is_its_own_bookmaker_not_part_of_stake():
    found = c.match_targets({"Stake.com", "stake", "MyStake", "mystake", "Stake BR"})
    assert found["MyStake"] == ["MyStake", "mystake"]
    assert found["Stake"] == ["Stake BR", "Stake.com", "stake"]  # MyStake is not counted as Stake


def test_sample_asks_for_mystake_too_when_it_is_in_the_list(tmp_path):
    names = Resp(200, body=[{"name": "roobet"}, {"name": "stake"}, {"name": "mystake"}, {"name": "pinnacle"}])
    s = Session(names, FIXTURES, ODDS)
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s, save_dir=tmp_path, sleep=lambda x: None, today=date(2026, 10, 5))
    assert s.calls[2][1]["bookmakers"] == "roobet,stake,mystake,pinnacle"
    assert "mystake   NO ODDS returned for this fixture on this plan" in render(r)


# ------------------------------------------------------------------ Odds-API.io + error messages
def test_oddsapiio_no_key_found_and_errors(monkeypatch):
    assert c.check_oddsapiio(environ={}, session=Session()).status == "NO_KEY"
    s = Session(Resp(200, body=[{"name": "Stake"}, {"name": "Roobet"}, {"name": "Mystake"}, {"name": "Bet365"}]))
    r = c.check_oddsapiio(environ={"ODDSAPIIO_API_KEY": KEY}, session=s)
    text = render(r)
    assert s.calls[0][0] == "https://api.odds-api.io/v3/bookmakers" and s.calls[0][1] == {"apiKey": KEY}
    assert "FOUND: Stake" in text and "FOUND: Roobet" in text and "MyStake   FOUND: Mystake" in text and "Tipsport  not in the list" in text
    assert KEY not in text
    r = c.check_oddsapiio(environ={"ODDSAPIIO_API_KEY": KEY}, session=Session(Resp(404, text="nope", ctype="text/html")))
    assert r.status == "ERROR" and "endpoint may have changed" in r.error
    assert c.check_oddsapiio(environ={"ODDSAPIIO_API_KEY": KEY}, session=Session(Resp(401, text="x", ctype="text/html"))).status == "AUTH_FAILED"
    leaky = requests.ConnectionError(f"https://api.odds-api.io/v3/bookmakers?apiKey={KEY}")
    assert KEY not in render(c.check_oddsapiio(environ={"ODDSAPIIO_API_KEY": KEY}, session=Session(leaky, leaky, leaky), sleep=lambda x: None))


def test_the_services_own_error_message_is_shown_with_the_key_removed():
    def msg():
        return Resp(500, text=f'{{"error": "Internal error for key {KEY}, try again"}}', ctype="application/json")

    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(msg(), msg(), msg()), sleep=NOSLEEP)
    assert r.status == "ERROR" and "service says:" in r.error and "Internal error" in r.error
    assert KEY not in render(r) and "***" in r.error

    def page():
        return Resp(500, text="<html><body>Bad gateway</body></html>", ctype="text/html")

    assert "service says" not in c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(page(), page(), page()), sleep=NOSLEEP).error


def test_sample_retries_a_transient_500_and_reports_the_service_message(tmp_path):
    sleeps = []
    s = Session(LIST, Resp(500, text="x", ctype="text/html"), FIXTURES, ODDS)
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s, save_dir=tmp_path, sleep=sleeps.append, today=date(2026, 10, 5))
    assert "(requests sent: 4)" in render(r) and "odds returned" in render(r)
    bad = lambda: Resp(403, text='{"message": "plan does not include this endpoint"}', ctype="application/json")  # noqa: E731
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(LIST, bad()), save_dir=tmp_path, sleep=NOSLEEP)
    assert "sample failed: HTTP 403 - service says: " in render(r) and "plan does not include" in render(r)


def test_all_target_includes_no_aggregator_and_names_are_listed():
    assert {"oddspapi", "sportmonks", "oddsapiio"} <= set(d.available_targets())


LIVE = lambda: Resp(403, text='{"error":{"message":"No bookmakers with live access found.","code":"RESTRICTED_ACCESS","details":"Fixture you have requested is live."}}', ctype="application/json")  # noqa: E731


def test_sample_asks_only_for_not_started_fixtures_and_skips_a_live_one(tmp_path):
    s = Session(LIST, FIXTURES, LIVE(), ODDS)
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s, save_dir=tmp_path, sleep=lambda x: None, today=date(2026, 10, 5))
    text = render(r)
    assert s.calls[1][1]["statusId"] == 0
    assert [call[1]["fixtureId"] for call in s.calls[2:]] == ["id100", "id101"]  # live one skipped, next one tried
    assert "fixture id100 is live" in text and "pinnacle  odds returned" in text and "sample failed" not in text


def test_sample_gives_up_after_the_candidates_and_other_403s_are_not_skipped(tmp_path):
    fixtures = Resp(200, body={"data": [{"fixtureId": f"id{i}"} for i in range(5)]})
    s = Session(LIST, fixtures, LIVE(), LIVE(), LIVE())
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s, save_dir=tmp_path, sleep=lambda x: None, today=date(2026, 10, 5))
    assert len(s.calls) == 5 and "sample failed: HTTP 403" in render(r)  # 3 candidates max
    other = Resp(403, text='{"message": "plan does not include this endpoint"}', ctype="application/json")
    s = Session(LIST, FIXTURES, other)
    c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s, save_dir=tmp_path, sleep=lambda x: None, today=date(2026, 10, 5))
    assert len(s.calls) == 3  # not a live problem: no point trying another fixture


def test_collect_values():
    assert c.collect_values({"a": [{"fixtureId": "x"}, {"fixtureId": "y"}, {"fixtureId": "x"}]}, "fixtureId", limit=5) == ["x", "y"]
    assert c.collect_values({"a": 1}, "fixtureId", limit=3) == []


# ------------------------------------------------------------------ Odds-API.io: one real sample
OAI_LIST = lambda: Resp(200, body=[{"name": "Stake"}, {"name": "Roobet"}, {"name": "Bet365"}])  # noqa: E731
OAI_EVENTS = lambda: Resp(200, body=[{"id": 111, "home": "A", "away": "B"}, {"id": 112}])  # noqa: E731
OAI_ODDS = lambda: Resp(200, body={"id": 111, "bookmakers": {"Stake": {"markets": [{"name": "ML", "odds": [{"home": 1.9, "away": 2.1}]}]}}, "price": 1.5, "odds": 2.0})  # noqa: E731


def test_oddsapiio_sample_saves_files_and_reports_what_it_can_verify(tmp_path):
    s = Session(OAI_LIST(), OAI_EVENTS(), OAI_ODDS())
    r = c.check_oddsapiio(environ={"ODDSAPIIO_API_KEY": KEY}, session=s, save_dir=tmp_path, sleep=lambda x: None)
    text = render(r)
    assert s.calls[1][0] == "https://api.odds-api.io/v3/events" and s.calls[1][1] == {"sport": "football", "bookmaker": "Stake", "limit": 10, "apiKey": KEY}
    assert s.calls[2][0] == "https://api.odds-api.io/v3/odds" and s.calls[2][1]["eventId"] == 111 and s.calls[2][1]["bookmakers"] == "Stake,Roobet"
    assert "upcoming football events returned for Stake: 2" in text and "(requests sent: 3)" in text
    assert "Stake     mentioned in the odds answer" in text and "Roobet    NOT in the odds answer for this event" in text
    assert "odds-like numbers in the answer: 2" in text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["oddsapiio_events.json", "oddsapiio_odds.json"]
    assert KEY not in text and all(KEY not in p.read_text() for p in tmp_path.iterdir())


def test_oddsapiio_sample_failures_and_no_save_dir(tmp_path):
    s = Session(OAI_LIST())
    c.check_oddsapiio(environ={"ODDSAPIIO_API_KEY": KEY}, session=s, sleep=lambda x: None)
    assert len(s.calls) == 1  # no --save: only the list request
    bad = Resp(403, text='{"message": "bookmaker not in your plan"}', ctype="application/json")
    r = c.check_oddsapiio(environ={"ODDSAPIIO_API_KEY": KEY}, session=Session(OAI_LIST(), bad), save_dir=tmp_path, sleep=lambda x: None)
    assert "sample failed: HTTP 403 - service says: " in render(r) and "not in your plan" in render(r)
    r = c.check_oddsapiio(environ={"ODDSAPIIO_API_KEY": KEY}, session=Session(OAI_LIST(), Resp(200, body={"x": 1})), save_dir=tmp_path, sleep=lambda x: None)
    assert "no event id found" in render(r)
    leaky = requests.ConnectionError(f"https://x?apiKey={KEY}")
    r = c.check_oddsapiio(environ={"ODDSAPIIO_API_KEY": KEY}, session=Session(leaky, leaky, leaky), sleep=lambda x: None)
    assert r.status == "UNREACHABLE" and KEY not in render(r)

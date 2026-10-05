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


def test_oddspapi_errors_never_leak_the_key():
    leaky = requests.ConnectionError(f"failed: https://api.oddspapi.io/v4/bookmakers?apiKey={KEY}")
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(leaky))
    assert r.status == "UNREACHABLE" and KEY not in render(r) and "ConnectionError" in r.error
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(Resp(401, text="no", ctype="text/html")))
    assert r.status == "AUTH_FAILED" and KEY not in render(r)
    assert c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(Resp(429, text="x", ctype="text/html"))).status == "RATE_LIMITED"
    assert c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(Resp(500, text="x", ctype="text/html"))).status == "ERROR"
    assert c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(Resp(200, text="<html>", ctype="text/html"))).status == "ERROR"


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
    assert fixtures_call[1] == {"sportId": 10, "from": "2026-10-05", "to": "2026-10-08", "hasOdds": "true", "apiKey": KEY}
    assert odds_call[1]["fixtureId"] == "id100" and odds_call[1]["bookmakers"] == "roobet,stake,pinnacle" and odds_call[1]["oddsFormat"] == "decimal"
    assert "fixtures with odds in the next 3 days (football): 2" in text
    assert "pinnacle  odds returned (2 markets)" in text and "stake     odds returned (1 markets)" in text
    assert "roobet    NO ODDS returned for this fixture on this plan" in text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["oddspapi_fixtures.json", "oddspapi_odds.json"]
    assert all(KEY not in p.read_text() for p in tmp_path.iterdir()) and KEY not in text


def test_sample_is_skipped_without_save_dir_and_errors_are_reported_without_the_key(tmp_path):
    s = Session(LIST)
    c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=s)
    assert len(s.calls) == 1  # no --save -> only the free list request
    leaky = requests.ConnectionError(f"https://api.oddspapi.io/v4/fixtures?apiKey={KEY}")
    r = c.check_oddspapi(environ={"ODDSPAPI_API_KEY": KEY}, session=Session(LIST, leaky), save_dir=tmp_path, sleep=lambda x: None)
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

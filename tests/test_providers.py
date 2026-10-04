import json
import logging
from datetime import timedelta

import pytest
import requests

from odds_scanner.errors import (
    AuthenticationError,
    ConfigError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitError,
)
from odds_scanner.providers import OddsProvider, ReplayProvider, TheOddsApiProvider
from odds_scanner.providers.parsing import parse_datetime, parse_events
from odds_scanner.providers.the_odds_api import parse_quota
from tests.conftest import NOW, FakeResponse, FakeSession

KW = dict(regions=["eu"], markets=["h2h"])


# ------------------------------------------------------------------ parsing
def test_parse_datetime_variants():
    assert parse_datetime("2026-10-04T12:00:00Z").tzinfo is not None
    assert parse_datetime("2026-10-04T12:00:00") == parse_datetime("2026-10-04T12:00:00+00:00")
    assert parse_datetime("2026-10-04T14:00:00+02:00") == parse_datetime("2026-10-04T12:00:00Z")
    assert parse_datetime(None) is None
    with pytest.raises(ValueError):
        parse_datetime(12345)


def test_parse_events_skips_malformed_and_bad_outcomes(caplog):
    raw = [
        {"id": "ok", "sport_key": "s", "commence_time": "2026-10-05T10:00:00Z", "home_team": "H", "away_team": "A",
         "bookmakers": [{"key": "b", "markets": [{"key": "h2h", "outcomes": [
             {"name": "H", "price": 2.0}, {"name": "A", "price": "n/a"}, {"name": "X"}]}]}]},
        {"id": "no-teams", "sport_key": "s", "commence_time": "2026-10-05T10:00:00Z"},
        {"id": "bad-time", "sport_key": "s", "commence_time": "soon", "home_team": "H", "away_team": "A"},
        "garbage",
    ]
    with caplog.at_level(logging.WARNING):
        events = parse_events(raw)
    assert [e.id for e in events] == ["ok"]
    outcomes = events[0].bookmakers[0].markets[0].outcomes
    assert [o.name for o in outcomes] == ["H"]  # unparsable prices dropped, not guessed
    assert events[0].bookmakers[0].title == "b"  # title falls back to key
    assert "skipping malformed event" in caplog.text


# ------------------------------------------------------------------ replay provider
def test_replay_provider_implements_interface_and_filters_by_sport(sample_path):
    p = ReplayProvider(sample_path)
    assert isinstance(p, OddsProvider)
    epl = p.fetch_odds("soccer_epl", **KW)
    assert {e.sport_key for e in epl.events} == {"soccer_epl"}
    assert len(epl.events) == 5
    assert epl.quota is None
    assert p.fetch_odds("no_such_sport", **KW).events == []


def test_replay_provider_dict_format(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"x": [{"id": "1", "sport_key": "x", "commence_time": "2026-10-05T10:00:00Z",
                                    "home_team": "H", "away_team": "A"}]}))
    assert [e.id for e in ReplayProvider(f).fetch_odds("x", **KW).events] == ["1"]


def test_replay_rebase_shifts_newest_update_to_now_keeping_relative_ages(sample_path):
    p = ReplayProvider(sample_path, rebase_timestamps=True, clock=lambda: NOW + timedelta(days=30))
    events = {e.id: e for e in p.fetch_odds("soccer_epl", **KW).events}
    fresh = events["evt_football_arb"].bookmakers[0].last_update
    stale = events["evt_football_stale"].bookmakers[1].last_update
    assert fresh == NOW + timedelta(days=30)
    assert fresh - stale == timedelta(minutes=29, seconds=30)
    assert events["evt_football_arb"].commence_time > fresh


def test_replay_errors(tmp_path):
    with pytest.raises(ProviderError, match="cannot read"):
        ReplayProvider(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    with pytest.raises(ProviderError, match="not valid JSON"):
        ReplayProvider(bad)
    scalar = tmp_path / "s.json"
    scalar.write_text("42")
    with pytest.raises(ProviderError, match="list or object"):
        ReplayProvider(scalar)


# ------------------------------------------------------------------ The Odds API provider
def make(session, **kw):
    return TheOddsApiProvider("SECRET-KEY", session=session, sleep=lambda s: None, **kw)


def test_requires_api_key_from_env(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="ODDS_API_KEY"):
        TheOddsApiProvider()
    monkeypatch.setenv("ODDS_API_KEY", "from-env")
    assert TheOddsApiProvider(session=FakeSession())._key == "from-env"
    monkeypatch.setenv("MY_KEY", "custom")
    assert TheOddsApiProvider(api_key_env="MY_KEY", session=FakeSession())._key == "custom"


def test_fetch_builds_request_and_reads_quota_headers(sample_path):
    body = json.loads(sample_path.read_text())[:2]
    session = FakeSession(FakeResponse(200, body, {
        "X-Requests-Remaining": "480", "X-Requests-Used": "20", "X-Requests-Last": "2"}))
    result = make(session).fetch_odds("soccer_epl", regions=["eu", "uk"], markets=["h2h", "totals"], bookmakers=["a", "b"])
    url, params = session.calls[0]
    assert url.endswith("/sports/soccer_epl/odds")
    assert params["regions"] == "eu,uk" and params["markets"] == "h2h,totals"
    assert params["bookmakers"] == "a,b" and params["oddsFormat"] == "decimal"
    assert len(result.events) == 2
    assert (result.quota.remaining, result.quota.used, result.quota.last_cost) == (480, 20, 2)


def test_bookmakers_param_omitted_when_no_whitelist():
    session = FakeSession(FakeResponse(200, []))
    make(session).fetch_odds("s", **KW)
    assert "bookmakers" not in session.calls[0][1]


def test_parse_quota_handles_missing_and_garbage_headers():
    q = parse_quota({"x-requests-remaining": "abc"})
    assert (q.remaining, q.used, q.last_cost) == (None, None, None)


def test_rate_limit_429_raises_with_retry_after_and_is_not_retried():
    session = FakeSession(FakeResponse(429, {}, {"Retry-After": "7"}))
    with pytest.raises(RateLimitError) as ei:
        make(session).fetch_odds("s", **KW)
    assert ei.value.retry_after == 7.0
    assert len(session.calls) == 1


def test_out_of_credits_401_raises_quota_exhausted():
    session = FakeSession(FakeResponse(401, {"error_code": "OUT_OF_USAGE_CREDITS"}))
    with pytest.raises(QuotaExhaustedError):
        make(session).fetch_odds("s", **KW)


def test_invalid_key_401_raises_authentication_error_without_leaking_key():
    session = FakeSession(FakeResponse(401, {"error_code": "INVALID_KEY"}))
    with pytest.raises(AuthenticationError) as ei:
        make(session).fetch_odds("s", **KW)
    assert "SECRET-KEY" not in str(ei.value)


@pytest.mark.parametrize("status", [404, 422])
def test_client_errors_are_not_retried(status):
    session = FakeSession(FakeResponse(status, {"error_code": "UNKNOWN_SPORT"}))
    with pytest.raises(ProviderError, match=str(status)):
        make(session).fetch_odds("s", **KW)
    assert len(session.calls) == 1


def test_server_errors_retried_then_succeed():
    sleeps = []
    session = FakeSession(FakeResponse(503), FakeResponse(500), FakeResponse(200, []))
    p = TheOddsApiProvider("k", session=session, sleep=sleeps.append, retry_backoff=1.0)
    assert p.fetch_odds("s", **KW).events == []
    assert sleeps == [1.0, 2.0]


def test_server_errors_exhaust_retries():
    session = FakeSession(*[FakeResponse(502)] * 3)
    with pytest.raises(ProviderError, match="after 3 attempts"):
        make(session).fetch_odds("s", **KW)


def test_network_error_never_leaks_api_key():
    leaky = requests.ConnectionError("HTTPSConnectionPool: /v4/sports/s/odds?apiKey=SECRET-KEY failed")
    session = FakeSession(leaky, leaky, leaky)
    with pytest.raises(ProviderError) as ei:
        make(session).fetch_odds("s", **KW)
    assert "SECRET-KEY" not in str(ei.value)
    assert "ConnectionError" in str(ei.value)
    assert "SECRET-KEY" not in repr(ei.value)
    assert ei.value.__cause__ is None and ei.value.__context__ is None  # original exception not chained


def test_bad_payloads_raise_provider_error():
    with pytest.raises(ProviderError, match="not valid JSON"):
        make(FakeSession(FakeResponse(200, text_json=False))).fetch_odds("s", **KW)
    with pytest.raises(ProviderError, match="expected a JSON list"):
        make(FakeSession(FakeResponse(200, {"message": "hi"}))).fetch_odds("s", **KW)


def test_live_provider_over_real_http_never_logs_api_key(caplog):
    """Real `requests` against a local server: quota headers parse, and DEBUG logs stay key-free."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from odds_scanner import cli

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Requests-Remaining", "321")
            self.end_headers()
            self.wfile.write(b"[]")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cli.setup_logging("DEBUG")  # the CLI's guard keeps urllib3 (which logs full URLs) quiet
        caplog.set_level(logging.DEBUG)
        provider = TheOddsApiProvider("TOPSECRETKEY", base_url=f"http://127.0.0.1:{server.server_port}/v4")
        result = provider.fetch_odds("soccer_epl", **KW)
    finally:
        server.shutdown()
    assert result.quota.remaining == 321
    assert "TOPSECRETKEY" not in caplog.text
    assert "remaining=321" in caplog.text

import csv
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from odds_scanner import cli
from odds_scanner.config import config_from_dict
from odds_scanner.errors import (
    AuthenticationError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitError,
)
from odds_scanner.models import Arbitrage, QuotaInfo
from odds_scanner.notifiers import Notifier
from odds_scanner.providers import OddsProvider, ReplayProvider
from odds_scanner.providers.base import FetchResult
from odds_scanner.scanner import EXIT_AUTH, EXIT_OK, EXIT_QUOTA, Scanner
from odds_scanner.storage import ArbLog
from tests.conftest import NOW


class ScriptedProvider(OddsProvider):
    """Returns/raises queued items; falls back to the last one. Records requests."""

    name = "scripted"

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[str] = []
        self.closed = False

    def fetch_odds(self, sport, *, regions, markets, bookmakers=()):
        self.requests.append(sport)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class Recorder(Notifier, ArbLog):
    def __init__(self):
        self.batches: list[list[Arbitrage]] = []

    def notify(self, arbs):
        self.batches.append(list(arbs))

    def append(self, arbs):
        self.batches.append(list(arbs))


class Exploding(Notifier):
    def notify(self, arbs):
        raise RuntimeError("boom")


def cfg(**kw):
    base = {"sports": ["soccer_epl"], "regions": ["eu"], "markets": ["h2h"], "poll_interval_seconds": 100,
            "storage": {"backend": "none"}}
    base.update(kw)
    return config_from_dict(base)


def replay_result(sample_path, sport="soccer_epl"):
    return ReplayProvider(sample_path, rebase_timestamps=False).fetch_odds(sport, regions=[], markets=[])


def scanner(config, provider, **kw):
    return Scanner(config, provider, clock=lambda: NOW, sleep=kw.pop("sleep", lambda s: None), **kw)


# ------------------------------------------------------------------ scan_once
def test_scan_once_finds_arbs_across_sports(sample_path):
    class PerSport(OddsProvider):
        name = "x"

        def fetch_odds(self, sport, **kw):
            return replay_result(sample_path, sport)

    r = scanner(cfg(sports=["soccer_epl", "tennis_atp_paris_masters", "basketball_nba"]), PerSport()).scan_once()
    assert {a.event_id for a in r.arbs} == {
        "evt_football_arb", "evt_tennis_arb", "evt_nba_totals", "evt_nba_spreads"}  # replay serves every market in the file
    assert r.sports_ok == 3 and r.errors == 0 and r.events_scanned == 9
    assert [a.realized_profit_percent for a in r.arbs] == sorted((a.realized_profit_percent for a in r.arbs), reverse=True)


def test_provider_error_for_one_sport_does_not_stop_the_others(sample_path, caplog):
    p = ScriptedProvider(ProviderError("HTTP 404 UNKNOWN_SPORT"), replay_result(sample_path))
    with caplog.at_level(logging.ERROR):
        r = scanner(cfg(sports=["gone", "soccer_epl"]), p).scan_once()
    assert p.requests == ["gone", "soccer_epl"]
    assert r.errors == 1 and r.sports_ok == 1 and r.fatal is None and r.arbs
    assert "UNKNOWN_SPORT" in caplog.text


@pytest.mark.parametrize("exc, expected", [(AuthenticationError("bad key"), AuthenticationError), (QuotaExhaustedError("out"), QuotaExhaustedError)])
def test_fatal_errors_stop_cycle(exc, expected):
    p = ScriptedProvider(exc)
    r = scanner(cfg(sports=["a", "b"]), p).scan_once()
    assert isinstance(r.fatal, expected) and p.requests == ["a"]


def test_rate_limit_ends_cycle_and_reports_wait():
    p = ScriptedProvider(RateLimitError("429", retry_after=42))
    r = scanner(cfg(sports=["a", "b"]), p).scan_once()
    assert p.requests == ["a"] and r.rate_limited_for == 42 and r.fatal is None


def test_rate_limit_wait_extends_delay():
    # Provider asked for 42s but the base interval is 10s: wait at least what it asked.
    s = scanner(cfg(poll_interval_seconds=10), ScriptedProvider(RateLimitError("429", retry_after=42)))
    r = s.scan_once()
    assert s.next_delay(r) >= 42


# ------------------------------------------------------------------ quota
def quota_result(remaining):
    return FetchResult(events=[], quota=QuotaInfo(remaining=remaining, used=500 - remaining, last_cost=2))


def test_quota_floor_stops_before_dipping_below(caplog):
    # regions x markets = 1 x 1 = cost 1; floor 10 -> with 10 left, a request would leave 9: refuse.
    p = ScriptedProvider(quota_result(10))
    s = scanner(cfg(sports=["a", "b"], quota={"stop_below": 10}), p)
    r1 = s.scan_once()  # sport "a" is fetched (quota unknown), then "b" is refused
    assert p.requests == ["a"] and r1.fatal is not None and "quota.stop_below" in str(r1.fatal)
    assert s.run(once=True) == EXIT_QUOTA


def test_quota_cost_accounts_for_markets_times_regions():
    p = ScriptedProvider(quota_result(30))
    c = cfg(sports=["a", "b"], regions=["eu", "uk"], markets=["h2h", "totals"], quota={"stop_below": 27})  # cost 4
    r = scanner(c, p).scan_once()
    assert p.requests == ["a"] and r.fatal is not None  # 30 - 4 = 26 < 27


def test_quota_healthy_keeps_going():
    p = ScriptedProvider(quota_result(400))
    r = scanner(cfg(sports=["a", "b", "c"]), p).scan_once()
    assert p.requests == ["a", "b", "c"] and r.fatal is None and r.quota.remaining == 400


def test_poll_interval_backs_off_when_quota_low(caplog):
    s = scanner(cfg(quota={"backoff_below": 100, "backoff_multiplier": 4, "stop_below": 5}), ScriptedProvider(quota_result(80)))
    with caplog.at_level(logging.WARNING):
        assert s.next_delay(s.scan_once()) == 400
    assert "quota low" in caplog.text
    s2 = scanner(cfg(), ScriptedProvider(quota_result(300)))
    assert s2.next_delay(s2.scan_once()) == 100


def test_failing_cycles_back_off_exponentially_and_reset():
    p = ScriptedProvider(ProviderError("down"), ProviderError("down"), ProviderError("down"), quota_result(400))
    s = scanner(cfg(poll_interval_seconds=60), p)
    delays = [s.next_delay(s.scan_once()) for _ in range(4)]
    assert delays == [120, 240, 480, 60]


# ------------------------------------------------------------------ publish / run
def test_publish_isolates_failing_sinks(sample_path, caplog):
    rec = Recorder()
    s = scanner(cfg(), ScriptedProvider(replay_result(sample_path)), notifiers=[Exploding(), rec], logs=[rec])
    with caplog.at_level(logging.ERROR):
        assert s.run(once=True) == EXIT_OK
    assert len(rec.batches) == 2 and rec.batches[0] and "Exploding failed" in caplog.text


def test_empty_cycle_still_notifies_so_console_can_say_so():
    rec = Recorder()
    scanner(cfg(), ScriptedProvider(FetchResult()), notifiers=[rec]).run(once=True)
    assert rec.batches == [[]]


def test_nothing_published_when_every_fetch_failed():
    rec = Recorder()
    scanner(cfg(), ScriptedProvider(ProviderError("down")), notifiers=[rec]).run(once=True)
    assert rec.batches == []


def test_run_returns_auth_exit_code_and_closes_provider():
    p = ScriptedProvider(AuthenticationError("bad key"))
    assert scanner(cfg(), p).run() == EXIT_AUTH
    assert p.closed


def test_run_loops_sleeping_between_polls_until_interrupted():
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    p = ScriptedProvider(FetchResult())
    assert scanner(cfg(), p, sleep=sleep).run() == EXIT_OK
    assert sleeps == [100, 100, 100] and len(p.requests) == 3


# ------------------------------------------------------------------ CLI end to end (offline)
def test_cli_replay_end_to_end_with_csv_and_sqlite(tmp_path, sample_path, capsys, monkeypatch):
    conf = tmp_path / "c.yaml"
    conf.write_text(f"""
bankroll: 1000
min_profit_percent: 1.0
storage: {{backend: both, csv_path: {tmp_path}/out/arbs.csv, sqlite_path: {tmp_path}/out/arbs.db}}
""")
    code = cli.main(["--config", str(conf), "--replay", str(sample_path), "--once"])
    out = capsys.readouterr().out
    assert code == 0
    for name in ("Northgate FC vs Riverside United", "Jan Kovac vs Luca Moretti", "Metro Hawks vs Bay Wolves", "Capital Bears vs Delta Kings"):
        assert name in out
    assert "Harbor City" not in out  # trap events are absent
    rows = list(csv.DictReader((tmp_path / "out" / "arbs.csv").open()))
    assert len(rows) == 4
    assert sqlite3.connect(tmp_path / "out" / "arbs.db").execute("SELECT COUNT(*) FROM arbs").fetchone()[0] == 4


def test_cli_overrides(tmp_path, sample_path, capsys):
    conf = tmp_path / "c.yaml"
    conf.write_text("storage: {backend: none}\n")
    cli.main(["-c", str(conf), "--replay", str(sample_path), "--once", "--min-profit", "3", "--bankroll", "500"])
    out = capsys.readouterr().out
    assert "Jan Kovac" in out and "Northgate" not in out  # 2.89 % < 3 %
    assert "total stake 500.00" in out or "total stake 499.00" in out or "total stake 501.00" in out


def test_cli_config_errors_exit_2(tmp_path, capsys):
    assert cli.main(["-c", str(tmp_path / "missing.yaml"), "--once"]) == 2
    assert "cannot read config file" in capsys.readouterr().err
    bad = tmp_path / "bad.yaml"
    bad.write_text("bankroll: -5\n")
    assert cli.main(["-c", str(bad), "--once"]) == 2
    assert "bankroll" in capsys.readouterr().err


def test_cli_missing_api_key_is_a_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    conf = tmp_path / "c.yaml"
    conf.write_text("storage: {backend: none}\n")
    assert cli.main(["-c", str(conf), "--once"]) == 2
    assert "ODDS_API_KEY" in capsys.readouterr().err


def test_cli_missing_telegram_env_is_a_clear_error(tmp_path, sample_path, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    conf = tmp_path / "c.yaml"
    conf.write_text("storage: {backend: none}\nnotifications: {telegram: {enabled: true}}\n")
    assert cli.main(["-c", str(conf), "--replay", str(sample_path), "--once"]) == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_shipped_config_is_valid_and_replay_demo_runs(sample_path, tmp_path, capsys, monkeypatch):
    from odds_scanner.config import load_config

    root = Path(__file__).resolve().parent.parent
    assert load_config(root / "config.yaml").min_profit_percent == 1.0
    monkeypatch.chdir(tmp_path)  # so the shipped config's data/ output lands in tmp
    assert cli.main(["-c", str(root / "config.yaml"), "--replay", str(sample_path), "--once"]) == 0
    assert "Jan Kovac" in capsys.readouterr().out
    assert (tmp_path / "data" / "arbs.csv").exists()


def test_logging_setup_keeps_urllib3_quiet_even_at_debug():
    cli.setup_logging("DEBUG")
    assert logging.getLogger("urllib3").getEffectiveLevel() >= logging.WARNING

"""Live engine, dashboard, probe and end-to-end tests (all offline)."""

import csv
import json
import sqlite3
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from odds_scanner import cli
from odds_scanner.arbitrage import FinderSettings, find_arbitrages
from odds_scanner.config import config_from_dict
from odds_scanner.dashboard import Dashboard
from odds_scanner.engine import LiveEngine, Source
from odds_scanner.errors import BlockedError, ProviderError
from odds_scanner.matching import match_events
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome
from odds_scanner.notifiers import Notifier
from odds_scanner.opportunities import ValidationSettings
from odds_scanner.probe import probe, probe_fortuna
from odds_scanner.providers.base import FetchResult, OddsProvider
from odds_scanner.providers.sk import MonacobetProvider, NikeProvider
from odds_scanner.storage import ArbLog
from tests.conftest import NOW, FakeResponse, FakeSession, load_sk

SETTINGS = FinderSettings(bankroll=100, min_profit_percent=0.5, stake_rounding=0.5, verify_above_percent=10)


def tweaked_nike():
    """Niké sample with better draw/away prices for Košice - Komárno, so an arb exists vs MONACObet."""
    payload = load_sk("nike_football.json")
    row = payload["bets"][0]["selectionGrid"][0]
    row[1]["odds"], row[2]["odds"] = 4.6, 5.5
    return payload


def sample_events(sport="football"):
    return MonacobetProvider.parse(load_sk("monacobet_football_league.json"), sport, NOW) + \
        NikeProvider.parse(tweaked_nike(), sport, NOW)


# ------------------------------------------------------------------ end to end: samples -> match -> arb -> stakes
def test_end_to_end_two_providers_arb_with_exact_stakes_and_profit():
    merged, _ = match_events(sample_events())
    (arb,) = find_arbitrages(merged, SETTINGS, now=NOW)
    assert arb.event_name == "FC Košice vs Komárno" and arb.market == "h2h_3_way"
    assert [(l.outcome, l.bookmaker_key, l.odds) for l in arb.legs] == [
        ("1", "monacobet", 1.69), ("X", "nike", 4.6), ("2", "nike", 5.5)]
    inv = 1 / 1.69 + 1 / 4.6 + 1 / 5.5
    assert arb.profit_percent == pytest.approx((1 / inv - 1) * 100) and arb.profit_percent == pytest.approx(0.9158, abs=1e-3)
    # ideal 59.71 / 21.94 / 18.35 -> rounded to 0.50 steps, profit recomputed from the rounded stakes
    assert [l.stake for l in arb.legs] == [59.5, 22.0, 18.5]
    assert arb.total_stake == 100.0
    assert [l.payout for l in arb.legs] == pytest.approx([100.555, 101.2, 101.75])
    assert arb.guaranteed_profit == pytest.approx(0.555)
    assert arb.realized_profit_percent == pytest.approx(0.555)
    assert not arb.verify_manually
    assert {l.event_name for l in arb.legs} == {"FC Košice - Komárno", "FC Košice - KFC Komárno"}


def test_end_to_end_regulation_time_hockey_samples():
    merged, _ = match_events(sample_events("hockey"))
    (arb,) = find_arbitrages(merged, SETTINGS, now=NOW)
    assert arb.market == "h2h_3_way@reg"  # both books' hockey 1X2 is regulation time: comparable
    # An overtime-inclusive two-way winner from another source is never mixed in, however good its prices.
    m = MarketOdds("h2h", (Outcome("FC Košice", 9.0), Outcome("Komárno", 9.0)), NOW)
    api = Event("api1", "icehockey_x", "X", arb.commence_time, "FC Košice", "Komárno", (BookmakerOdds("pinnacle", "Pinnacle", (m,), NOW),))
    merged, _ = match_events(sample_events("hockey") + [api])
    arbs = find_arbitrages(merged, SETTINGS, now=NOW)
    assert [a.market for a in arbs] == ["h2h_3_way@reg"]
    assert all(l.bookmaker_key != "pinnacle" for l in arbs[0].legs)


# ------------------------------------------------------------------ engine
class Fixed(OddsProvider):
    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def fetch_odds(self, sport, **kw):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return FetchResult(events=list(item))


class Recorder(Notifier, ArbLog):
    def __init__(self):
        self.batches = []

    def notify(self, arbs):
        self.batches.append(list(arbs))

    def append(self, arbs):
        self.batches.append(list(arbs))


def engine(*sources, **kw):
    return LiveEngine(list(sources), SETTINGS, clock=lambda: NOW, **kw)


def mono_nike_sources():
    mono = [e for e in sample_events() if e.bookmakers[0].key == "monacobet"]
    nike = [e for e in sample_events() if e.bookmakers[0].key == "nike"]
    return (Source("monacobet", "MONACObet", Fixed(mono), ["football"], 120, homepage="https://www.monacobet.sk"),
            Source("nike", "Niké", Fixed(nike), ["football"], 60))


def test_engine_finds_arb_notifies_and_logs_new_arbs_once():
    console, telegram, csvlog = Recorder(), Recorder(), Recorder()
    eng = engine(*mono_nike_sources(), notifiers=[console, telegram], logs=[csvlog], validation=ValidationSettings(confirm_polls=0))
    eng.run_once()
    eng.analyze()  # same arb again
    assert len(console.batches) == 1 and len(console.batches[0]) == 1  # announced once
    assert len(csvlog.batches) == 1  # logged once
    assert len(telegram.batches) == 1  # every notifier is told once; the registry de-duplicates
    assert eng.states["nike"].status == "ok" and eng.states["nike"].events == 2
    assert eng.stats.multi_bookmaker_events == 1


def test_engine_blocked_provider_stops_and_drops_its_events():
    mono, nike = mono_nike_sources()
    eng = engine(mono, nike)
    assert eng.poll(mono) == 120 and eng.poll(nike) == 60
    assert len(eng.analyze()) == 1
    nike.provider = Fixed(BlockedError("Niké: HTTP 403"))
    assert eng.poll(nike) is None  # stop polling
    st = eng.states["nike"]
    assert st.status == "blocked" and "403" in st.message and st.events == 0
    assert eng.analyze() == []


def test_engine_errors_back_off_and_recover():
    src = Source("nike", "Niké", Fixed(ProviderError("HTTP 500"), ProviderError("HTTP 500"), []), ["football"], 60)
    eng = engine(src)
    assert eng.poll(src) == 120 and eng.states["nike"].status == "error"
    assert eng.poll(src) == 240
    assert eng.poll(src) == 60 and eng.states["nike"].status == "ok"


def test_engine_keeps_other_sports_when_one_fails():
    src = Source("nike", "Niké", Fixed(ProviderError("boom")), ["football", "hockey"], 60, skipped_sports=["tennis"])
    eng = engine(src)
    eng.poll(src)
    assert eng.states["nike"].status == "error" and "not configured: tennis" in eng.states["nike"].message


def test_snapshot_is_json_and_has_everything_the_dashboard_needs():
    eng = engine(*mono_nike_sources(), dashboard_options={"refresh_seconds": 3, "sports": ["football"]})
    eng.run_once()
    snap = json.loads(json.dumps(eng.snapshot()))
    (arb,) = snap["arbs"]
    assert arb["market_label"] == "1X2" and arb["profit"] == pytest.approx(0.555) and arb["first_seen"]
    leg = arb["legs"][0]
    assert leg["outcome_label"] == "1 (FC Košice)" and leg["step"] == 0.5 and leg["updated"].endswith("Z")
    assert leg["homepage"] == "https://www.monacobet.sk"
    assert {p["key"]: p["status"] for p in snap["providers"]} == {"monacobet": "ok", "nike": "ok"}
    assert snap["matching"]["multi_bookmaker_events"] == 1 and snap["refresh_seconds"] == 3


def test_dashboard_serves_page_and_state():
    eng = engine(*mono_nike_sources())
    eng.run_once()
    dash = Dashboard(eng.snapshot, "127.0.0.1", 0)
    dash.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        page = opener.open(f"http://127.0.0.1:{dash.port}/", timeout=5).read().decode()
        assert "Arb Scanner" in page and "/api/state" in page
        state = json.loads(opener.open(f"http://127.0.0.1:{dash.port}/api/state", timeout=5).read())
        assert state["arbs"][0]["event"] == "FC Košice vs Komárno"
    finally:
        dash.stop()


# ------------------------------------------------------------------ CLI end to end with saved responses
def _future_samples(tmp_path):
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2)
    mono = load_sk("monacobet_football_league.json")
    for m in mono["esMatches"]:
        m["kickOffTime"] = int(start.timestamp() * 1000)
    nike = tweaked_nike()
    for b in nike["bets"]:
        b["expirationTime"] = start.isoformat()
    (tmp_path / "mono.json").write_text(json.dumps(mono))
    (tmp_path / "nike.json").write_text(json.dumps(nike))


def test_cli_once_offline_with_sample_files(tmp_path, capsys):
    _future_samples(tmp_path)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"""
bankroll: 100
providers:
  monacobet: {{options: {{sample_files: {{football: {tmp_path}/mono.json}}}}}}
  nike: {{options: {{sample_files: {{football: {tmp_path}/nike.json}}}}}}
  doxxbet: {{enabled: false}}
  tipos: {{enabled: false}}
  synot: {{enabled: false}}
storage: {{backend: both, csv_path: {tmp_path}/out/arbs.csv, sqlite_path: {tmp_path}/out/arbs.db}}
""")
    assert cli.main(["-c", str(conf), "--once"]) == 0
    out = capsys.readouterr().out
    assert "FC Košice vs Komárno" in out and "1 (FC Košice)" in out and "59.50" in out
    rows = list(csv.DictReader((tmp_path / "out" / "arbs.csv").open(encoding="utf-8")))
    assert len(rows) == 1 and rows[0]["market"] == "h2h_3_way"
    assert sqlite3.connect(tmp_path / "out" / "arbs.db").execute("SELECT COUNT(*) FROM arb_legs").fetchone()[0] == 3


def test_cli_all_providers_disabled_is_a_clear_error(tmp_path, capsys):
    conf = tmp_path / "c.yaml"
    conf.write_text("providers: {" + ", ".join(f"{n}: {{enabled: false}}" for n in ("monacobet", "doxxbet", "nike", "tipos", "synot")) + "}\n")
    assert cli.main(["-c", str(conf), "--once"]) == 2
    assert "no provider is enabled" in capsys.readouterr().err


# ------------------------------------------------------------------ probe
def _fake(behaviour):
    class P(MonacobetProvider):
        def fetch_odds(self, sport, **kw):
            if isinstance(behaviour, Exception):
                raise behaviour
            return FetchResult(events=[object()] * behaviour)

        def close(self):
            pass

    return P


def test_probe_reports_ok_blocked_error():
    lines = []
    fakes = {"monacobet": _fake(3), "doxxbet": _fake(BlockedError("DOXXbet: HTTP 403")), "nike": _fake(ProviderError("Niké: HTTP 500")),
             "tipos": _fake(0), "synot": _fake(1)}
    code = probe(config_from_dict({}), lines.append, providers=fakes)
    assert code == 1
    text = "\n".join(lines)
    assert "monacobet  OK       3 football" in text
    assert "doxxbet    BLOCKED" in text and "nike       ERROR" in text


def test_probe_fortuna_detects_data_or_not():
    html = "<div class='odds-value'>" + "".join(f"<span>{1 + i / 10:.2f}</span>" for i in range(30)) + "</div><div data-odd='1'>"
    lines = []
    assert probe_fortuna(lines.append, session=FakeSession(FakeResponse(text=html))) == 0
    assert lines[-1] == "fortuna  contains match data: YES"
    lines = []
    assert probe_fortuna(lines.append, session=FakeSession(FakeResponse(text="<html><body></body></html>"))) == 1
    assert lines[-1] == "fortuna  contains match data: NO"
    lines = []
    assert probe_fortuna(lines.append, session=FakeSession(FakeResponse(status=403, text="denied"))) == 1
    assert "BLOCKED" in lines[0]


# ------------------------------------------------------------------ history
def test_history_groups_logged_arbs_and_serves_them(tmp_path):
    from odds_scanner.storage import CsvArbLog, SqliteArbLog
    from odds_scanner.storage.history import read_history

    merged, _ = match_events(sample_events())
    (arb,) = find_arbitrages(merged, SETTINGS, now=NOW)
    better = replace(arb, profit_percent=arb.profit_percent + 1, found_at=NOW + timedelta(minutes=5))
    db, csv_file = SqliteArbLog(tmp_path / "a.db"), CsvArbLog(tmp_path / "a.csv")
    for sink in (db, csv_file):
        sink.append([arb])
        sink.append([better])
    db.close()
    for hist in (read_history(tmp_path / "a.db", None), read_history(None, tmp_path / "a.csv")):
        (one,) = hist  # same match + market: one entry
        assert one["times"] == 2 and one["profit"] == pytest.approx(better.profit_percent, abs=1e-3)
        assert one["first_found"].startswith("2026-10-04T12:00") and one["last_found"].startswith("2026-10-04T12:05")
        assert [l["outcome_label"] for l in one["legs"]] == ["1 (FC Košice)", "X (draw)", "2 (Komárno)"]
        assert one["market_label"] == "1X2"
    assert read_history(tmp_path / "missing.db", None) == []

    eng = engine(*mono_nike_sources())
    dash = Dashboard(eng.snapshot, "127.0.0.1", 0, get_history=lambda: read_history(tmp_path / "a.db", None))
    dash.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        served = json.loads(opener.open(f"http://127.0.0.1:{dash.port}/api/history", timeout=5).read())
        assert served[0]["event"] == "FC Košice vs Komárno" and served[0]["times"] == 2
    finally:
        dash.stop()


def test_cli_history_reader_adds_stake_rules(tmp_path):
    from odds_scanner.storage import SqliteArbLog

    merged, _ = match_events(sample_events())
    (arb,) = find_arbitrages(merged, SETTINGS, now=NOW)
    log_ = SqliteArbLog(tmp_path / "a.db")
    log_.append([arb])
    log_.close()
    cfg = config_from_dict({"storage": {"backend": "sqlite", "sqlite_path": str(tmp_path / "a.db")},
                            "bookmaker_settings": {"nike": {"stake_step": 1, "min_stake": 2, "stake_fee": 10}}})
    (one,) = cli.history_reader(cfg)()
    nike_leg = next(l for l in one["legs"] if l["bookmaker_key"] == "nike")
    assert nike_leg["step"] == 1 and nike_leg["min_stake"] == 2
    assert nike_leg["effective_odds"] == pytest.approx(4.6 * 0.9)  # 10 % stake fee, no win tax

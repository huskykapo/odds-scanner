import csv
import io
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest
import requests

from odds_scanner.arbitrage import FinderSettings, find_arbitrages
from odds_scanner.errors import ConfigError
from odds_scanner.notifiers import ConsoleNotifier, DedupeCache, TelegramNotifier, format_arb_table
from odds_scanner.notifiers.telegram import format_message
from odds_scanner.storage import CsvArbLog, SqliteArbLog
from tests.conftest import NOW, FakeResponse, FakeSession


@pytest.fixture
def arbs(sample_events):
    return find_arbitrages(sample_events.values(), FinderSettings(stake_rounding=1.0), now=NOW)


# ------------------------------------------------------------------ console
def test_table_lists_every_bet_sorted_as_given(arbs):
    text = format_arb_table(arbs, "EUR")
    lines = text.splitlines()
    assert lines[0].split()[0] == "Profit"
    for arb in arbs:
        assert arb.event_name in text
        for leg in arb.legs:
            assert leg.bookmaker_title in text and f"{leg.odds:.2f}" in text
    assert "guaranteed profit" in text and "EUR" in text
    assert text.index(arbs[0].event_name) < text.index(arbs[-1].event_name)
    assert "spreads (home -5.5)" in text and "totals 220.5" in text


def test_table_empty():
    assert format_arb_table([]) == "No arbitrage opportunities found."


def test_console_notifier_writes_to_stream(arbs):
    out = io.StringIO()
    ConsoleNotifier("EUR", stream=out).notify(arbs)
    assert arbs[0].event_name in out.getvalue()


# ------------------------------------------------------------------ dedupe cache
def test_dedupe_cache_ttl():
    clock = [NOW]
    cache = DedupeCache(timedelta(minutes=10), clock=lambda: clock[0])
    assert not cache.is_duplicate("k")
    cache.remember("k")
    assert cache.is_duplicate("k") and len(cache) == 1
    clock[0] = NOW + timedelta(minutes=9, seconds=59)
    assert cache.is_duplicate("k")
    clock[0] = NOW + timedelta(minutes=10)
    assert not cache.is_duplicate("k") and len(cache) == 0


# ------------------------------------------------------------------ telegram
def make_notifier(session, **kw):
    return TelegramNotifier("123:SECRET", "42", dedupe=DedupeCache(timedelta(hours=1)), session=session, currency="EUR", **kw)


def test_telegram_sends_each_arb_once(arbs):
    session = FakeSession(*[FakeResponse(200, {"ok": True})] * 20)
    n = make_notifier(session)
    n.notify(arbs)
    assert len(session.calls) == len(arbs)
    url, payload = session.calls[0]
    assert url == "https://api.telegram.org/bot123:SECRET/sendMessage"
    assert payload["chat_id"] == "42" and payload["parse_mode"] == "HTML"
    n.notify(arbs)  # same arbs again -> de-duplicated
    assert len(session.calls) == len(arbs)


def test_telegram_resends_when_odds_change(arbs):
    session = FakeSession(*[FakeResponse(200, {"ok": True})] * 20)
    n = make_notifier(session)
    n.notify(arbs[:1])
    a = arbs[0]
    moved = replace(a, legs=(replace(a.legs[0], odds=a.legs[0].odds + 0.05), *a.legs[1:]))
    n.notify([moved])
    assert len(session.calls) == 2


def test_telegram_failure_is_logged_not_raised_and_retried_next_cycle(arbs, caplog):
    session = FakeSession(FakeResponse(400, {"description": "chat not found"}), *[FakeResponse(200, {"ok": True})] * 20)
    n = make_notifier(session)
    n.notify(arbs)  # first send fails -> stop for this cycle, nothing remembered
    assert len(session.calls) == 1 and "chat not found" in caplog.text
    n.notify(arbs)  # retried
    assert len(session.calls) == 1 + len(arbs)


def test_telegram_network_error_does_not_leak_token(arbs, caplog):
    session = FakeSession(requests.ConnectionError("failed: /bot123:SECRET/sendMessage"))
    make_notifier(session).notify(arbs)
    assert "SECRET" not in caplog.text
    assert "ConnectionError" in caplog.text


def test_telegram_per_cycle_cap(arbs):
    session = FakeSession(*[FakeResponse(200, {"ok": True})] * 20)
    n = make_notifier(session, max_per_cycle=2)
    n.notify(arbs)
    assert len(session.calls) == 2
    n.notify(arbs)  # the rest go out next cycle
    assert len(session.calls) == min(4, len(arbs))


def test_telegram_message_escapes_html(arbs):
    a = replace(arbs[0], event_name="A <b>&</b> B")
    msg = format_message(a, "EUR")
    assert "A &lt;b&gt;&amp;&lt;/b&gt; B" in msg
    assert "verify every price" in msg


def test_telegram_from_env(monkeypatch):
    monkeypatch.delenv("TG_T", raising=False)
    monkeypatch.delenv("TG_C", raising=False)
    with pytest.raises(ConfigError, match="TG_T, TG_C"):
        TelegramNotifier.from_env("TG_T", "TG_C", dedupe=DedupeCache(timedelta(hours=1)))
    monkeypatch.setenv("TG_T", "tok")
    monkeypatch.setenv("TG_C", "chat")
    assert TelegramNotifier.from_env("TG_T", "TG_C", dedupe=DedupeCache(timedelta(hours=1))) is not None


# ------------------------------------------------------------------ storage
def test_csv_log_appends_with_single_header(tmp_path, arbs):
    path = tmp_path / "sub" / "arbs.csv"
    log = CsvArbLog(path)
    log.append(arbs)
    log.append(arbs[:1])
    log.append([])
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == len(arbs) + 1
    first = rows[0]
    assert first["event"] == arbs[0].event_name and first["market"] == arbs[0].market
    assert float(first["profit_percent"]) == pytest.approx(arbs[0].profit_percent, abs=1e-4)
    assert first["bookmaker_1"] == arbs[0].legs[0].bookmaker_title
    assert float(first["odds_2"]) == arbs[0].legs[1].odds
    assert first["found_at"] == NOW.isoformat()
    three_way = next(r for r in rows if r["market"] == "h2h" and r["outcome_3"])
    assert three_way["outcome_2"] == "Draw"
    assert path.read_text().count("found_at,sport") == 1


def test_csv_log_neutralises_formula_injection(tmp_path, arbs):
    path = tmp_path / "a.csv"
    CsvArbLog(path).append([replace(arbs[0], event_name="=HYPERLINK(\"http://x\")")])
    assert next(csv.DictReader(path.open()))["event"].startswith("'=")


def test_sqlite_log(tmp_path, arbs):
    path = tmp_path / "arbs.db"
    log = SqliteArbLog(path)
    log.append(arbs)
    log.append([])
    log.close()
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM arbs").fetchone()[0] == len(arbs)
    assert con.execute("SELECT COUNT(*) FROM arb_legs").fetchone()[0] == sum(len(a.legs) for a in arbs)
    event, line = con.execute("SELECT event, line FROM arbs WHERE market='totals'").fetchone()
    assert event == "Metro Hawks vs Bay Wolves" and line == 220.5
    legs = con.execute(
        "SELECT outcome, bookmaker, odds, stake FROM arb_legs JOIN arbs ON id=arb_id WHERE event_id='evt_tennis_arb' ORDER BY position"
    ).fetchall()
    assert [l[:3] for l in legs] == [("Jan Kovac", "AlphaBet", 2.10), ("Luca Moretti", "BetaPlay", 2.05)]
    con.close()

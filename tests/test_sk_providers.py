"""Offline parser tests for the Slovak providers, using the captured samples in sample_data/sk."""

import base64
import struct
from datetime import datetime, timezone

import pytest
import requests

from odds_scanner.errors import BlockedError, ProviderError, RateLimitError
from odds_scanner.providers.sk import (
    DoxxbetProvider,
    MonacobetProvider,
    NikeProvider,
    SynotProvider,
    TiposProvider,
)
from odds_scanner.providers.sk.common import bratislava_to_utc
from odds_scanner.providers.sk.http import HostThrottle, SiteClient
from odds_scanner.providers.sk.protobuf import Message
from tests.conftest import NOW, FakeResponse, FakeSession, load_sk

UTC = timezone.utc


def markets(event):
    return {m.key: {(o.name, o.point): o.price for o in m.outcomes} for m in event.bookmakers[0].markets}


# ------------------------------------------------------------------ MONACObet
def test_monacobet_parser():
    events = MonacobetProvider.parse(load_sk("monacobet_football_league.json"), "football", NOW)
    assert [(e.home_team, e.away_team) for e in events] == [("Trenčín", "Banská Bystrica"), ("FC Košice", "Komárno")]
    trencin = events[0]
    assert trencin.id == "monacobet:513503387" and trencin.betradar_id == "72042276"
    assert trencin.commence_time == datetime(2026, 10, 10, 13, 30, tzinfo=UTC)  # epoch ms UTC
    assert trencin.bookmakers[0].key == "monacobet" and trencin.bookmakers[0].last_update == NOW
    m = markets(trencin)
    assert m["h2h_3_way"] == {("1", None): 1.98, ("X", None): 3.79, ("2", None): 3.32}
    totals = [mk for mk in trencin.bookmakers[0].markets if mk.key == "totals"]
    assert [(o.name, o.point, o.price) for o in totals[1].outcomes] == [("Over", 2.5, 1.54), ("Under", 2.5, 2.33)]


def test_monacobet_hockey_is_regulation_time_and_skips_locked_and_low_odds():
    payload = load_sk("monacobet_football_league.json")
    match = payload["esMatches"][0]
    match["betMap"]["2"]["NULL"]["s"] = "L"  # draw locked -> no complete 1X2
    payload["esMatches"][1]["betMap"]["1"]["NULL"]["ov"] = 1.01  # <= 1.01 -> unusable
    events = MonacobetProvider.parse(payload, "hockey", NOW)
    assert all("h2h_3_way" not in markets(e) for e in events)
    assert {k for e in events for k in markets(e)} == {"totals@reg"}
    payload2 = load_sk("monacobet_football_league.json")
    (ev, _) = MonacobetProvider.parse(payload2, "hockey", NOW)
    assert "h2h_3_way@reg" in markets(ev)


def test_monacobet_skips_blocked_and_live_matches():
    payload = load_sk("monacobet_football_league.json")
    payload["esMatches"][0]["blocked"] = True
    payload["esMatches"][1]["live"] = True
    assert MonacobetProvider.parse(payload, "football", NOW) == []


# ------------------------------------------------------------------ DOXXbet
def test_doxxbet_parser_converts_bratislava_time_and_reads_betradar_id():
    events = DoxxbetProvider.parse(load_sk("doxxbet_football.json"), "football", NOW)
    cyprus = next(e for e in events if e.home_team == "Cyprus")
    assert cyprus.away_team == "Lotyšsko"
    assert cyprus.commence_time == datetime(2026, 10, 5, 16, 0, tzinfo=UTC)  # 18:00 CEST
    assert cyprus.betradar_id == "68932418"
    m = markets(cyprus)
    assert m["h2h_3_way"] == {("1", None): 1.67, ("X", None): 3.75, ("2", None): 5.5}
    assert m["double_chance"] == {("1X", None): 1.16, ("12", None): 1.28, ("X2", None): 2.23}


def test_doxxbet_skips_inactive_odds():
    payload = load_sk("doxxbet_football.json")
    payload["Odds"]["769610250_X"]["Status"] = "suspended"
    cyprus = next(e for e in DoxxbetProvider.parse(payload, "football", NOW) if e.home_team == "Cyprus")
    assert "h2h_3_way" not in markets(cyprus) and "double_chance" in markets(cyprus)


def test_bratislava_time_winter_and_summer():
    assert bratislava_to_utc(datetime(2026, 1, 10, 20, 0)) == datetime(2026, 1, 10, 19, 0, tzinfo=UTC)
    assert bratislava_to_utc(datetime(2026, 7, 10, 20, 0)) == datetime(2026, 7, 10, 18, 0, tzinfo=UTC)
    # 2026 DST ends Sunday 25 October 03:00 CEST -> 02:00 CET
    assert bratislava_to_utc(datetime(2026, 10, 25, 12, 0)) == datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
    assert bratislava_to_utc(datetime(2026, 10, 24, 12, 0)) == datetime(2026, 10, 24, 10, 0, tzinfo=UTC)


# ------------------------------------------------------------------ Niké
def test_nike_parser():
    events = NikeProvider.parse(load_sk("nike_football.json"), "football", NOW)
    assert [(e.home_team, e.away_team) for e in events] == [("FC Košice", "KFC Komárno"), ("Podbrezová", "Spartak Trnava")]
    kosice = events[0]
    assert kosice.betradar_id is None
    assert kosice.commence_time == datetime(2026, 10, 10, 13, 30, tzinfo=UTC)
    m = markets(kosice)
    assert m["h2h_3_way"] == {("1", None): 1.69, ("X", None): 4.1, ("2", None): 4.3}
    assert m["double_chance"] == {("1X", None): 1.2, ("12", None): 1.21, ("X2", None): 2.1}


def test_nike_skips_locked_selection_and_non_match_markets():
    payload = load_sk("nike_football.json")
    payload["bets"][0]["selectionGrid"][0][1]["locked"] = True
    events = NikeProvider.parse(payload, "football", NOW)
    assert "h2h_3_way" not in markets(events[0])
    assert all("Belgicko" not in e.home_team for e in events)  # "Umiestnenie" outright is ignored


def test_nike_hockey_is_regulation_time_and_tennis_two_way():
    payload = load_sk("nike_football.json")
    assert "h2h_3_way@reg" in markets(NikeProvider.parse(payload, "hockey", NOW)[0])
    bet = payload["bets"][0]
    bet["selectionGrid"] = [[bet["selectionGrid"][0][0], bet["selectionGrid"][0][2]]]
    (tennis,) = [e for e in NikeProvider.parse(payload, "tennis", NOW) if e.home_team == "FC Košice"]
    assert markets(tennis) == {"h2h": {("1", None): 1.69, ("2", None): 4.3}}


def test_nike_paging_follows_has_more_bets_and_stops_on_repeat():
    first = load_sk("nike_football.json")
    second = load_sk("nike_football.json")
    for b in second["bets"]:
        b["betId"] += "0"
    second["hasMoreBets"] = True
    session = FakeSession(FakeResponse(json_body=first), FakeResponse(json_body=second), FakeResponse(json_body=second))
    p = NikeProvider(client=client(session))
    p.fetch_odds("football")
    urls = [u for u, _ in session.calls]
    assert len(urls) == 3 and "menu=/futbal" in urls[0] and urls[1].endswith("&page=2") and urls[2].endswith("&page=3")


# ------------------------------------------------------------------ Tipos / Synot (protobuf)
def test_synot_parser_decodes_protobuf():
    events = SynotProvider.parse(load_sk("synot_football_standard_events.json"), "football", NOW)
    assert [(e.home_team, e.away_team) for e in events] == [
        ("Kosovo", "Rakúsko"), ("Malta", "Andorra"), ("Grécko", "Nemecko"), ("Holandsko", "Srbsko")]
    kosovo = events[0]
    assert kosovo.id == "synot:3771133" and kosovo.betradar_id == "68932732"
    assert kosovo.commence_time == datetime(2026, 10, 4, 16, 0, tzinfo=UTC)
    assert markets(kosovo)["h2h_3_way"] == {("1", None): 3.82, ("X", None): 3.48, ("2", None): 2.08}  # float32 rounded
    assert kosovo.bookmakers[0].title == "Synot tip"


def test_tipos_uses_same_parser():
    (ev, *_) = TiposProvider.parse(load_sk("synot_football_standard_events.json"), "football", NOW)
    assert ev.bookmakers[0].key == "tipos" and ev.id.startswith("tipos:")


def test_tipos_bad_result_is_an_error():
    with pytest.raises(ValueError):
        TiposProvider.parse({"Result": 0, "ReturnValue": ""}, "football", NOW)


def _pb(field, wire, payload):
    key = bytes([(field << 3) | wire])
    if wire == 2:
        return key + bytes([len(payload)]) + payload
    return key + payload


def test_protobuf_reader_basics():
    buf = _pb(1, 0, b"\x96\x01") + _pb(2, 2, "Zápas".encode()) + _pb(3, 5, struct.pack("<f", 1.5)) + _pb(4, 2, _pb(1, 0, b"\x05"))
    m = Message(buf)
    assert m.int(1) == 150 and m.text(2) == "Zápas" and m.float32(3) == 1.5 and m.message(4).int(1) == 5
    assert Message.try_parse(b"\x0a\x05ab") is None  # truncated -> not a message
    assert Message(b"\x08\x01\x0a\x05ab", lenient=True).int(1) == 1


def test_tipos_request_body(monkeypatch):
    session = FakeSession(FakeResponse(json_body=load_sk("synot_football_standard_events.json")))
    events = TiposProvider(client=client(session), options={"top": 50}).fetch_odds("football").events
    url, body = session.calls[0]
    assert url == "https://tipkurz.etipos.sk/WebServices/Api/SportsBettingService.svc/GetWebStandardEvents"
    assert body["CategoryID"] == "28" and body["LanguageID"] == 17 and body["IncludeLiveCategories"] is False
    assert len(body["Token"]) == 32 and int(body["Token"], 16) >= 0
    assert len(events) == 4


def test_unconfigured_sport_is_a_clear_error():
    with pytest.raises(ProviderError, match="category_ids"):
        TiposProvider(client=client(FakeSession())).fetch_odds("hockey")
    p = TiposProvider(client=client(FakeSession()), options={"category_ids": {"hockey": "29"}})
    assert p.supports("hockey")


# ------------------------------------------------------------------ HTTP client
def client(session, **kw):
    return SiteClient("Test", session=session, sleep=lambda s: None, throttle=HostThrottle(), **kw)


def test_403_marks_blocked_and_is_not_retried():
    session = FakeSession(FakeResponse(status=403, text="Forbidden"))
    with pytest.raises(BlockedError):
        client(session).get_json("https://x.example/a")
    assert len(session.calls) == 1


def test_captcha_page_marks_blocked():
    session = FakeSession(FakeResponse(status=200, text_json=False, text="<html><div id='px-captcha'></div></html>"))
    with pytest.raises(BlockedError):
        client(session).get_json("https://x.example/a")
    session = FakeSession(FakeResponse(status=503, text="<title>Just a moment...</title>"))
    with pytest.raises(BlockedError):
        client(session).get_json("https://x.example/a")


def test_server_errors_and_network_errors_retry_with_backoff():
    sleeps = []
    session = FakeSession(requests.ConnectionError("x"), FakeResponse(status=502), FakeResponse(json_body={"ok": 1}))
    c = SiteClient("T", session=session, sleep=sleeps.append, throttle=HostThrottle(), max_retries=2, retry_backoff=2,
                   monotonic=lambda: 0.0)
    assert c.get_json("https://x.example/a") == {"ok": 1}
    assert sleeps == [2, 1.0, 4, 2.0]  # backoff 2 s, 4 s; plus the per-site 1 req/s spacing (clock frozen)


def test_retries_exhausted_and_429():
    with pytest.raises(ProviderError, match="3 attempts"):
        client(FakeSession(*[FakeResponse(status=500)] * 3)).get_json("https://x.example/a")
    with pytest.raises(RateLimitError):
        client(FakeSession(FakeResponse(status=429, headers={"Retry-After": "120"}))).get_json("https://x.example/a")


def test_throttle_spaces_requests_per_host_at_least_one_second():
    t = HostThrottle()
    assert t.reserve("a", 1.0, 100.0) == 0
    assert t.reserve("a", 1.0, 100.2) == pytest.approx(0.8)
    assert t.reserve("b", 1.0, 100.2) == 0  # other site: independent
    sleeps = []
    session = FakeSession(FakeResponse(json_body=1), FakeResponse(json_body=2))
    c = SiteClient("T", session=session, sleep=sleeps.append, throttle=HostThrottle(), min_interval=0.1, monotonic=lambda: 5.0)
    c.get_json("https://x.example/a")
    c.get_json("https://x.example/b")
    assert sleeps == [1.0]  # min_interval below 1 s is raised to 1 s


def test_sample_files_option_replays_saved_responses(tmp_path):
    import json
    f = tmp_path / "s.json"
    f.write_text(json.dumps(load_sk("doxxbet_football.json")))
    p = DoxxbetProvider(client=client(FakeSession()), options={"sample_files": {"football": str(f)}}, clock=lambda: NOW)
    assert len(p.fetch_odds("football").events) == 2
    assert p.supports("football") and not p.supports("hockey")  # only sports with a sample


def test_nike_tennis_uses_viraz_zapasu_header():
    events = NikeProvider.parse(load_sk("nike_tennis.json"), "tennis", NOW)
    assert [(e.home_team, e.away_team) for e in events] == [
        ("Alexandrova E./Stollar F.", "Bucsa C./Melichar-Martinez N."), ("Aoyama S./Liang E.", "Danilina A./Krawczyk D.")]
    assert markets(events[0]) == {"h2h": {("1", None): 2.65, ("2", None): 1.43}}


def test_nike_winner_header_is_never_used_outside_tennis():
    # In basketball "Víťaz zápasu" would include overtime: it must not be read as the regulation 1X2.
    assert NikeProvider.parse(load_sk("nike_tennis.json"), "basketball", NOW) == []

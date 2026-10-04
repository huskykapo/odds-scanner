import base64
import json
import threading
from datetime import timedelta

from odds_scanner.discovery import (
    DiscoveryCache,
    classify,
    discover,
    probe_doxxbet,
    probe_tipos,
    sport_from_name,
)
from odds_scanner.engine import LiveEngine, Source
from odds_scanner.arbitrage import FinderSettings
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome
from odds_scanner.providers.base import FetchResult
from odds_scanner.providers.sk import DoxxbetProvider, SynotProvider
from odds_scanner.providers.sk.http import HostThrottle, SiteClient
from tests.conftest import NOW, FakeResponse


# ------------------------------------------------------------------ tiny protobuf writer for fake Tipos replies
def _varint(n):
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def fld(num, value):
    if isinstance(value, int):
        return _varint(num << 3) + _varint(value)
    data = value.encode() if isinstance(value, str) else value
    return _varint(num << 3 | 2) + _varint(len(data)) + data


def tipos_reply(cat_id, name, betradar_ids=()):
    events = b"".join(
        fld(5, fld(1, 100 + i) + fld(2, f"A{i} - B{i}") + fld(4, fld(1, 1791129600000)) + fld(5, br) + fld(6, fld(1, 62)))
        for i, br in enumerate(betradar_ids))
    league = fld(2, fld(1, "xx1") + fld(2, "Liga") + events)
    sport = fld(1, fld(1, str(cat_id)) + fld(2, name)) + fld(4, league)
    return {"Result": 1, "ReturnValue": base64.b64encode(fld(1, fld(1, sport))).decode()}


class Site:
    """Fake requests.Session answering POSTs from a function of the JSON body."""

    def __init__(self, answer):
        self.answer = answer
        self.bodies = []

    def post(self, url, json=None, timeout=None):
        self.bodies.append(json)
        body = self.answer(json)
        return body if isinstance(body, FakeResponse) else FakeResponse(json_body=body)

    def close(self):
        pass


def client(session):
    return SiteClient("T", session=session, sleep=lambda s: None, throttle=HostThrottle(), max_retries=0)


# ------------------------------------------------------------------ rules
def test_sport_names_are_exact():
    assert sport_from_name("Hokej") == "hockey" and sport_from_name("Ľadový hokej") == "hockey"
    assert sport_from_name("Basketbal") == "basketball" and sport_from_name("Tenis") == "tennis"
    for other in ("Stolný tenis", "Pozemný hokej", "Hokejbal", "Americký futbal", "Basketbal 3x3", None):
        assert sport_from_name(other) is None


def test_classify_needs_a_clear_majority():
    ref = {"hockey": {"1", "2", "3", "4"}, "football": {"9", "10"}}
    assert classify({"1", "2", "77"}, ref) == "hockey"
    assert classify({"1", "9"}, ref) is None  # ambiguous
    assert classify({"1", "2", "3", "9"}, ref) == "hockey"  # 3 vs 1
    assert classify({"55"}, ref) is None


# ------------------------------------------------------------------ Tipos / Synot: by category name
def test_tipos_discovery_by_category_name():
    names = {28: "Futbal", 29: "Hokej", 30: "Stolný tenis", 31: "Tenis", 33: "Basketbal"}

    def answer(body):
        cid = int(body["CategoryID"])
        return tipos_reply(cid, names[cid]) if cid in names else {"Result": 0, "ReturnValue": None}

    site = Site(answer)
    p = SynotProvider(client=client(site))
    found = discover(p, probe_tipos, ["hockey", "basketball", "tennis"], {}, skip=["28"])
    assert found == {"hockey": 29, "tennis": 31, "basketball": 33}
    tried = [int(b["CategoryID"]) for b in site.bodies]
    assert 28 not in tried and max(tried) == 33  # stops as soon as everything is found


def test_tipos_discovery_falls_back_to_betradar_ids():
    def answer(body):
        cid = int(body["CategoryID"])
        return tipos_reply(cid, "Nezname", ["501", "502", "503"] if cid == 5 else ["900"])

    p = SynotProvider(client=client(Site(answer)))
    ref = {"hockey": {"501", "502", "503", "504"}, "football": {"900"}}
    assert discover(p, probe_tipos, ["hockey"], ref, max_candidate=10) == {"hockey": 5}


# ------------------------------------------------------------------ DOXXbet: by Betradar ids vs known sports
def test_doxxbet_discovery_by_betradar_overlap():
    def answer(body):
        sid = body["sport"]
        ids = {54: ["1", "2"], 67: ["11", "12", "13"], 63: ["21", "22"], 70: []}.get(sid, [])
        return {"EventChanceTypes": [{"SportID": sid, "BetradarStatisticsUrn": f"sr:match:{i}"} for i in ids], "Odds": {}}

    ref = {"football": {"1", "2"}, "hockey": {"11", "12", "13", "14"}, "basketball": {"21", "22", "23"}, "tennis": {"31"}}
    site = Site(answer)
    p = DoxxbetProvider(client=client(site))
    found = discover(p, probe_doxxbet, ["hockey", "basketball", "tennis"], ref, skip=[54], max_candidate=80)
    assert found == {"basketball": 63, "hockey": 67}  # tennis: no overlapping events
    assert all(b["date"] == "TM" for b in site.bodies)


def test_discovery_survives_errors_and_gives_up_after_many():
    p = DoxxbetProvider(client=client(Site(lambda body: FakeResponse(status=500))))
    assert discover(p, probe_doxxbet, ["hockey"], {"hockey": {"1"}}) == {}


# ------------------------------------------------------------------ cache + engine
def test_cache_roundtrip_and_retry_window(tmp_path):
    c = DiscoveryCache(tmp_path / "d.json")
    c.record("doxxbet", found={"hockey": 67}, tried=["tennis"], now=NOW)
    c2 = DiscoveryCache(tmp_path / "d.json")
    assert c2.found("doxxbet") == {"hockey": 67}
    assert not c2.due("doxxbet", "tennis", NOW + timedelta(hours=2))
    assert c2.due("doxxbet", "tennis", NOW + timedelta(days=2)) and c2.due("tipos", "tennis", NOW)


def test_engine_discovers_and_starts_polling_new_sport(tmp_path):
    def answer(body):
        cid = int(body["CategoryID"])
        return tipos_reply(cid, {28: "Futbal", 29: "Hokej"}.get(cid, "Iné"))

    p = SynotProvider(client=client(Site(answer)))
    src = Source("synot", "Synot tip", p, ["football"], 60, skipped_sports=["hockey"])
    eng = LiveEngine([src], FinderSettings(), clock=lambda: NOW)
    eng.states["synot"].status = "ok"  # first poll done
    cache = DiscoveryCache(tmp_path / "d.json")
    t = eng.start_discovery(cache)
    assert "looking up" in src.note
    t.join(10)  # stops right after finding hockey at 29
    assert src.sports == ["football", "hockey"] and src.skipped_sports == [] and src.note == ""
    assert p.sport_params["hockey"] == 29
    assert json.loads((tmp_path / "d.json").read_text())["synot"]["found"] == {"hockey": 29}
    # nothing left to look up next time
    assert eng.start_discovery(cache) is None

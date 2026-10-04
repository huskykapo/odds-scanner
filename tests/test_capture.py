import json
import zipfile

from odds_scanner.capture import capture
from odds_scanner.engine import Source
from odds_scanner.errors import BlockedError
from odds_scanner.providers.sk import DoxxbetProvider, NikeProvider
from odds_scanner.providers.sk.http import HostThrottle, SiteClient
from tests.conftest import NOW, FakeResponse, load_sk


class Site:
    def __init__(self, answer):
        self.answer = answer

    def post(self, url, json=None, timeout=None):
        return self.answer()

    def get(self, url, params=None, timeout=None):
        return self.answer()

    def close(self):
        pass


def client(answer):
    return SiteClient("T", session=Site(answer), sleep=lambda s: None, throttle=HostThrottle(), max_retries=0)


def test_capture_zips_raw_responses_and_reports_blocked_sites(tmp_path):
    doxx = DoxxbetProvider(client=client(lambda: FakeResponse(json_body=load_sk("doxxbet_football.json"))), clock=lambda: NOW)
    nike = NikeProvider(client=client(lambda: FakeResponse(status=403)), clock=lambda: NOW)
    lines = []
    path = capture([Source("doxxbet", "DOXXbet", doxx, ["football"], 60), Source("nike", "Niké", nike, ["football"], 60)],
                   lines.append, tmp_path, NOW)
    names = zipfile.ZipFile(path).namelist()
    assert path.name == "capture-20261004-1200.zip"
    assert "doxxbet/football_1.json" in names and "INFO.txt" in names and not any(n.startswith("nike/") for n in names)
    saved = json.loads(zipfile.ZipFile(path).read("doxxbet/football_1.json"))
    assert saved["EventChanceTypes"][0]["EventName"] == "Cyprus vs. Lotyšsko"
    assert any("BLOCKED" in l for l in lines) and "Send this file" in lines[-1]


def test_capture_with_nothing_saved_leaves_no_file(tmp_path):
    nike = NikeProvider(client=client(lambda: FakeResponse(status=403)), clock=lambda: NOW)
    assert capture([Source("nike", "Niké", nike, ["football"], 60)], lambda l: None, tmp_path, NOW) is None
    assert list(tmp_path.iterdir()) == []


def test_capture_saves_match_detail_pages(tmp_path):
    from datetime import timedelta

    from odds_scanner.providers.sk import SynotProvider

    later = NOW - timedelta(days=1)  # sample kick-offs are 4-5 Oct; pretend it is the day before
    calls = []

    class Recording(Site):
        def post(self, url, json=None, timeout=None):
            calls.append((url, json))
            if url.endswith("GetOfferEventDetail"):
                return FakeResponse(json_body={"detail": json["eventId"]})
            if url.endswith("GetWebStandardEventExt"):
                return FakeResponse(json_body={"Result": 1, "ReturnValue": "", "id": json["EventID"]})
            return self.answer()

    def cl(answer):
        return SiteClient("T", session=Recording(answer), sleep=lambda s: None, throttle=HostThrottle(), max_retries=0)

    doxx = DoxxbetProvider(client=cl(lambda: FakeResponse(json_body=load_sk("doxxbet_football.json"))), options={"dates": ["TM"], "top_values": [1]}, clock=lambda: later)
    synot = SynotProvider(client=cl(lambda: FakeResponse(json_body=load_sk("synot_football_standard_events.json"))), options={"top": 50}, clock=lambda: later)
    path = capture([Source("doxxbet", "DOXXbet", doxx, ["football"], 60), Source("synot", "Synot tip", synot, ["football"], 60)],
                   lambda l: None, tmp_path, later)
    names = set(zipfile.ZipFile(path).namelist())
    assert {"doxxbet/detail_80003338.json", "doxxbet/detail_80003323.json"} <= names
    assert "synot/detail_3771133.json" in names and "synot/detail_3771133_longpolling.json" in names
    assert len([n for n in names if n.startswith("synot/detail_")]) == 5  # 4 matches + 1 long-polling variant
    detail = json.loads(zipfile.ZipFile(path).read("doxxbet/detail_80003338.json"))
    assert detail["response"] == {"detail": 80003338} and detail["_event"] == "Cyprus vs Lotyšsko"
    bodies = [b for u, b in calls if u.endswith("GetWebStandardEventExt")]
    assert bodies[0]["EventID"] == 3771133 and bodies[0]["LanguageID"] == 17 and len(bodies[0]["Token"]) == 32
    assert [b["UseLongPolling"] for b in bodies[:2]] == [False, True]
    assert ("https://www.doxxbet.sk/offer/GetOfferEventDetail", {"eventId": 80003338}) in calls or \
        ("https://www.doxxbet.sk/offer/GetOfferEventDetail", {"eventId": 80003323}) in calls


def test_capture_saves_match_pages_of_other_sports_too(tmp_path):
    from datetime import timedelta

    later = NOW - timedelta(days=1)

    class Pages(Site):
        def post(self, url, json=None, timeout=None):
            if url.endswith("GetOfferEventDetail"):
                return FakeResponse(json_body={"detail": json["eventId"]})
            return self.answer()

    cl = SiteClient("T", session=Pages(lambda: FakeResponse(json_body=load_sk("doxxbet_football.json"))),
                    sleep=lambda s: None, throttle=HostThrottle(), max_retries=0)
    doxx = DoxxbetProvider(client=cl, options={"dates": ["TM"], "top_values": [1], "sport_ids": {"hockey": 4}}, clock=lambda: later)
    path = capture([Source("doxxbet", "DOXXbet", doxx, ["hockey"], 60)], lambda l: None, tmp_path, later)
    names = set(zipfile.ZipFile(path).namelist())
    assert {"doxxbet/hockey_detail_80003338.json", "doxxbet/hockey_detail_80003323.json"} <= names

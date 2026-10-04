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

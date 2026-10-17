"""Near misses through the live engine, the dashboard server and the config."""

import json
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from odds_scanner import cli
from odds_scanner.config import ConfigError, config_from_dict
from odds_scanner.dashboard import Dashboard
from odds_scanner.engine import LiveEngine
from tests.conftest import NOW
from tests.test_engine_e2e import SETTINGS, mono_nike_sources

# The sample match is a +0.92 % arb; with a 2 % alert threshold it becomes a near miss.
STRICT = replace(SETTINGS, min_profit_percent=2.0)


def make_engine(settings=STRICT, **kw):
    return LiveEngine(list(mono_nike_sources()), settings, clock=lambda: NOW, **kw)


def test_engine_collects_near_misses_when_enabled():
    eng = make_engine(near_miss_floor=3.0)
    eng.run_once()
    assert eng.arbs == []
    (m,) = eng.near_misses
    assert m.event_name == "FC Košice vs Komárno" and m.profit_percent == pytest.approx(0.9158, abs=1e-3)


def test_engine_near_misses_off_by_default_and_with_zero():
    for kw in ({}, {"near_miss_floor": 0}, {"near_miss_floor": None}):
        eng = make_engine(**kw)
        eng.run_once()
        assert eng.near_misses == []
        assert eng.near_miss_snapshot()["enabled"] is False


def test_engine_real_arb_is_not_also_a_near_miss():
    eng = make_engine(SETTINGS, near_miss_floor=3.0)  # 0.5 % threshold: it is a genuine arb
    eng.run_once()
    assert len(eng.arbs) == 1 and eng.near_misses == []


def test_near_miss_limit():
    eng = make_engine(near_miss_floor=3.0, near_miss_limit=0)
    eng.run_once()
    assert eng.near_misses == []


def test_near_miss_snapshot_is_json_with_everything_the_page_needs():
    eng = make_engine(near_miss_floor=3.0, dashboard_options={"refresh_seconds": 4})
    eng.run_once()
    snap = json.loads(json.dumps(eng.near_miss_snapshot()))
    assert snap["enabled"] and snap["floor"] == 3.0 and snap["min_profit"] == 2.0 and snap["refresh_seconds"] == 4
    (item,) = snap["items"]
    assert item["event"] == "FC Košice vs Komárno" and item["market_label"] == "1X2" and item["profit"] == pytest.approx(0.916, abs=1e-3)
    assert [l["outcome_label"] for l in item["legs"]] == ["1 (FC Košice)", "X (draw)", "2 (Komárno)"]
    assert [l["bookmaker"] for l in item["legs"]] == ["MONACObet", "Niké", "Niké"]
    assert item["legs"][0]["odds"] == 1.69 and item["legs"][0]["updated"].endswith("Z")


def test_state_json_for_the_main_dashboard_is_unchanged():
    eng = make_engine(near_miss_floor=3.0)
    eng.run_once()
    assert "near_misses" not in eng.snapshot()  # friend's /api/state contract stays as it was


# ------------------------------------------------------------------ web server
def _get(port, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(f"http://127.0.0.1:{port}{path}", timeout=5)


def test_dashboard_serves_near_miss_page_and_api():
    eng = make_engine(near_miss_floor=3.0)
    eng.run_once()
    dash = Dashboard(eng.snapshot, "127.0.0.1", 0, get_near_misses=eng.near_miss_snapshot)
    dash.start()
    try:
        page = _get(dash.port, "/near-misses").read().decode()
        assert "Near misses" in page and "/api/near-misses" in page and "innerHTML" not in page
        data = json.loads(_get(dash.port, "/api/near-misses").read())
        assert data["items"][0]["event"] == "FC Košice vs Komárno"
        assert "Near misses" in _get(dash.port, "/").read().decode()  # link from the main page
    finally:
        dash.stop()


def test_near_miss_routes_are_absent_when_not_configured():
    eng = make_engine()
    dash = Dashboard(eng.snapshot, "127.0.0.1", 0)
    dash.start()
    try:
        for path in ("/near-misses", "/api/near-misses"):
            with pytest.raises(urllib.error.HTTPError) as ei:
                _get(dash.port, path)
            assert ei.value.code == 404
    finally:
        dash.stop()


# ------------------------------------------------------------------ config / cli
def test_near_miss_config_default_validation_and_cli_wiring():
    cfg = config_from_dict({})
    assert cfg.near_miss_percent == 3.0
    assert config_from_dict({"near_miss_percent": 0}).near_miss_percent == 0
    for bad in (-1, "x", None):
        with pytest.raises(ConfigError, match="near_miss_percent"):
            config_from_dict({"near_miss_percent": bad})
    assert cli.build_engine(cfg)._near_floor == 3.0
    assert cli.build_engine(config_from_dict({"near_miss_percent": 0}))._near_floor is None


# ------------------------------------------------------------------ Ctrl+C must stop the scanner (Windows)
def test_run_live_waits_with_a_timeout_so_ctrl_c_works_on_windows(monkeypatch):
    """A bare Event().wait() cannot be interrupted by Ctrl+C on Windows; every wait needs a timeout."""
    timeouts = []

    class InterruptingEvent:
        def wait(self, timeout=None):
            timeouts.append(timeout)
            if len(timeouts) == 3:
                raise KeyboardInterrupt
            return False

    eng = make_engine()  # built first: it needs real threading.Events
    monkeypatch.setattr(eng, "start", lambda **kw: None)
    monkeypatch.setattr(eng, "start_discovery", lambda cache: None)
    monkeypatch.setattr(cli, "DiscoveryCache", lambda: None)
    monkeypatch.setattr(cli.threading, "Event", InterruptingEvent)
    assert cli.run_live(config_from_dict({}), eng, once=False, dashboard=False) == 0
    assert timeouts == [0.5, 0.5, 0.5]  # never an untimed wait

from datetime import datetime, time, timedelta, timezone

import pytest

from odds_scanner.config import ConfigError, config_from_dict
from odds_scanner.engine import LiveEngine, Source
from odds_scanner.schedule import Window, parse_window
from tests.conftest import NOW
from tests.test_engine_e2e import SETTINGS, Fixed

DAY = parse_window("08:00-23:00")
NIGHT = parse_window("22:00-06:00")
LOCAL = timezone(timedelta(hours=2))


def at(h, m=0, day=5):
    return datetime(2026, 10, day, h, m, tzinfo=LOCAL)


def test_parse_and_str():
    assert parse_window(" 8:00 - 23:00 ") == Window(time(8), time(23)) and str(DAY) == "08:00-23:00"
    for bad in ("", "8-23", "25:00-26:00", "08:60-09:00", "08:00", None, "08:00-23:00-01:00"):
        with pytest.raises(ValueError):
            parse_window(bad)


def test_contains_day_and_overnight_windows():
    assert DAY.contains(time(8, 0)) and DAY.contains(time(22, 59)) and not DAY.contains(time(23, 0)) and not DAY.contains(time(3))
    assert NIGHT.contains(time(23)) and NIGHT.contains(time(2)) and not NIGHT.contains(time(12)) and NIGHT.contains(time(22, 0))
    assert Window(time(0), time(0)).contains(time(13))  # equal bounds = always on


def test_seconds_until_open():
    assert DAY.seconds_until_open(at(12)) == 0
    assert DAY.seconds_until_open(at(23, 30)) == 8.5 * 3600  # tomorrow 08:00
    assert DAY.seconds_until_open(at(3)) == 5 * 3600  # today 08:00
    assert NIGHT.seconds_until_open(at(12)) == 10 * 3600


def engine(window, hour):
    src = Source("x", "X", Fixed([]), ["football"], 60, active_hours=window)
    eng = LiveEngine([src], SETTINGS, clock=lambda: NOW, local_now=lambda d: at(hour))
    return eng, src


def test_engine_pauses_outside_active_hours_and_says_so():
    eng, src = engine(DAY, 3)
    eng._stop.wait = lambda t: waits.append(t)  # record instead of sleeping
    waits = []
    assert eng._sleep_until_active(src) is False
    assert waits == [300.0] and eng.states["x"].status == "sleeping"
    assert "paused outside active hours (08:00-23:00)" in eng.states["x"].message and "5.0 h" in eng.states["x"].message
    assert src.provider.calls == 0  # nothing was fetched


def test_engine_polls_inside_active_hours_and_without_a_window():
    eng, src = engine(DAY, 12)
    assert eng._sleep_until_active(src) is True
    eng, src = engine(None, 3)
    assert eng._sleep_until_active(src) is True


def test_a_blocked_source_keeps_its_status_while_outside_hours():
    eng, src = engine(DAY, 3)
    eng._stop.wait = lambda t: None
    eng.states["x"].status = "blocked"
    eng._sleep_until_active(src)
    assert eng.states["x"].status == "blocked"


def test_provider_loop_does_not_poll_outside_hours():
    eng, src = engine(DAY, 3)
    calls = {"n": 0}

    def fake_wait(t):
        calls["n"] += 1
        if calls["n"] >= 3:
            eng._stop.set()

    eng._stop.wait = fake_wait
    eng._provider_loop(src)
    assert src.provider.calls == 0 and calls["n"] == 3


def test_config_validation_and_cli_wiring():
    cfg = config_from_dict({"providers": {"nike": {"active_hours": "08:00-23:00"}}})
    assert cfg.providers["nike"].active_hours == "08:00-23:00" and cfg.providers["doxxbet"].active_hours is None
    with pytest.raises(ConfigError, match="active_hours"):
        config_from_dict({"providers": {"nike": {"active_hours": "morning"}}})
    from odds_scanner import cli

    nike = next(s for s in cli.build_sources(cfg) if s.key == "nike")
    assert str(nike.active_hours) == "08:00-23:00"
    assert next(s for s in cli.build_sources(cfg) if s.key == "doxxbet").active_hours is None

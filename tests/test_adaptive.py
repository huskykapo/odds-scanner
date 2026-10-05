from datetime import timedelta

import pytest

from odds_scanner.adaptive import AdaptiveSettings, adaptive_interval, soonest_start
from odds_scanner.config import ConfigError, config_from_dict
from odds_scanner.engine import LiveEngine, Source
from odds_scanner.models import Event
from tests.conftest import NOW
from tests.test_engine_e2e import SETTINGS, Fixed, mono_nike_sources

S = AdaptiveSettings()  # soon 3h x0.5, far 24h x2.0, floor 30, ceiling 300


def hours(h):
    return timedelta(hours=h)


# ------------------------------------------------------------------ the pure function
@pytest.mark.parametrize(
    "base, soonest, expected",
    [
        (60, hours(1), 30),  # a match within 3 h: twice as often
        (60, hours(3), 30),  # boundary counts as soon
        (60, hours(10), 60),  # normal
        (60, hours(24), 60),  # boundary of far is still normal
        (60, hours(30), 120),  # nothing within 24 h: half as often
        (60, None, 120),  # nothing upcoming at all
        (120, hours(1), 60),
        (120, hours(30), 240),
        (200, hours(30), 300),  # ceiling
    ],
)
def test_tiers(base, soonest, expected):
    assert adaptive_interval(base, soonest, S)[0] == expected


def test_faster_is_never_slower_and_slower_is_never_faster_than_the_configured_interval():
    assert adaptive_interval(20, hours(1), S)[0] == 20  # floor 30 > base 20: stay at the configured 20
    assert adaptive_interval(500, hours(30), S)[0] == 500  # ceiling 300 < base 500: stay at 500
    assert adaptive_interval(40, hours(1), S)[0] == 30  # floor applies when base allows it


def test_disabled_returns_the_configured_interval():
    assert adaptive_interval(60, hours(1), AdaptiveSettings(enabled=False)) == (60, "adaptive polling off")


def test_reason_is_reported():
    assert "starts within 3h" in adaptive_interval(60, hours(1), S)[1]
    assert "nothing starts within 24h" in adaptive_interval(60, None, S)[1]


def test_soonest_start_ignores_started_matches():
    now = NOW
    assert soonest_start([now - hours(1), now + hours(5), now + hours(2)], now) == hours(2)
    assert soonest_start([now - hours(1)], now) is None and soonest_start([], now) is None


# ------------------------------------------------------------------ in the engine
def engine(adaptive, starts):
    mono, nike = mono_nike_sources()
    events = [Event("e", "football", "Football", NOW + hours(h), "A", "B", ()) for h in starts]
    src = Source("x", "X", Fixed(events), ["football"], 60)
    return LiveEngine([src], SETTINGS, clock=lambda: NOW, adaptive=adaptive), src


def test_engine_polls_sooner_when_a_match_is_about_to_start():
    eng, src = engine(S, [1, 40])
    assert eng.poll(src) == 30 and src.current_interval == 30
    assert eng.snapshot()["providers"][0]["interval"] == 30  # the dashboard shows what is really used


def test_engine_polls_later_when_nothing_starts_soon_and_default_is_off():
    eng, src = engine(S, [40, 50])
    assert eng.poll(src) == 120
    eng, src = engine(None, [1])
    assert eng.poll(src) == 60  # not configured -> unchanged behaviour


def test_failures_keep_their_exponential_backoff():
    src = Source("x", "X", Fixed(__import__("odds_scanner.errors", fromlist=["ProviderError"]).ProviderError("HTTP 500")), ["football"], 60)
    eng = LiveEngine([src], SETTINGS, clock=lambda: NOW, adaptive=S)
    assert eng.poll(src) == 120 and eng.poll(src) == 240


# ------------------------------------------------------------------ config
def test_config_defaults_and_validation():
    a = config_from_dict({}).adaptive_polling
    assert a.enabled and (a.soon_hours, a.soon_factor, a.far_hours, a.far_factor) == (3.0, 0.5, 24.0, 2.0)
    for bad, text in [({"soon_factor": 0}, "soon_factor"), ({"soon_factor": 1.5}, "soon_factor"), ({"far_factor": 0.5}, "far_factor"),
                      ({"soon_hours": 30}, "soon_hours"), ({"min_interval_seconds": 900}, "min_interval"), ({"enabled": "yes"}, "enabled"),
                      ({"nope": 1}, "unknown config key")]:
        with pytest.raises(ConfigError, match=text):
            config_from_dict({"adaptive_polling": bad})


def test_cli_wires_the_settings():
    from odds_scanner import cli

    eng = cli.build_engine(config_from_dict({"adaptive_polling": {"soon_factor": 0.25, "min_interval_seconds": 10}}))
    assert eng._adaptive.soon_factor == 0.25 and eng._adaptive.min_interval == 10
    assert not cli.build_engine(config_from_dict({"adaptive_polling": {"enabled": False}}))._adaptive.enabled

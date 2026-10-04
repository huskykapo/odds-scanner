import pytest

from odds_scanner.config import Config, config_from_dict, load_config
from odds_scanner.errors import ConfigError


def test_defaults_are_valid():
    cfg = config_from_dict({})
    assert isinstance(cfg, Config)
    assert cfg.min_profit_percent == 0.5
    assert cfg.provider.api_key_env == "ODDS_API_KEY"
    # works with no API key at all: Slovak providers on, The Odds API off
    assert [n for n, p in cfg.providers.items() if p.enabled] == ["monacobet", "doxxbet", "nike", "tipos", "synot"]
    assert not cfg.providers["the_odds_api"].enabled
    assert cfg.providers["monacobet"].poll_interval_seconds == 120
    assert cfg.providers["nike"].poll_interval_seconds == 60
    assert cfg.dashboard.port == 8765 and cfg.verify_above_percent == 10
    assert cfg.stake_rounding == 0.5


def test_provider_sections_override_defaults_field_by_field():
    cfg = config_from_dict({
        "providers": {"nike": {"poll_interval_seconds": 30, "sports": ["football"]}, "doxxbet": {"enabled": False},
                      "monacobet": {"options": {"league_ids": {"football": [2529497]}}}},
        "bookmaker_settings": {"doxxbet": {"stake_step": 1, "min_stake": 2, "stake_fee": 5}},
        "matching": {"aliases": {"Slovan": "ŠK Slovan Bratislava"}},
    })
    assert cfg.providers["nike"].poll_interval_seconds == 30 and cfg.providers["nike"].sports == ("football",)
    assert cfg.providers["nike"].timeout_seconds == 20  # untouched default
    assert not cfg.providers["doxxbet"].enabled and cfg.providers["tipos"].enabled
    assert cfg.providers["monacobet"].poll_interval_seconds == 120
    assert cfg.providers["monacobet"].options["league_ids"]["football"] == [2529497]
    assert cfg.bookmaker_settings["doxxbet"].stake_fee == 5
    assert cfg.matching.aliases == {"Slovan": "ŠK Slovan Bratislava"}


def test_overrides_and_nested_sections():
    cfg = config_from_dict(
        {
            "sports": ["a", "b"],
            "bankroll": 250,
            "notifications": {"telegram": {"enabled": True}, "dedupe_ttl_minutes": 5},
            "quota": {"stop_below": 3},
        }
    )
    assert cfg.sports == ("a", "b")
    assert cfg.notifications.telegram.enabled is True
    assert cfg.notifications.dedupe_ttl_minutes == 5
    assert cfg.quota.stop_below == 3


@pytest.mark.parametrize(
    "data, fragment",
    [
        ({"bankroll": 0}, "bankroll"),
        ({"bankroll": "lots"}, "bankroll"),
        ({"min_profit_percent": -1}, "min_profit_percent"),
        ({"markets": ["h2h", "outrights"]}, "unsupported markets"),
        ({"sports": []}, "sports must not be empty"),
        ({"regionz": ["eu"]}, "unknown config key"),
        ({"storage": {"backend": "excel"}}, "storage.backend"),
        ({"provider": {"name": "replay"}}, "replay_file"),
        ({"notifications": {"telegram": {"enable": True}}}, "notifications.telegram.enable"),
        ({"min_profit_percent": 5, "max_profit_percent": 2}, "max_profit_percent"),
        ({"log_level": "LOUD"}, "log_level"),
        ("not a mapping", "mapping"),
        ({"providers": {"tipsport": {}}}, "unknown provider"),
        ({"providers": {"nike": {"min_request_interval_seconds": 0.5}}}, "at most one request per second"),
        ({"providers": {"nike": {"pol_interval": 5}}}, "providers.nike.pol_interval"),
        ({"bookmaker_settings": {"nike": {"win_tax": 150}}}, "percentage"),
        ({"matching": {"name_threshold": 2}}, "between 0 and 1"),
        ({"dashboard": {"port": 70000}}, "dashboard.port"),
    ],
)
def test_invalid_config_raises_clear_error(data, fragment):
    with pytest.raises(ConfigError, match=fragment):
        config_from_dict(data)


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("sports: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)


def test_load_config_ok_and_empty_file(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("bankroll: 500\nsports: [soccer_epl]\n")
    assert load_config(p).bankroll == 500
    p.write_text("")
    assert load_config(p).bankroll == 1000.0

import pytest

from odds_scanner.config import Config, config_from_dict, load_config
from odds_scanner.errors import ConfigError


def test_defaults_are_valid():
    cfg = config_from_dict({})
    assert isinstance(cfg, Config)
    assert cfg.min_profit_percent == 1.0
    assert cfg.provider.api_key_env == "ODDS_API_KEY"


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

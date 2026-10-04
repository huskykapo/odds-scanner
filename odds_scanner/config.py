"""Configuration loading and validation (``config.yaml``)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from odds_scanner.errors import ConfigError

# Markets whose outcomes are mutually exclusive AND exhaustive, so that
# "sum of inverse odds < 1" really is a guaranteed profit.
TWO_OR_THREE_WAY_MARKETS = frozenset({"h2h", "h2h_3_way"})
TWO_WAY_MARKETS = frozenset({"totals", "spreads", "btts", "draw_no_bet"})
SUPPORTED_MARKETS = TWO_OR_THREE_WAY_MARKETS | TWO_WAY_MARKETS

PROVIDER_NAMES = ("the_odds_api", "replay")
STORAGE_BACKENDS = ("csv", "sqlite", "both", "none")


def _positive(name: str, value: Any, *, allow_zero: bool = False) -> None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not ok or value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{name} must be a {'non-negative' if allow_zero else 'positive'} number, got {value!r}")


def _str_list(name: str, value: Any) -> None:
    if not isinstance(value, tuple) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{name} must be a list of non-empty strings, got {value!r}")


@dataclass(frozen=True)
class ProviderConfig:
    name: str = "the_odds_api"
    api_key_env: str = "ODDS_API_KEY"
    timeout_seconds: float = 15.0
    max_retries: int = 2
    replay_file: str | None = None
    # Shift replayed timestamps so the sample data looks fresh "now".
    replay_rebase_timestamps: bool = True

    def __post_init__(self) -> None:
        if self.name not in PROVIDER_NAMES:
            raise ConfigError(f"provider.name must be one of {PROVIDER_NAMES}, got {self.name!r}")
        _positive("provider.timeout_seconds", self.timeout_seconds)
        _positive("provider.max_retries", self.max_retries, allow_zero=True)
        if self.name == "replay" and not self.replay_file:
            raise ConfigError("provider.replay_file is required when provider.name is 'replay'")


@dataclass(frozen=True)
class QuotaConfig:
    backoff_below: int = 100  # remaining requests under which we slow down...
    backoff_multiplier: float = 4.0  # ...by multiplying the poll interval
    stop_below: int = 10  # stop polling rather than dip under this many requests

    def __post_init__(self) -> None:
        _positive("quota.backoff_below", self.backoff_below, allow_zero=True)
        _positive("quota.stop_below", self.stop_below, allow_zero=True)
        if not isinstance(self.backoff_multiplier, (int, float)) or self.backoff_multiplier < 1:
            raise ConfigError("quota.backoff_multiplier must be >= 1")


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"


@dataclass(frozen=True)
class NotificationConfig:
    console: bool = True
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    dedupe_ttl_minutes: float = 60.0  # don't re-send an identical arb within this window
    max_per_cycle: int = 10  # cap Telegram messages per poll (rest wait for the next poll)

    def __post_init__(self) -> None:
        _positive("notifications.dedupe_ttl_minutes", self.dedupe_ttl_minutes)
        _positive("notifications.max_per_cycle", self.max_per_cycle)


@dataclass(frozen=True)
class StorageConfig:
    backend: str = "csv"
    csv_path: str = "data/arbs.csv"
    sqlite_path: str = "data/arbs.db"

    def __post_init__(self) -> None:
        if self.backend not in STORAGE_BACKENDS:
            raise ConfigError(f"storage.backend must be one of {STORAGE_BACKENDS}, got {self.backend!r}")


@dataclass(frozen=True)
class Config:
    sports: tuple[str, ...] = ("soccer_epl",)
    regions: tuple[str, ...] = ("eu", "uk")
    markets: tuple[str, ...] = ("h2h",)
    bookmakers: tuple[str, ...] = ()  # whitelist; empty = all bookmakers
    min_profit_percent: float = 1.0
    max_profit_percent: float | None = 25.0  # above this is almost certainly bad data; None disables
    bankroll: float = 1000.0
    currency: str = "EUR"
    stake_rounding: float = 1.0  # stakes are rounded to a multiple of this
    poll_interval_seconds: float = 300.0
    stale_after_seconds: float = 300.0
    log_level: str = "INFO"
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    def __post_init__(self) -> None:
        for name in ("sports", "regions", "markets"):
            _str_list(name, getattr(self, name))
            if not getattr(self, name):
                raise ConfigError(f"{name} must not be empty")
        _str_list("bookmakers", self.bookmakers)
        bad = [m for m in self.markets if m not in SUPPORTED_MARKETS]
        if bad:
            raise ConfigError(f"unsupported markets {bad}; supported: {sorted(SUPPORTED_MARKETS)}")
        _positive("min_profit_percent", self.min_profit_percent, allow_zero=True)
        if self.max_profit_percent is not None:
            _positive("max_profit_percent", self.max_profit_percent)
            if self.max_profit_percent < self.min_profit_percent:
                raise ConfigError("max_profit_percent must be >= min_profit_percent")
        _positive("bankroll", self.bankroll)
        _positive("stake_rounding", self.stake_rounding)
        _positive("poll_interval_seconds", self.poll_interval_seconds)
        _positive("stale_after_seconds", self.stale_after_seconds)
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"log_level is not a valid logging level: {self.log_level!r}")


def _build(cls: type, data: Any, section: str, nested: Mapping[str, Callable[[Any], Any]] | None = None) -> Any:
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"'{section or 'config'}' must be a mapping, got {type(data).__name__}")
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        prefix = f"{section}." if section else ""
        raise ConfigError(f"unknown config key(s): {', '.join(prefix + k for k in sorted(unknown))}")
    nested = nested or {}
    kwargs = {k: nested[k](v) if k in nested else (tuple(v) if isinstance(v, list) else v) for k, v in data.items()}
    try:
        return cls(**kwargs)
    except ConfigError:
        raise
    except TypeError as exc:  # e.g. wrong type fed to a comparison
        raise ConfigError(f"invalid value in '{section or 'config'}': {exc}") from exc


def config_from_dict(data: Mapping[str, Any] | None) -> Config:
    """Build a validated :class:`Config` from a parsed YAML mapping (missing keys use defaults)."""
    return _build(
        Config,
        data,
        "",
        nested={
            "provider": lambda v: _build(ProviderConfig, v, "provider"),
            "quota": lambda v: _build(QuotaConfig, v, "quota"),
            "storage": lambda v: _build(StorageConfig, v, "storage"),
            "notifications": lambda v: _build(
                NotificationConfig,
                v,
                "notifications",
                nested={"telegram": lambda t: _build(TelegramConfig, t, "notifications.telegram")},
            ),
        },
    )


def load_config(path: str | Path) -> Config:
    """Load ``config.yaml``. Raises :class:`ConfigError` with a clear message on any problem."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror or exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {path} is not valid YAML: {exc}") from exc
    return config_from_dict(data)

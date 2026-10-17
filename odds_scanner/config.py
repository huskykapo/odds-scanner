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

PROVIDER_NAMES = ("the_odds_api", "replay")  # legacy single-provider mode (--replay)
SK_SOURCES = ("monacobet", "doxxbet", "nike", "tipos", "synot")
SOURCE_NAMES = SK_SOURCES + ("the_odds_api",)
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
class SourceConfig:
    """One entry under ``providers:`` - an odds source polled in its own background thread."""

    enabled: bool = True
    poll_interval_seconds: float = 60.0
    timeout_seconds: float = 20.0
    max_retries: int = 2
    retry_backoff_seconds: float = 2.0
    min_request_interval_seconds: float = 1.0  # per site; values below 1 are refused
    sports: tuple[str, ...] | None = None  # None = the top-level ``sports`` list
    options: Mapping[str, Any] = field(default_factory=dict)  # provider-specific, see config.yaml
    active_hours: str | None = None  # e.g. "08:00-23:00" local time: no polling outside it (saves metered API requests)

    def __post_init__(self) -> None:
        if self.active_hours is not None:
            from odds_scanner.schedule import parse_window

            try:
                parse_window(self.active_hours)
            except ValueError as exc:
                raise ConfigError(f"providers.*.active_hours: {exc}") from exc
        if self.options is None:
            object.__setattr__(self, "options", {})
        if not isinstance(self.enabled, bool):
            raise ConfigError("providers.*.enabled must be true or false")
        _positive("providers.*.poll_interval_seconds", self.poll_interval_seconds)
        _positive("providers.*.timeout_seconds", self.timeout_seconds)
        _positive("providers.*.max_retries", self.max_retries, allow_zero=True)
        _positive("providers.*.retry_backoff_seconds", self.retry_backoff_seconds, allow_zero=True)
        _positive("providers.*.min_request_interval_seconds", self.min_request_interval_seconds)
        if self.min_request_interval_seconds < 1.0:
            raise ConfigError("providers.*.min_request_interval_seconds must be >= 1 (at most one request per second per site)")
        if self.sports is not None:
            _str_list("providers.*.sports", self.sports)
        if not isinstance(self.options, Mapping):
            raise ConfigError("providers.*.options must be a mapping")


def default_sources() -> dict[str, SourceConfig]:
    return {
        # The whole-sport MONACObet feed is ~5 MB: poll it less often (or set options.league_ids).
        "monacobet": SourceConfig(poll_interval_seconds=120.0),
        "doxxbet": SourceConfig(),
        "nike": SourceConfig(),
        "tipos": SourceConfig(),
        "synot": SourceConfig(),
        "the_odds_api": SourceConfig(enabled=False, poll_interval_seconds=600.0, timeout_seconds=15.0, sports=("soccer_epl",)),
    }


@dataclass(frozen=True)
class BookmakerConfig:
    """Money rules for one bookmaker (keyed by bookmaker key under ``bookmaker_settings:``)."""

    stake_step: float | None = None  # round stakes to this; None = top-level stake_rounding
    min_stake: float = 0.0
    stake_fee: float = 0.0  # percent of each stake the bookmaker keeps
    win_tax: float = 0.0  # percent of net winnings withheld

    def __post_init__(self) -> None:
        if self.stake_step is not None:
            _positive("bookmaker_settings.*.stake_step", self.stake_step)
        _positive("bookmaker_settings.*.min_stake", self.min_stake, allow_zero=True)
        for name in ("stake_fee", "win_tax"):
            value = getattr(self, name)
            _positive(f"bookmaker_settings.*.{name}", value, allow_zero=True)
            if value >= 100:
                raise ConfigError(f"bookmaker_settings.*.{name} is a percentage and must be below 100")


@dataclass(frozen=True)
class MatchingConfig:
    time_tolerance_minutes: float = 15.0
    name_threshold: float = 0.8
    aliases: Mapping[str, str] = field(default_factory=dict)  # bookmaker spelling -> other spelling

    def __post_init__(self) -> None:
        if self.aliases is None:  # a key whose entries are all commented out
            object.__setattr__(self, "aliases", {})
        _positive("matching.time_tolerance_minutes", self.time_tolerance_minutes)
        _positive("matching.name_threshold", self.name_threshold)
        if self.name_threshold > 1:
            raise ConfigError("matching.name_threshold must be between 0 and 1")
        if not isinstance(self.aliases, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in self.aliases.items()
        ):
            raise ConfigError("matching.aliases must map names to names")


@dataclass(frozen=True)
class DetailsConfig:
    """Match pages (over/under, handicaps, both teams to score ...) of DOXXbet, Tipos and Synot."""

    enabled: bool = True
    sports: tuple[str, ...] = ("football",)  # bet mapping verified for football
    horizon_hours: float = 24.0  # only matches starting within this many hours
    refresh_seconds: float = 240.0  # re-fetch each match page this often
    pause_seconds: float = 3.0  # wait between two match pages of the same site
    max_matches: int = 150  # per site, soonest kick-off first

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigError("details.enabled must be true or false")
        _str_list("details.sports", self.sports)
        _positive("details.horizon_hours", self.horizon_hours)
        _positive("details.refresh_seconds", self.refresh_seconds)
        _positive("details.pause_seconds", self.pause_seconds, allow_zero=True)
        _positive("details.max_matches", self.max_matches)


@dataclass(frozen=True)
class DashboardConfig:
    enabled: bool = True
    host: str = "0.0.0.0"  # all interfaces, so a phone on the same Wi-Fi can open it
    port: int = 8765
    refresh_seconds: float = 3.0
    highlight_seconds: float = 120.0  # arbs first seen this recently are highlighted
    open_browser: bool = True  # open the dashboard in the default browser at start-up

    def __post_init__(self) -> None:
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not 0 < self.port < 65536:
            raise ConfigError(f"dashboard.port must be a TCP port number, got {self.port!r}")
        _positive("dashboard.refresh_seconds", self.refresh_seconds)
        _positive("dashboard.highlight_seconds", self.highlight_seconds, allow_zero=True)


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
    min_profit_percent: float | None = None  # alert threshold; None = the top-level min_profit_percent


@dataclass(frozen=True)
class AdaptivePollingConfig:
    """Poll a bookmaker faster when a match there starts soon, slower when nothing starts for a day."""

    enabled: bool = True
    soon_hours: float = 3.0  # a match starts within this -> faster
    soon_factor: float = 0.5  # ...polling every (configured interval x this)
    far_hours: float = 24.0  # nothing starts within this -> slower
    far_factor: float = 2.0
    min_interval_seconds: float = 30.0  # never faster than this (the 1 request/s per site rule still applies)
    max_interval_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigError("adaptive_polling.enabled must be true or false")
        _positive("adaptive_polling.soon_hours", self.soon_hours)
        _positive("adaptive_polling.far_hours", self.far_hours)
        _positive("adaptive_polling.min_interval_seconds", self.min_interval_seconds)
        _positive("adaptive_polling.max_interval_seconds", self.max_interval_seconds)
        if not (isinstance(self.soon_factor, (int, float)) and 0 < self.soon_factor <= 1):
            raise ConfigError("adaptive_polling.soon_factor must be between 0 (exclusive) and 1")
        if not (isinstance(self.far_factor, (int, float)) and self.far_factor >= 1):
            raise ConfigError("adaptive_polling.far_factor must be 1 or more")
        if self.soon_hours >= self.far_hours:
            raise ConfigError("adaptive_polling.soon_hours must be smaller than far_hours")
        if self.min_interval_seconds > self.max_interval_seconds:
            raise ConfigError("adaptive_polling.min_interval_seconds must not exceed max_interval_seconds")


@dataclass(frozen=True)
class NotificationConfig:
    console: bool = True
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    dedupe_ttl_minutes: float = 60.0  # don't re-send an identical arb within this window
    max_per_cycle: int = 10  # cap Telegram messages per poll (rest wait for the next poll)
    dashboard_url: str | None = None  # shown as "OPEN DASHBOARD" in Telegram alerts, e.g. http://192.168.1.20:8765

    def __post_init__(self) -> None:
        _positive("notifications.dedupe_ttl_minutes", self.dedupe_ttl_minutes)
        _positive("notifications.max_per_cycle", self.max_per_cycle)
        if self.dashboard_url is not None and not str(self.dashboard_url).startswith(("http://", "https://")):
            raise ConfigError("notifications.dashboard_url must start with http:// or https://")


@dataclass(frozen=True)
class ValidationConfig:
    """Checks an arbitrage must pass before it is announced, and the notify-once rules."""

    max_odds_age_seconds: float = 180.0  # every leg's price must be at most this old
    confirm_polls: int = 1  # fresh polls of EACH involved bookmaker needed to confirm it (0 = off: alert at once)
    renotify_roi_delta: float = 1.0  # alert again if the ROI moved by this many percentage points...
    renotify_min_interval_seconds: float = 300.0  # ...but not more often than this
    gone_after_seconds: float = 30.0  # unseen for this long = the opportunity ended (a return is announced again)

    def __post_init__(self) -> None:
        _positive("validation.max_odds_age_seconds", self.max_odds_age_seconds)
        _positive("validation.confirm_polls", self.confirm_polls, allow_zero=True)
        _positive("validation.renotify_roi_delta", self.renotify_roi_delta)
        _positive("validation.renotify_min_interval_seconds", self.renotify_min_interval_seconds, allow_zero=True)
        _positive("validation.gone_after_seconds", self.gone_after_seconds)


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
    # Slovak providers: football, hockey, basketball, tennis. (Legacy --replay / Odds API mode: sport keys.)
    sports: tuple[str, ...] = ("football", "hockey", "basketball", "tennis")
    regions: tuple[str, ...] = ("eu", "uk")
    markets: tuple[str, ...] = ("h2h",)
    bookmakers: tuple[str, ...] = ()  # whitelist; empty = all bookmakers
    min_profit_percent: float = 0.5
    max_profit_percent: float | None = 25.0  # above this is almost certainly bad data; None disables
    verify_above_percent: float | None = 10.0  # shown with a "verify manually" flag above this
    near_miss_percent: float = 3.0  # also list the closest non-arbs, down to this % loss; 0 turns it off
    bankroll: float = 1000.0
    currency: str = "EUR"
    stake_rounding: float = 0.5  # stakes are rounded to a multiple of this (per bookmaker: stake_step)
    poll_interval_seconds: float = 300.0
    stale_after_seconds: float = 300.0
    log_level: str = "INFO"
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    providers: Mapping[str, SourceConfig] = field(default_factory=default_sources)
    bookmaker_settings: Mapping[str, BookmakerConfig] = field(default_factory=dict)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    details: DetailsConfig = field(default_factory=DetailsConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    adaptive_polling: AdaptivePollingConfig = field(default_factory=AdaptivePollingConfig)

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
        if self.verify_above_percent is not None:
            _positive("verify_above_percent", self.verify_above_percent)
        _positive("near_miss_percent", self.near_miss_percent, allow_zero=True)
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


def _build_sources(data: Any) -> dict[str, SourceConfig]:
    """Listed providers override the defaults field by field; unlisted ones keep their defaults."""
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError("'providers' must be a mapping of provider name -> settings")
    unknown = set(data) - set(SOURCE_NAMES)
    if unknown:
        raise ConfigError(f"unknown provider(s) {sorted(unknown)}; known: {', '.join(SOURCE_NAMES)}")
    sources = default_sources()
    for name, raw in data.items():
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            raise ConfigError(f"providers.{name} must be a mapping")
        base = {f.name: getattr(sources[name], f.name) for f in fields(SourceConfig)}
        sources[name] = _build(SourceConfig, {**base, **raw}, f"providers.{name}")
    return sources


def _build_bookmakers(data: Any) -> dict[str, BookmakerConfig]:
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError("'bookmaker_settings' must be a mapping of bookmaker key -> settings")
    return {str(k): _build(BookmakerConfig, v, f"bookmaker_settings.{k}") for k, v in data.items()}


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
            "providers": _build_sources,
            "bookmaker_settings": _build_bookmakers,
            "matching": lambda v: _build(MatchingConfig, v, "matching"),
            "dashboard": lambda v: _build(DashboardConfig, v, "dashboard"),
            "details": lambda v: _build(DetailsConfig, v, "details"),
            "validation": lambda v: _build(ValidationConfig, v, "validation"),
            "adaptive_polling": lambda v: _build(AdaptivePollingConfig, v, "adaptive_polling"),
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

"""Shared pieces of the Slovak bookmaker providers: base class, event builder, local time."""

from __future__ import annotations

import json
import logging
import time
from abc import abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from odds_scanner.errors import ProviderError
from odds_scanner.markets import (
    DC_CODES,
    DOUBLE_CHANCE,
    H2H,
    H2H_3_WAY,
    OVER,
    TOTALS,
    TWO_WAY_SPORTS,
    UNDER,
    period_for,
    with_period,
)
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome
from odds_scanner.providers.base import FetchResult, OddsProvider
from odds_scanner.providers.sk.http import SiteClient

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ time
def _last_sunday(year: int, month: int) -> datetime:
    day = datetime(year, month, 31)  # March and October both have 31 days
    return day - timedelta(days=(day.weekday() - 6) % 7)


def bratislava_to_utc(local: datetime) -> datetime:
    """Convert a naive Europe/Bratislava wall-clock time to aware UTC.

    EU rule: CEST (UTC+2) from the last Sunday of March 01:00 UTC to the last Sunday of
    October 01:00 UTC, otherwise CET (UTC+1). Implemented here so Windows needs no tz database.
    """
    if local.tzinfo is not None:
        return local.astimezone(timezone.utc)
    start = _last_sunday(local.year, 3).replace(hour=1, tzinfo=timezone.utc)
    end = _last_sunday(local.year, 10).replace(hour=1, tzinfo=timezone.utc)
    as_summer = local.replace(tzinfo=timezone.utc) - timedelta(hours=2)
    if start <= as_summer < end:
        return as_summer
    return local.replace(tzinfo=timezone.utc) - timedelta(hours=1)


def epoch_ms_to_utc(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def betradar_digits(value: Any) -> str | None:
    """``sr:match:68932418`` / ``68932418`` / ``68932418`` (int) -> ``"68932418"``."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().rsplit(":", 1)[-1]
    return text if text.isdigit() and int(text) > 0 else None


# ------------------------------------------------------------------ event building
def build_markets(
    sport: str,
    prices: Mapping[str, float],
    totals: Mapping[float, Mapping[str, float]] | None,
    fetched_at: datetime,
) -> tuple[MarketOdds, ...]:
    """Turn normalised prices into finder markets.

    ``prices`` uses the codes ``1``/``X``/``2``/``1X``/``12``/``X2`` (already filtered for
    suspended/locked odds and odds <= 1.01). Hockey/basketball markets get the regulation-time
    period. A 1X2 with a missing outcome is dropped (a suspended draw must not create a fake
    two-way); tennis gets a two-way winner instead.
    """
    period = period_for(sport)
    markets: list[MarketOdds] = []
    if sport in TWO_WAY_SPORTS:
        if "1" in prices and "2" in prices and "X" not in prices:
            markets.append(MarketOdds(H2H, (Outcome("1", prices["1"]), Outcome("2", prices["2"])), fetched_at))
    else:
        if all(code in prices for code in ("1", "X", "2")):
            markets.append(
                MarketOdds(with_period(H2H_3_WAY, period), tuple(Outcome(c, prices[c]) for c in ("1", "X", "2")), fetched_at)
            )
        dc = [Outcome(c, prices[c]) for c in DC_CODES if c in prices]
        if dc:
            markets.append(MarketOdds(with_period(DOUBLE_CHANCE, period), tuple(dc), fetched_at))
    for line, ou in sorted((totals or {}).items()):
        if OVER in ou and UNDER in ou:
            markets.append(
                MarketOdds(
                    with_period(TOTALS, period),
                    (Outcome(OVER, ou[OVER], line), Outcome(UNDER, ou[UNDER], line)),
                    fetched_at,
                )
            )
    return tuple(markets)


def make_event(
    *,
    bookmaker: str,
    title: str,
    native_id: Any,
    sport: str,
    start: datetime,
    home: str,
    away: str,
    prices: Mapping[str, float],
    fetched_at: datetime,
    totals: Mapping[float, Mapping[str, float]] | None = None,
    betradar_id: str | None = None,
    url: str | None = None,
) -> Event | None:
    """One provider event with a single bookmaker; None when no usable market is left."""
    markets = build_markets(sport, prices, totals, fetched_at)
    if not markets or not home or not away:
        return None
    return Event(
        id=f"{bookmaker}:{native_id}",
        sport_key=sport,
        sport_title=sport.title(),
        commence_time=start,
        home_team=home.strip(),
        away_team=away.strip(),
        bookmakers=(
            BookmakerOdds(
                key=bookmaker, title=title, markets=markets, last_update=fetched_at, url=url,
                event_name=f"{home.strip()} - {away.strip()}",
            ),
        ),
        betradar_id=betradar_id,
    )


def split_teams(name: str, separators: Sequence[str] = (" - ", " vs. ", " vs ", " – ")) -> tuple[str, str] | None:
    for sep in separators:
        if sep in name:
            home, away = name.split(sep, 1)
            if home.strip() and away.strip():
                return home.strip(), away.strip()
    return None


# ------------------------------------------------------------------ provider base
class SlovakProvider(OddsProvider):
    """Base for the Slovak bookmaker scrapers of public JSON endpoints.

    Subclasses define ``name``, ``title``, ``homepage``, ``DEFAULT_SPORTS`` (our sport ->
    site-specific parameter) and implement :meth:`_fetch_payloads` and :meth:`parse`.

    ``options`` (from ``config.yaml`` ``providers.<name>.options``) can override the
    per-sport parameters (e.g. ``sport_ids: {hockey: 67}``) and set ``sample_file`` to replay a
    saved response instead of calling the site (offline demo / tests).
    """

    name = "slovak"
    title = "Slovak bookmaker"
    homepage = ""
    SPORT_OPTION = "sport_ids"
    DEFAULT_SPORTS: Mapping[str, Any] = {}

    def __init__(
        self,
        *,
        options: Mapping[str, Any] | None = None,
        client: SiteClient | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
        retry_backoff: float = 2.0,
        min_interval: float = 1.0,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.options = dict(options or {})
        self._client = client or SiteClient(
            self.title, timeout=timeout, max_retries=max_retries, retry_backoff=retry_backoff,
            min_interval=min_interval, sleep=sleep,
        )
        self._clock = clock
        sports = dict(self.DEFAULT_SPORTS)
        sports.update({k: v for k, v in (self.options.get(self.SPORT_OPTION) or {}).items()})
        self.sport_params = {k: v for k, v in sports.items() if v not in (None, "")}

    def supports(self, sport: str) -> bool:
        return sport in self.sport_params

    def fetch_odds(
        self,
        sport: str,
        *,
        regions: Sequence[str] = (),
        markets: Sequence[str] = (),
        bookmakers: Sequence[str] = (),
    ) -> FetchResult:
        if not self.supports(sport):
            raise ProviderError(
                f"{self.title}: no site parameter configured for sport {sport!r} "
                f"(set providers.{self.name}.options.{self.SPORT_OPTION}.{sport})"
            )
        sample = self.options.get("sample_file")
        payloads = [self._load_sample(sample)] if sample else self._fetch_payloads(sport, self.sport_params[sport])
        fetched_at = self._clock()
        events: list[Event] = []
        seen: set[str] = set()
        for payload in payloads:
            try:
                parsed = self._parse(payload, sport, fetched_at)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise ProviderError(f"{self.title}: unexpected response format ({type(exc).__name__}: {exc})") from exc
            for ev in parsed:
                if ev.id not in seen:
                    seen.add(ev.id)
                    events.append(ev)
        log.debug("%s: %d %s event(s)", self.title, len(events), sport)
        return FetchResult(events=events)

    @abstractmethod
    def _fetch_payloads(self, sport: str, param: Any) -> list[Any]:
        """Download the raw response(s) for one sport."""

    @classmethod
    @abstractmethod
    def parse(cls, payload: Any, sport: str, fetched_at: datetime) -> list[Event]:
        """Parse one raw response into events (pure: used by the offline tests)."""

    def _parse(self, payload: Any, sport: str, fetched_at: datetime) -> list[Event]:
        """Hook for subclasses whose parser takes options."""
        return self.parse(payload, sport, fetched_at)

    def _load_sample(self, path: str) -> Any:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderError(f"{self.title}: cannot read sample_file {path}: {exc}") from exc

    def close(self) -> None:
        self._client.close()

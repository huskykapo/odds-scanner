"""The polling loop: fetch -> find arbitrages -> notify and log, while respecting the API quota."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from odds_scanner.arbitrage import FinderSettings, find_arbitrages
from odds_scanner.config import Config
from odds_scanner.errors import (
    AuthenticationError,
    ProviderError,
    QuotaExhaustedError,
    QuotaLowError,
    RateLimitError,
)
from odds_scanner.models import Arbitrage, QuotaInfo
from odds_scanner.notifiers.base import Notifier
from odds_scanner.providers.base import OddsProvider
from odds_scanner.storage.base import ArbLog

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_QUOTA = 3
EXIT_AUTH = 4

MAX_BACKOFF_SECONDS = 3600.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CycleResult:
    arbs: list[Arbitrage] = field(default_factory=list)
    events_scanned: int = 0
    sports_ok: int = 0
    errors: int = 0
    quota: QuotaInfo | None = None
    rate_limited_for: float | None = None  # seconds the provider asked us to wait
    fatal: ProviderError | None = None  # a failure that retrying cannot fix


class Scanner:
    def __init__(
        self,
        config: Config,
        provider: OddsProvider,
        *,
        notifiers: Sequence[Notifier] = (),
        logs: Sequence[ArbLog] = (),
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cfg = config
        self._provider = provider
        self._notifiers = list(notifiers)
        self._logs = list(logs)
        self._clock = clock
        self._sleep = sleep
        self._settings = FinderSettings(
            bankroll=config.bankroll,
            min_profit_percent=config.min_profit_percent,
            max_profit_percent=config.max_profit_percent,
            stale_after=timedelta(seconds=config.stale_after_seconds),
            stake_rounding=config.stake_rounding,
            bookmakers=frozenset(config.bookmakers),
        )
        self._quota: QuotaInfo | None = None
        self._failed_cycles = 0

    # ------------------------------------------------------------------ one cycle
    def scan_once(self) -> CycleResult:
        """Poll every configured sport once. Never raises for provider problems - see ``fatal``."""
        result = CycleResult(quota=self._quota)
        for sport in self._cfg.sports:
            low = self._quota_floor_breached()
            if low:
                result.fatal = low
                break
            try:
                fetched = self._provider.fetch_odds(
                    sport,
                    regions=self._cfg.regions,
                    markets=self._cfg.markets,
                    bookmakers=self._cfg.bookmakers,
                )
            except (AuthenticationError, QuotaExhaustedError) as exc:
                result.fatal = exc
                break
            except RateLimitError as exc:
                log.warning("rate limited by provider (retry after %s s); ending this cycle early", exc.retry_after)
                result.errors += 1
                result.rate_limited_for = exc.retry_after or self._cfg.poll_interval_seconds
                break
            except ProviderError as exc:
                log.error("fetching %s failed: %s", sport, exc)
                result.errors += 1
                continue

            if fetched.quota is not None:
                self._quota = result.quota = fetched.quota
            result.sports_ok += 1
            result.events_scanned += len(fetched.events)
            found = find_arbitrages(fetched.events, self._settings, now=self._clock())
            log.debug("%s: %d event(s), %d arbitrage(s)", sport, len(fetched.events), len(found))
            result.arbs.extend(found)

        result.arbs.sort(key=lambda a: (-a.realized_profit_percent, a.event_name, a.market))
        return result

    def _quota_floor_breached(self) -> QuotaLowError | None:
        """Refuse a request that would take the remaining quota below ``quota.stop_below``."""
        if self._quota is None or self._quota.remaining is None:
            return None
        cost = len(self._cfg.markets) * len(self._cfg.regions)
        floor = self._cfg.quota.stop_below
        if self._quota.remaining - cost < floor:
            return QuotaLowError(
                f"stopping: {self._quota.remaining} request(s) left and a poll costs up to {cost} "
                f"(quota.stop_below={floor})"
            )
        return None

    # ------------------------------------------------------------------ outputs
    def publish(self, result: CycleResult) -> None:
        """Hand a cycle's arbs to notifiers and logs. A failing sink never stops the others."""
        if result.sports_ok == 0 and not result.arbs:
            return  # nothing was fetched; don't claim "no opportunities"
        for sink in (*self._notifiers, *self._logs):
            try:
                if isinstance(sink, Notifier):
                    sink.notify(result.arbs)
                else:
                    sink.append(result.arbs)
            except Exception:  # noqa: BLE001 - isolation boundary between independent sinks
                log.exception("%s failed", type(sink).__name__)

    # ------------------------------------------------------------------ loop
    def next_delay(self, result: CycleResult) -> float:
        """Seconds to wait before the next poll: slower when quota is low or polls are failing."""
        delay = self._cfg.poll_interval_seconds
        q = self._cfg.quota
        remaining = result.quota.remaining if result.quota else None
        if remaining is not None and remaining <= q.backoff_below:
            delay *= q.backoff_multiplier
            log.warning("quota low (%d left): slowing polling to every %.0fs", remaining, delay)
        if result.rate_limited_for:
            delay = max(delay, result.rate_limited_for)
        self._failed_cycles = self._failed_cycles + 1 if result.errors and not result.sports_ok else 0
        if self._failed_cycles:
            delay = min(delay * 2 ** min(self._failed_cycles, 4), max(MAX_BACKOFF_SECONDS, delay))
        return delay

    def run(self, *, once: bool = False) -> int:
        """Poll until interrupted (or once). Returns a process exit code."""
        try:
            while True:
                result = self.scan_once()
                self.publish(result)
                if result.fatal is not None:
                    log.error("%s", result.fatal)
                    return EXIT_AUTH if isinstance(result.fatal, AuthenticationError) else EXIT_QUOTA
                if once:
                    return EXIT_OK
                delay = self.next_delay(result)
                log.info("next poll in %.0fs", delay)
                self._sleep(delay)
        except KeyboardInterrupt:
            log.info("interrupted, shutting down")
            return EXIT_OK
        finally:
            self.close()

    def close(self) -> None:
        for obj in (self._provider, *self._logs):
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                log.exception("error closing %s", type(obj).__name__)

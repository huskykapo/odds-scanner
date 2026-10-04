"""Live multi-bookmaker engine: one polling thread per provider, one analyzer thread.

Each provider thread fetches its sports, stores the events as that provider's latest snapshot and
sleeps for its own poll interval. The analyzer merges all snapshots (event matching), finds
arbitrages, hands *new* ones to the notifiers and logs, and keeps the state the web dashboard
shows. A provider that answers with a 403 / bot-check page is marked "blocked" and its thread
stops - it is never retried automatically.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from odds_scanner.arbitrage import FinderSettings, find_arbitrages
from odds_scanner.errors import (
    AuthenticationError,
    BlockedError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitError,
)
from odds_scanner.markets import market_title, outcome_title
from odds_scanner.matching import MatchSettings, MatchStats, match_events
from odds_scanner.models import Arbitrage, Event
from odds_scanner.notifiers.base import Notifier
from odds_scanner.notifiers.dedupe import DedupeCache
from odds_scanner.providers.base import OddsProvider
from odds_scanner.storage.base import ArbLog

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 3600.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if t else None


@dataclass
class Source:
    """A provider plus how to poll it."""

    key: str
    title: str
    provider: OddsProvider
    sports: list[str]
    poll_interval: float
    homepage: str = ""
    skipped_sports: list[str] = field(default_factory=list)  # requested but not configured for this site
    fetch_kwargs: dict[str, Any] = field(default_factory=dict)  # regions/markets for The Odds API
    quota_floor: int | None = None  # stop below this many remaining requests (The Odds API)


@dataclass
class ProviderState:
    key: str
    title: str
    status: str = "starting"  # starting | ok | error | blocked | stopped
    message: str = ""
    events: int = 0
    last_update: datetime | None = None
    last_attempt: datetime | None = None
    failures: int = 0

    def as_dict(self, interval: float, homepage: str) -> dict[str, Any]:
        return {
            "key": self.key, "title": self.title, "status": self.status, "message": self.message,
            "events": self.events, "last_update": _iso(self.last_update), "last_attempt": _iso(self.last_attempt),
            "interval": interval, "homepage": homepage,
        }


class LiveEngine:
    def __init__(
        self,
        sources: Sequence[Source],
        settings: FinderSettings,
        *,
        match_settings: MatchSettings | None = None,
        notifiers: Sequence[Notifier] = (),
        logs: Sequence[ArbLog] = (),
        dedupe_ttl: timedelta = timedelta(minutes=60),
        currency: str = "EUR",
        dashboard_options: dict[str, Any] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.sources = list(sources)
        self._settings = settings
        self._match = match_settings or MatchSettings()
        self._notifiers = list(notifiers)
        self._logs = list(logs)
        self._clock = clock
        self._currency = currency
        self._dash = dashboard_options or {}
        self._lock = threading.Lock()
        self._snapshots: dict[tuple[str, str], list[Event]] = {}
        self.states = {s.key: ProviderState(s.key, s.title) for s in self.sources}
        for s in self.sources:
            if not s.sports:
                self.states[s.key].status = "stopped"
                self.states[s.key].message = "no configured sport for this site"
        self._seen = DedupeCache(dedupe_ttl, clock=clock)
        self._first_seen: dict[str, datetime] = {}
        self.arbs: list[Arbitrage] = []
        self.stats = MatchStats()
        self.last_analysis: datetime | None = None
        self._stop = threading.Event()
        self._changed = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ polling
    def poll(self, source: Source) -> float | None:
        """Fetch every sport of ``source`` once. Returns the delay before the next poll, None to stop."""
        state = self.states[source.key]
        state.last_attempt = self._clock()
        ok, failed = 0, []
        delay = source.poll_interval
        for sport in source.sports:
            if self._stop.is_set():
                return None
            try:
                result = source.provider.fetch_odds(sport, **source.fetch_kwargs)
            except BlockedError as exc:
                log.error("%s is blocking automated requests - stopped polling it: %s", source.title, exc)
                self._finish(state, "blocked", str(exc), source)
                return None
            except (AuthenticationError, QuotaExhaustedError) as exc:
                log.error("%s: %s - stopped polling it", source.title, exc)
                self._finish(state, "stopped", str(exc), source)
                return None
            except RateLimitError as exc:
                failed.append(f"{sport}: rate limited")
                delay = max(delay, exc.retry_after or delay * 2)
                break
            except ProviderError as exc:
                log.warning("%s %s: %s", source.title, sport, exc)
                failed.append(f"{sport}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - a parser bug must not kill the thread
                log.exception("%s %s: unexpected error", source.title, sport)
                failed.append(f"{sport}: {type(exc).__name__}")
                continue
            ok += 1
            with self._lock:
                self._snapshots[(source.key, sport)] = list(result.events)
            q = result.quota
            if source.quota_floor is not None and q is not None and q.remaining is not None and q.remaining < source.quota_floor:
                self._finish(state, "stopped", f"API quota low ({q.remaining} requests left)", source)
                return None
        with self._lock:
            state.events = sum(len(self._snapshots.get((source.key, sp), ())) for sp in source.sports)
        notes = list(failed)
        if source.skipped_sports:
            notes.append("not configured: " + ", ".join(source.skipped_sports))
        state.message = "; ".join(notes)
        if ok:
            state.status, state.failures, state.last_update = "ok", 0, self._clock()
        else:
            state.status = "error"
            state.failures += 1
            delay = min(delay * 2 ** min(state.failures, 4), max(MAX_BACKOFF_SECONDS, delay))
        self._changed.set()
        return delay

    def _finish(self, state: ProviderState, status: str, message: str, source: Source) -> None:
        state.status, state.message = status, message
        with self._lock:
            for sport in source.sports:
                self._snapshots.pop((source.key, sport), None)
            state.events = 0
        self._changed.set()

    # ------------------------------------------------------------------ analysis
    def analyze(self) -> list[Arbitrage]:
        """Match events across providers, find arbs, publish new ones. Returns all current arbs."""
        now = self._clock()
        with self._lock:
            events = [e for s in self.sources for sport in s.sports for e in self._snapshots.get((s.key, sport), ())]
        merged, stats = match_events(events, self._match)
        arbs = find_arbitrages(merged, self._settings, now=now)
        first_seen = {a.identity: self._first_seen.get(a.identity, now) for a in arbs}
        new = [a for a in arbs if not self._seen.is_duplicate(a.dedupe_key)]
        for a in new:
            self._seen.remember(a.dedupe_key)
        with self._lock:
            self.arbs, self.stats, self.last_analysis, self._first_seen = arbs, stats, now, first_seen
        self._publish(arbs, new)
        return arbs

    def _publish(self, arbs: list[Arbitrage], new: list[Arbitrage]) -> None:
        for n in self._notifiers:
            batch = arbs if n.handles_dedupe else new
            if not batch:
                continue
            try:
                n.notify(batch)
            except Exception:  # noqa: BLE001 - isolation boundary between independent sinks
                log.exception("%s failed", type(n).__name__)
        if new:
            for sink in self._logs:
                try:
                    sink.append(new)
                except Exception:  # noqa: BLE001
                    log.exception("%s failed", type(sink).__name__)

    # ------------------------------------------------------------------ dashboard state
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            arbs, stats, analysed, first_seen = list(self.arbs), self.stats, self.last_analysis, self._first_seen
        rules = self._settings
        homepages = {s.key: s.homepage for s in self.sources}
        out_arbs = []
        for a in arbs:
            out_arbs.append({
                "id": a.identity,
                "first_seen": _iso(first_seen.get(a.identity)),
                "event": a.event_name,
                "home": a.home_team,
                "away": a.away_team,
                "sport": a.sport_key,
                "start": _iso(a.commence_time),
                "market": a.market,
                "market_label": market_title(a.market, a.line),
                "line": a.line,
                "profit": round(a.realized_profit_percent, 4),
                "theoretical_profit": round(a.profit_percent, 4),
                "verify": a.verify_manually,
                "push_possible": a.push_possible,
                "total_stake": a.total_stake,
                "guaranteed_profit": a.guaranteed_profit,
                "legs": [
                    {
                        "bookmaker": leg.bookmaker_title,
                        "bookmaker_key": leg.bookmaker_key,
                        "outcome": leg.outcome,
                        "outcome_label": outcome_title(leg.outcome, a.home_team, a.away_team, a.line),
                        "odds": leg.odds,
                        "effective_odds": leg.effective_odds or leg.odds,
                        "stake": leg.stake,
                        "payout": leg.payout,
                        "updated": _iso(leg.odds_updated),
                        "url": leg.url,
                        "homepage": homepages.get(leg.bookmaker_key, ""),
                        "event_name": leg.event_name,
                        "step": rules.rule(leg.bookmaker_key).stake_step or rules.stake_rounding,
                        "min_stake": rules.rule(leg.bookmaker_key).min_stake,
                    }
                    for leg in a.legs
                ],
            })
        return {
            "now": _iso(self._clock()),
            "last_analysis": _iso(analysed),
            "currency": self._currency,
            "bankroll": rules.bankroll,
            "min_profit": rules.min_profit_percent,
            **self._dash,
            "providers": [self.states[s.key].as_dict(s.poll_interval, s.homepage) for s in self.sources],
            "matching": stats.as_dict(),
            "arbs": out_arbs,
        }

    # ------------------------------------------------------------------ threads
    def run_once(self) -> list[Arbitrage]:
        """Poll every provider once (sequentially) and analyse - for ``--once``."""
        for s in self.sources:
            if s.sports:
                self.poll(s)
        return self.analyze()

    def start(self, analyze_every: float = 3.0) -> None:
        for s in self.sources:
            if s.sports:
                t = threading.Thread(target=self._provider_loop, args=(s,), name=f"poll-{s.key}", daemon=True)
                t.start()
                self._threads.append(t)
        t = threading.Thread(target=self._analyzer_loop, args=(analyze_every,), name="analyzer", daemon=True)
        t.start()
        self._threads.append(t)

    def _provider_loop(self, source: Source) -> None:
        while not self._stop.is_set():
            delay = self.poll(source)
            if delay is None:
                return
            log.debug("%s: next poll in %.0fs", source.title, delay)
            self._stop.wait(delay)

    def _analyzer_loop(self, every: float) -> None:
        while not self._stop.is_set():
            self._changed.wait(every)
            self._changed.clear()
            if self._stop.is_set():
                return
            try:
                self.analyze()
            except Exception:  # noqa: BLE001 - keep the dashboard alive
                log.exception("analysis failed")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._changed.set()
        for t in self._threads:
            t.join(timeout)
        for s in self.sources:
            try:
                s.provider.close()
            except Exception:  # noqa: BLE001
                log.exception("error closing %s", s.title)
        for sink in self._logs:
            try:
                sink.close()
            except Exception:  # noqa: BLE001
                log.exception("error closing %s", type(sink).__name__)

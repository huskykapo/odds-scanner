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

from odds_scanner.arbitrage import FinderSettings, analyze_events
from odds_scanner.errors import (
    AuthenticationError,
    BlockedError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitError,
)
from odds_scanner.markets import market_title, outcome_title
from odds_scanner.matching import MatchSettings, MatchStats, match_events
from odds_scanner.models import Arbitrage, Event, NearMiss
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
    note: str = ""  # shown on the dashboard instead of the "not configured" note (sport id lookup)


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
        near_miss_floor: float | None = None,
        near_miss_limit: int = 50,
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
        self._near_floor = near_miss_floor or None  # 0 / None = do not compute near misses
        self._near_limit = near_miss_limit
        self.near_misses: list[NearMiss] = []
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
        if source.note:
            notes.append(source.note)
        elif source.skipped_sports:
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

    # ------------------------------------------------------------------ sport id discovery
    def betradar_reference(self) -> dict[str, set[str]]:
        """Betradar ids of the events seen so far, per sport (to recognise another site's sport ids)."""
        from odds_scanner.markets import sport_family

        ref: dict[str, set[str]] = {}
        with self._lock:
            for events in self._snapshots.values():
                for e in events:
                    if e.betradar_id:
                        ref.setdefault(sport_family(e.sport_key), set()).add(e.betradar_id)
        return ref

    def add_sport(self, source: Source, sport: str, param: Any) -> None:
        """Start polling ``sport`` at ``source`` (found by discovery) from its next cycle on."""
        source.provider.sport_params[sport] = param  # type: ignore[attr-defined]
        if sport not in source.sports:
            source.sports = [*source.sports, sport]
        source.skipped_sports = [s for s in source.skipped_sports if s != sport]
        self._changed.set()

    def start_discovery(self, cache: Any) -> threading.Thread | None:
        """Look up missing sport ids of DOXXbet/Tipos/Synot in the background (see discovery.py)."""
        from odds_scanner.discovery import DISCOVERABLE

        now = self._clock()
        todo = {}
        for s in self.sources:
            if s.key in DISCOVERABLE and not getattr(s.provider, "samples", None) and s.sports:
                wanted = [sp for sp in s.skipped_sports if cache.due(s.key, sp, now)]
                if wanted:
                    todo[s.key] = (s, wanted)
                    s.note = f"looking up the site's ids for {', '.join(wanted)} (one-time, a few minutes)"
                elif s.skipped_sports:
                    s.note = "not found on this site: " + ", ".join(s.skipped_sports)
        if not todo:
            return None
        t = threading.Thread(target=self._discovery_main, args=(todo, cache), name="discovery", daemon=True)
        t.start()
        self._threads.append(t)
        return t

    def _discovery_main(self, todo: dict[str, tuple[Source, list[str]]], cache: Any) -> None:
        # Wait for the first round of polls: their Betradar ids are the reference.
        for _ in range(120):
            if self._stop.is_set() or all(st.status != "starting" for st in self.states.values()):
                break
            self._stop.wait(2)
        reference = self.betradar_reference()
        workers = [
            threading.Thread(target=self._discover_one, args=(src, wanted, reference, cache), name=f"discover-{key}", daemon=True)
            for key, (src, wanted) in todo.items()
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

    def _discover_one(self, src: Source, wanted: list[str], reference: dict[str, set[str]], cache: Any) -> None:
        from odds_scanner.discovery import PROBES, discover

        def found_one(sport: str, cid: int) -> None:
            self.add_sport(src, sport, cid)
            cache.record(src.key, found={sport: cid})

        try:
            found = discover(
                src.provider, PROBES[src.key], wanted, reference,
                skip=getattr(src.provider, "sport_params", {}).values(), on_found=found_one, stop=self._stop,
            )
        except BlockedError:
            src.note = ""
            return  # normal polling reports the block
        except Exception:  # noqa: BLE001 - never take the scanner down
            log.exception("%s: sport id lookup failed", src.title)
            src.note = ""
            return
        if self._stop.is_set():
            return
        missing = [w for w in wanted if w not in found]
        if missing:
            cache.record(src.key, tried=missing, now=self._clock())
            log.info("%s: no sport id found for %s (will retry tomorrow)", src.title, ", ".join(missing))
        src.note = ("not found on this site: " + ", ".join(missing)) if missing else ""
        self._changed.set()

    # ------------------------------------------------------------------ analysis
    def analyze(self) -> list[Arbitrage]:
        """Match events across providers, find arbs, publish new ones. Returns all current arbs."""
        now = self._clock()
        with self._lock:
            events = [e for s in self.sources for sport in s.sports for e in self._snapshots.get((s.key, sport), ())]
        merged, stats = match_events(events, self._match)
        arbs, near = analyze_events(merged, self._settings, now=now, near_miss_floor=self._near_floor)
        first_seen = {a.identity: self._first_seen.get(a.identity, now) for a in arbs}
        new = [a for a in arbs if not self._seen.is_duplicate(a.dedupe_key)]
        for a in new:
            self._seen.remember(a.dedupe_key)
        with self._lock:
            self.arbs, self.stats, self.last_analysis, self._first_seen = arbs, stats, now, first_seen
            self.near_misses = near[: self._near_limit]
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

    def near_miss_snapshot(self) -> dict[str, Any]:
        """JSON state of the /near-misses page: the closest combinations that are not arbs (yet)."""
        with self._lock:
            near, analysed = list(self.near_misses), self.last_analysis
        homepages = {s.key: s.homepage for s in self.sources}
        items = [
            {
                "id": m.identity,
                "event": m.event_name,
                "sport": m.sport_key,
                "start": _iso(m.commence_time),
                "market_label": market_title(m.market, m.line),
                "profit": round(m.profit_percent, 3),
                "legs": [
                    {
                        "bookmaker": leg.bookmaker_title,
                        "outcome_label": outcome_title(leg.outcome, m.home_team, m.away_team, m.line),
                        "odds": leg.odds,
                        "effective_odds": leg.effective_odds or leg.odds,
                        "updated": _iso(leg.odds_updated),
                        "url": leg.url,
                        "homepage": homepages.get(leg.bookmaker_key, ""),
                    }
                    for leg in m.legs
                ],
            }
            for m in near
        ]
        return {
            "now": _iso(self._clock()),
            "last_analysis": _iso(analysed),
            "enabled": self._near_floor is not None,
            "floor": self._near_floor,
            "min_profit": self._settings.min_profit_percent,
            "refresh_seconds": self._dash.get("refresh_seconds", 5),
            "items": items,
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

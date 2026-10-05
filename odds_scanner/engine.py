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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from odds_scanner.adaptive import AdaptiveSettings, adaptive_interval, soonest_start
from odds_scanner.arbitrage import FinderSettings, analyze_events
from odds_scanner.errors import (
    AuthenticationError,
    BlockedError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitError,
)
from odds_scanner.markets import market_title, outcome_title, sport_family
from odds_scanner.matching import MatchSettings, MatchStats, match_events
from odds_scanner.models import Arbitrage, Event, MarketOdds, NearMiss
from odds_scanner.notifiers.base import Notifier
from odds_scanner.schedule import Window
from odds_scanner.opportunities import GONE, VERIFIED, Opportunity, OpportunityRegistry, ValidationSettings
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
    current_interval: float | None = None  # what adaptive polling chose last (None = the configured interval)
    active_hours: Window | None = None  # poll only inside this local-time window (None = around the clock)


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
    details: int = 0  # match pages currently held (over/under, handicaps ...)

    def as_dict(self, interval: float, homepage: str) -> dict[str, Any]:
        return {
            "key": self.key, "title": self.title, "status": self.status, "message": self.message,
            "events": self.events, "details": self.details, "last_update": _iso(self.last_update), "last_attempt": _iso(self.last_attempt),
            "interval": interval, "homepage": homepage,
        }


@dataclass(frozen=True)
class DetailSettings:
    """Fetching match pages (over/under, handicaps, ...) for matches quoted by 2+ bookmakers."""

    enabled: bool = True
    sports: frozenset[str] = frozenset({"football"})
    horizon: timedelta = timedelta(hours=24)  # only matches starting within this
    refresh: timedelta = timedelta(seconds=240)  # re-fetch a match page after this
    pause_seconds: float = 3.0  # between two match pages of the same site (on top of 1 req/s)
    max_matches: int = 150  # per site, soonest first


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
        detail_settings: DetailSettings | None = None,
        validation: ValidationSettings | None = None,
        adaptive: AdaptiveSettings | None = None,
        local_now: Callable[[datetime], datetime] = lambda d: d.astimezone(),
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
        self._adaptive = adaptive or AdaptiveSettings(enabled=False)  # off unless configured
        self._local_now = local_now  # UTC -> the machine's local time, for active hours
        self.registry = OpportunityRegistry(validation or ValidationSettings(), settings)
        self._generations: dict[str, int] = {src.key: 0 for src in self.sources}  # successful polls per provider
        self.arbs: list[Arbitrage] = []  # current candidates, enriched with status/confidence
        self.stats = MatchStats()
        self.last_analysis: datetime | None = None
        self._stop = threading.Event()
        self._changed = threading.Event()
        self._threads: list[threading.Thread] = []
        self._detail = detail_settings or DetailSettings(enabled=False)
        self._details: dict[str, tuple[tuple[MarketOdds, ...], datetime]] = {}  # provider event id -> markets
        self._detail_tried: dict[str, datetime] = {}  # provider event id -> last attempt (ok or not)
        self._detail_wanted: dict[str, list[tuple[datetime, str, str]]] = {}  # source -> (kick-off, id, sport)

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
            with self._lock:
                self._generations[source.key] += 1
            state.status, state.failures, state.last_update = "ok", 0, self._clock()
            delay = self._next_interval(source)
        else:
            state.status = "error"
            state.failures += 1
            delay = min(delay * 2 ** min(state.failures, 4), max(MAX_BACKOFF_SECONDS, delay))
        self._changed.set()
        return delay

    def _next_interval(self, source: Source) -> float:
        """Delay after a successful poll: shorter when this bookmaker has a match starting soon."""
        now = self._clock()
        with self._lock:
            starts = [e.commence_time for sp in source.sports for e in self._snapshots.get((source.key, sp), ())]
        seconds, reason = adaptive_interval(source.poll_interval, soonest_start(starts, now), self._adaptive)
        source.current_interval = seconds
        log.debug("%s: next poll in %.0fs (%s)", source.title, seconds, reason)
        return seconds

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
        from odds_scanner.discovery import PROBES, QUICK, discover

        def found_one(sport: str, cid: int) -> None:
            self.add_sport(src, sport, cid)
            cache.record(src.key, found={sport: cid})

        try:
            found: dict[str, int] = {}
            quick = QUICK.get(src.key)
            if quick is not None:
                for sport, cid in quick(src.provider).items():
                    if sport in wanted:
                        log.info("%s: %s has sport id %d (all-sports request)", src.title, sport, cid)
                        found[sport] = cid
                        found_one(sport, cid)
            rest = [w for w in wanted if w not in found]
            if rest:
                found.update(discover(
                    src.provider, PROBES[src.key], rest, reference,
                    skip=getattr(src.provider, "sport_params", {}).values(), on_found=found_one, stop=self._stop,
                ))
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
        """Match events across providers, find candidates, validate them and announce what is due."""
        now = self._clock()
        with self._lock:
            events = [e for s in self.sources for sport in s.sports for e in self._snapshots.get((s.key, sport), ())]
            events = [self._with_details(e) for e in events]
        merged, stats = match_events(events, self._match)
        self._plan_details(merged, now)
        candidates, near = analyze_events(merged, self._settings, now=now, near_miss_floor=self._near_floor)
        with self._lock:
            self.registry.update(candidates, now, dict(self._generations))
            arbs = [o.arb for o in self.registry.current()]
            self.arbs, self.stats, self.last_analysis = arbs, stats, now
            self.near_misses = near[: self._near_limit]
        self._publish()
        return arbs

    # ------------------------------------------------------------------ match pages
    def _with_details(self, event: Event) -> Event:
        """The event with its match-page markets added (newer prices last, so they win)."""
        found = self._details.get(event.id)
        if not found or len(event.bookmakers) != 1:
            return event
        book = event.bookmakers[0]
        return replace(event, bookmakers=(replace(book, markets=book.markets + found[0]),))

    def _plan_details(self, merged: list[Event], now: datetime) -> None:
        """Which match pages each site should fetch: matches on 2+ bookmakers, starting soon."""
        if not self._detail.enabled:
            return
        detailed = {s.key for s in self.sources if self._has_pages(s)}
        wanted: dict[str, list[tuple[datetime, str, str]]] = {k: [] for k in detailed}
        for ev in merged:
            if ev.sport_key not in self._detail.sports or not now < ev.commence_time <= now + self._detail.horizon:
                continue
            if len({b.key for b in ev.bookmakers}) < 2:
                continue
            for b in ev.bookmakers:
                if b.key in detailed and b.event_id:
                    wanted[b.key].append((ev.commence_time, b.event_id, ev.sport_key))
        keep: set[str] = set()
        with self._lock:
            for key, rows in wanted.items():
                rows.sort()
                self._detail_wanted[key] = rows[: self._detail.max_matches]
                keep.update(r[1] for r in self._detail_wanted[key])
                self.states[key].details = sum(1 for r in self._detail_wanted[key] if r[1] in self._details)
            for pid in [p for p in self._details if p not in keep]:
                del self._details[pid]

    @staticmethod
    def _has_pages(source: Source) -> bool:
        """The site has match pages to read (and we are not replaying saved samples)."""
        return callable(getattr(source.provider, "fetch_detail", None)) and not getattr(source.provider, "samples", None)

    def _next_detail(self, key: str, now: datetime) -> tuple[str, str] | None:
        with self._lock:
            rows = list(self._detail_wanted.get(key, ()))
        due = [(self._detail_tried.get(pid, datetime.min.replace(tzinfo=timezone.utc)), start, pid, sport)
               for start, pid, sport in rows
               if now - self._detail_tried.get(pid, datetime.min.replace(tzinfo=timezone.utc)) >= self._detail.refresh]
        if not due:
            return None
        _, _, pid, sport = min(due)  # never fetched first, then the oldest; ties: soonest kick-off
        return pid, sport

    def fetch_detail(self, source: Source, pid: str, sport: str) -> bool | None:
        """Fetch one match page. True = stored, False = failed, None = the site blocked us."""
        now = self._clock()
        self._detail_tried[pid] = now
        try:
            markets = source.provider.fetch_detail(pid.split(":", 1)[1], sport)  # type: ignore[attr-defined]
        except BlockedError as exc:
            log.error("%s is blocking automated requests - stopped polling it: %s", source.title, exc)
            self._finish(self.states[source.key], "blocked", str(exc), source)
            return None
        except ProviderError as exc:
            log.debug("%s: match page %s failed: %s", source.title, pid, exc)
            return False
        except Exception:  # noqa: BLE001 - a parser bug must not kill the thread
            log.exception("%s: match page %s: unexpected error", source.title, pid)
            return False
        with self._lock:
            self._details[pid] = (tuple(markets), now)
        self._changed.set()
        return True

    def _detail_loop(self, source: Source) -> None:
        while not self._stop.is_set():
            if self.states[source.key].status in ("blocked", "stopped"):
                return
            target = self._next_detail(source.key, self._clock())
            if target is None:
                self._stop.wait(5)
                continue
            if self.fetch_detail(source, *target) is None:
                return
            self._stop.wait(self._detail.pause_seconds)

    def _publish(self) -> None:
        """Offer each notifier/log the opportunities it has not been told about; ack what it handled.

        What is "due" is decided by the registry (verified; new, returned, or ROI moved). A sink that
        fails, or hands back only part of the batch, is simply not acked and is offered the rest again.
        """
        with self._lock:
            jobs = [(f"notifier:{i}:{type(n).__name__}", n.notify, self.registry.due(f"notifier:{i}:{type(n).__name__}"))
                    for i, n in enumerate(self._notifiers)]
            jobs += [(f"log:{i}:{type(k).__name__}", k.append, self.registry.due(f"log:{i}:{type(k).__name__}", renotify=False))
                     for i, k in enumerate(self._logs)]
        for key, deliver, due in jobs:
            if not due:
                continue
            try:
                handled = deliver([o.arb for o in due])
            except Exception:  # noqa: BLE001 - isolation boundary between independent sinks
                log.exception("%s failed", key.split(":", 2)[2])
                continue
            done = {o.fingerprint for o in due} if handled is None else {a.fingerprint for a in handled}
            with self._lock:
                self.registry.ack(key, [o for o in due if o.fingerprint in done])

    # ------------------------------------------------------------------ RECHECK
    RECHECK_COOLDOWN = 5.0  # seconds between rechecks of the same opportunity

    def _refresh(self, source: Source, sport: str) -> None:
        """Fetch one sport of one provider right now (same throttled client as the poll threads)."""
        result = source.provider.fetch_odds(sport, **source.fetch_kwargs)
        with self._lock:
            self._snapshots[(source.key, sport)] = list(result.events)
            self._generations[source.key] += 1

    def recheck(self, fingerprint: str) -> dict[str, Any]:
        """Re-fetch ONLY the bookmakers (and sport) of one opportunity, recalculate, report the verdict.

        STILL_AVAILABLE only if every involved bookmaker was re-fetched successfully and the arb is
        still found; NO_LONGER_AVAILABLE if it was re-fetched and the arb is gone; UNKNOWN if any
        re-fetch failed (never claims validity from stale data). Read-only: nothing is ever submitted.
        """
        now = self._clock()
        with self._lock:
            opp = self.registry.get(fingerprint)
            if opp is None or opp.status == GONE:
                return {"status": "UNKNOWN", "message": "This opportunity is no longer tracked (it ended, or the scanner restarted).",
                        "opportunity": None, "changes": [], "refreshed": [], "errors": [], "checked_at": _iso(now)}
            if opp.last_recheck is not None and (now - opp.last_recheck).total_seconds() < self.RECHECK_COOLDOWN:
                return {"status": "COOLDOWN", "message": f"Rechecked a moment ago - wait {self.RECHECK_COOLDOWN:.0f} seconds between checks.",
                        "opportunity": self.opportunity_dict(opp), "changes": [], "refreshed": [], "errors": [], "checked_at": _iso(now)}
            opp.last_recheck = now
            before = opp.arb
        by_key = {s.key: s for s in self.sources}
        refreshed: list[str] = []
        errors: list[str] = []
        done: set[tuple[str, str]] = set()
        for leg in before.legs:
            src = by_key.get(leg.bookmaker_key)
            if src is None:
                errors.append(f"{leg.bookmaker_title}: not a configured source")
                continue
            sport = next((sp for sp in src.sports if sp == before.sport_key or sport_family(sp) == sport_family(before.sport_key)), None)
            if sport is None:
                errors.append(f"{leg.bookmaker_title}: sport {before.sport_key!r} is not polled")
                continue
            if (src.key, sport) in done:
                continue
            done.add((src.key, sport))
            try:
                self._refresh(src, sport)
                refreshed.append(leg.bookmaker_title)
            except BlockedError:
                errors.append(f"{leg.bookmaker_title}: the site refused the request (blocked)")
            except ProviderError as exc:
                errors.append(f"{leg.bookmaker_title}: {exc}")
            except Exception as exc:  # noqa: BLE001 - report, never crash the request handler
                errors.append(f"{leg.bookmaker_title}: unexpected {type(exc).__name__}")
        self.analyze()
        with self._lock:
            after = self.registry.get(fingerprint)
            present = after is not None and after.status != GONE and self.registry.seen_now(fingerprint)
            card = self.opportunity_dict(after) if after is not None and present else None
            changes = []
            if present:
                for old, new in zip(before.legs, after.arb.legs):
                    if (old.odds, old.stake) != (new.odds, new.stake):
                        changes.append({"bookmaker": new.bookmaker_title, "outcome": new.outcome, "old_odds": old.odds,
                                        "new_odds": new.odds, "old_stake": old.stake, "new_stake": new.stake})
        checked_at = _iso(self._clock())
        if errors:
            return {"status": "UNKNOWN", "message": "Could not re-fetch every bookmaker, so the opportunity cannot be confirmed: " + "; ".join(errors),
                    "opportunity": card, "changes": changes, "refreshed": refreshed, "errors": errors, "checked_at": checked_at}
        if present and after.status != "FAILED":
            return {"status": "STILL_AVAILABLE", "message": f"Arbitrage still available: ROI {after.roi:.2f}% on the fresh prices.",
                    "opportunity": card, "changes": changes, "refreshed": refreshed, "errors": [], "checked_at": checked_at}
        why = "it failed validation on the fresh prices" if present else "the combination no longer exists on the fresh prices"
        return {"status": "NO_LONGER_AVAILABLE", "message": f"Arbitrage no longer available: {why}.", "opportunity": card,
                "changes": changes, "refreshed": refreshed, "errors": [], "checked_at": checked_at}

    # ------------------------------------------------------------------ dashboard state
    def opportunity_dict(self, opp: Opportunity) -> dict[str, Any]:
        """One opportunity as the dashboard shows it."""
        a, rules = opp.arb, self._settings
        homepages = {s.key: s.homepage for s in self.sources}
        return {
            "id": a.identity,
            "fingerprint": opp.fingerprint,
            "status": opp.status,
            "episode": opp.episode,
            "confidence": opp.confidence.score,
            "confidence_label": opp.confidence.label,
            "confidence_reasons": list(opp.confidence.reasons),
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail, "blocking": c.blocking} for c in opp.validation.checks],
            "first_seen": _iso(opp.first_seen),
            "last_seen": _iso(opp.last_seen),
            "verified_at": _iso(opp.verified_at),
            "odds_age": a.odds_age_seconds,
            "roi": round(a.realized_profit_percent, 4),
            "peak_roi": round(opp.peak_roi, 4),
            "return": round(a.total_stake + a.guaranteed_profit, 2),
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
                    "outcome_label": outcome_title(leg.outcome, a.home_team, a.away_team, a.line, a.market),
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
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            opps, stats, analysed = self.registry.current(), self.stats, self.last_analysis
            out_arbs = [self.opportunity_dict(o) for o in opps]
        rules = self._settings
        return {
            "now": _iso(self._clock()),
            "last_analysis": _iso(analysed),
            "currency": self._currency,
            "bankroll": rules.bankroll,
            "min_profit": rules.min_profit_percent,
            **self._dash,
            "providers": [self.states[s.key].as_dict(s.current_interval or s.poll_interval, s.homepage) for s in self.sources],
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
                        "outcome_label": outcome_title(leg.outcome, m.home_team, m.away_team, m.line, m.market),
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
        """Poll every provider once (sequentially), fetch the wanted match pages, analyse - for ``--once``."""
        for s in self.sources:
            if s.sports:
                self.poll(s)
        arbs = self.analyze()
        if not self._detail.enabled:
            return arbs
        for s in self.sources:
            for _, pid, sport in list(self._detail_wanted.get(s.key, ())):
                if self.fetch_detail(s, pid, sport) is None:
                    break
        return self.analyze()

    def start(self, analyze_every: float = 3.0) -> None:
        for s in self.sources:
            if s.sports:
                t = threading.Thread(target=self._provider_loop, args=(s,), name=f"poll-{s.key}", daemon=True)
                t.start()
                self._threads.append(t)
        if self._detail.enabled:
            for s in self.sources:
                if s.sports and self._has_pages(s):
                    t = threading.Thread(target=self._detail_loop, args=(s,), name=f"detail-{s.key}", daemon=True)
                    t.start()
                    self._threads.append(t)
        t = threading.Thread(target=self._analyzer_loop, args=(analyze_every,), name="analyzer", daemon=True)
        t.start()
        self._threads.append(t)

    MAX_SLEEP = 300.0  # while outside active hours, re-check the clock at least this often

    def _sleep_until_active(self, source: Source) -> bool:
        """True if a poll may happen now. Outside the source's active hours: say so and wait."""
        window = source.active_hours
        if window is None:
            return True
        wait = window.seconds_until_open(self._local_now(self._clock()))
        if wait <= 0:
            return True
        state = self.states[source.key]
        if state.status not in ("blocked", "stopped"):
            state.status, state.message = "sleeping", f"paused outside active hours ({window}); resumes in {wait / 3600:.1f} h"
            self._changed.set()
        self._stop.wait(min(wait, self.MAX_SLEEP))
        return False

    def _provider_loop(self, source: Source) -> None:
        while not self._stop.is_set():
            if not self._sleep_until_active(source):
                continue
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

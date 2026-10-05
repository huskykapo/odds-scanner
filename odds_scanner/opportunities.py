"""Opportunity registry: validation, confidence and notify-once rules for arbitrages.

A raw finder result is only a *candidate*. Here it becomes an **opportunity** with a history:

    candidate -> validate (identity, market, freshness, completeness, stake limits, recalculation)
              -> confirm (a fresh poll of every involved bookmaker still shows it)
              -> VERIFIED -> notify

One opportunity is one record however often it is seen: ``+2.84 % -> +2.90 % -> +2.75 %`` is a single
opportunity. It is announced again only if it disappeared and later came back, its ROI moved by at
least ``renotify_roi_delta`` points (and ``renotify_min_interval`` passed), or the bet combination
changed (that is a different fingerprint, i.e. a new opportunity).

Everything is deterministic arithmetic on the data already in memory: no network, no AI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable, Iterable, Mapping

from odds_scanner.arbitrage.calculator import EPSILON, calculate_stakes
from odds_scanner.arbitrage.finder import BookRule, FinderSettings
from odds_scanner.models import Arbitrage

log = logging.getLogger(__name__)

PENDING = "PENDING"  # candidate: waiting for a fresh poll to confirm it
VERIFIED = "VERIFIED"
FAILED = "FAILED"  # at least one validation check failed
GONE = "GONE"  # not seen for ``gone_after``

# Bookmakers whose feeds carry the Betradar match id (so an exact id join is possible for them).
ID_BOOKMAKERS = frozenset({"monacobet", "doxxbet", "tipos", "synot"})


@dataclass(frozen=True)
class ValidationSettings:
    max_odds_age: timedelta = timedelta(seconds=180)  # every leg's price must be at most this old
    confirm_polls: int = 1  # fresh polls of EACH involved bookmaker needed to confirm; 0 = off
    renotify_roi_delta: float = 1.0  # percentage points of ROI change that justify a new alert
    renotify_min_interval: timedelta = timedelta(seconds=300)  # ...but not more often than this
    gone_after: timedelta = timedelta(seconds=30)  # unseen this long -> the opportunity is gone
    keep_gone_for: timedelta = timedelta(hours=24)
    verify_above_percent: float | None = 10.0  # ROI above this is suspicious (confidence penalty)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True  # informational checks never fail an opportunity


@dataclass(frozen=True)
class Validation:
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks if c.blocking)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.blocking and not c.ok)


@dataclass(frozen=True)
class Confidence:
    score: int
    label: str
    reasons: tuple[str, ...]


@dataclass
class NotifyState:
    roi: float
    at: datetime
    episode: int


@dataclass
class Opportunity:
    fingerprint: str
    arb: Arbitrage  # the latest version, enriched with status/confidence
    first_seen: datetime
    last_seen: datetime
    episode: int = 1  # increments every time it comes back after being gone
    episode_started: datetime | None = None
    status: str = PENDING
    validation: Validation = field(default_factory=lambda: Validation(()))
    confidence: Confidence = Confidence(0, "LOW", ())
    verified_at: datetime | None = None
    roi: float = 0.0
    first_roi: float = 0.0
    peak_roi: float = 0.0
    cycles_seen: int = 1
    base_generations: dict[str, int] = field(default_factory=dict)
    notified: dict[str, NotifyState] = field(default_factory=dict)
    last_recheck: datetime | None = None

    @property
    def bookmakers(self) -> tuple[str, ...]:
        return tuple(leg.bookmaker_key for leg in self.arb.legs)


# ---------------------------------------------------------------------- validation
def validate(
    arb: Arbitrage,
    now: datetime,
    settings: ValidationSettings,
    finder: FinderSettings,
    rule: Callable[[str], BookRule] | None = None,
) -> Validation:
    """Run the deterministic checks on one candidate. Confirmation is the registry's job."""
    rule = rule or finder.rule
    checks: list[Check] = []

    # 1. event identity
    books = {leg.bookmaker_key for leg in arb.legs}
    ok = bool(arb.event_id) and len(books) >= 2 and arb.commence_time > now
    checks.append(Check("event identity", ok, f"{len(books)} different bookmakers, kick-off {'in the future' if arb.commence_time > now else 'ALREADY PASSED'}"))

    # 2. market identity: one market and line, distinct outcomes
    outcomes = [leg.outcome for leg in arb.legs]
    ok = bool(arb.market) and len(set(outcomes)) == len(outcomes)
    checks.append(Check("market identity", ok, f"{arb.market}{'' if arb.line is None else ' ' + str(arb.line)}: outcomes {', '.join(outcomes)}"))

    # 3. freshness of every leg
    ages = []
    for leg in arb.legs:
        ages.append(None if leg.odds_updated is None else max(0.0, (now - leg.odds_updated).total_seconds()))
    if any(a is None for a in ages):
        checks.append(Check("odds freshness", False, "a price has no timestamp, so its age is unknown"))
    else:
        oldest = max(ages)  # type: ignore[type-var]
        checks.append(Check("odds freshness", oldest <= settings.max_odds_age.total_seconds(),
                            f"oldest price {oldest:.0f}s old (limit {settings.max_odds_age.total_seconds():.0f}s)"))

    # 4. complete: all outcomes of the market are covered and the odds really add up to an arbitrage
    odds = [leg.effective_odds or leg.odds for leg in arb.legs]
    try:
        inv = sum(1.0 / o for o in odds)
    except ZeroDivisionError:
        inv = float("inf")
    ok = len(arb.legs) in (2, 3) and all(o > 1.0 for o in odds) and inv < 1.0 - EPSILON
    checks.append(Check("complete arbitrage", ok, f"{len(arb.legs)} outcomes, sum of inverse odds {inv:.4f}"))

    # 5. stake limits (minimum known from config; maximum is not published by these sources)
    below = [leg.bookmaker_title for leg in arb.legs if leg.stake + 1e-9 < rule(leg.bookmaker_key).min_stake]
    checks.append(Check("minimum stakes", not below, "all stakes meet the bookmakers' minimum" if not below else "below minimum at " + ", ".join(below)))
    checks.append(Check("maximum stakes", True, "not available from these sources: check each bookmaker's limit", blocking=False))

    # 6. recalculate from the leg odds and compare with what the finder reported
    try:
        rules = [rule(leg.bookmaker_key) for leg in arb.legs]
        plan = calculate_stakes(odds, arb.bankroll, [r.stake_step or finder.stake_rounding for r in rules],
                                min_stakes=[r.min_stake for r in rules])
        same = (abs(plan.guaranteed_profit - arb.guaranteed_profit) < 0.01
                and all(abs(a - leg.stake) < 0.01 for a, leg in zip(plan.stakes, arb.legs)))
        checks.append(Check("recalculation", same, f"recomputed profit {plan.guaranteed_profit:.2f} vs reported {arb.guaranteed_profit:.2f}"))
    except (ValueError, ZeroDivisionError) as exc:
        checks.append(Check("recalculation", False, f"cannot recompute: {exc}"))
    return Validation(tuple(checks))


def score_confidence(arb: Arbitrage, validation: Validation, confirmed: bool, settings: ValidationSettings) -> Confidence:
    """Heuristic 0-100 score with the reasons behind it (deterministic, documented in the README)."""
    score, reasons = 100, []
    books = {leg.bookmaker_key for leg in arb.legs}
    if arb.event_id.startswith("br:") and books <= ID_BOOKMAKERS:
        reasons.append("event joined on the Betradar id (exact)")
    else:
        score -= 20
        reasons.append("event joined by team names + kick-off time (fuzzy): -20")
    age = arb.odds_age_seconds
    if age is None:
        score -= 30
        reasons.append("price age unknown: -30")
    elif age <= 30:
        reasons.append(f"fresh prices ({age:.0f}s)")
    elif age <= 60:
        score -= 5
        reasons.append(f"prices {age:.0f}s old: -5")
    elif age <= 120:
        score -= 15
        reasons.append(f"prices {age:.0f}s old: -15")
    else:
        score -= 30
        reasons.append(f"prices {age:.0f}s old: -30")
    if not confirmed:
        score -= 25
        reasons.append("not yet confirmed by a fresh poll: -25")
    roi = arb.realized_profit_percent
    if settings.verify_above_percent is not None and roi > settings.verify_above_percent:
        score -= 40
        reasons.append(f"ROI {roi:.1f}% is suspiciously high: -40")
    elif roi > 5.0:
        score -= 15
        reasons.append(f"ROI {roi:.1f}% is unusually high: -15")
    if arb.push_possible:
        score -= 15
        reasons.append("whole-number line can be refunded (push): -15")
    if not validation.passed:
        score = min(score, 20)
        reasons.append("validation failed: " + ", ".join(c.name for c in validation.failures))
    score = max(0, min(100, score))
    return Confidence(score, "HIGH" if score >= 80 else "MEDIUM" if score >= 55 else "LOW", tuple(reasons))


# ---------------------------------------------------------------------- registry
class OpportunityRegistry:
    def __init__(self, settings: ValidationSettings, finder: FinderSettings) -> None:
        self.settings = settings
        self._finder = finder
        self._opps: dict[str, Opportunity] = {}
        self._now: datetime | None = None
        self._latest: set[str] = set()  # fingerprints seen in the most recent update()

    # -- updating
    def update(self, arbs: Iterable[Arbitrage], now: datetime, generations: Mapping[str, int] | None = None) -> None:
        """Fold one analysis cycle's candidates in. ``generations``: bookmaker -> successful polls so far."""
        generations = generations or {}
        self._now = now
        seen: set[str] = set()
        self._latest = seen
        for arb in arbs:
            fp = arb.fingerprint
            seen.add(fp)
            gens = {leg.bookmaker_key: generations.get(leg.bookmaker_key, 0) for leg in arb.legs}
            opp = self._opps.get(fp)
            if opp is None:
                opp = Opportunity(fp, arb, first_seen=now, last_seen=now, episode_started=now, roi=arb.realized_profit_percent,
                                  first_roi=arb.realized_profit_percent, peak_roi=arb.realized_profit_percent, base_generations=gens)
                self._opps[fp] = opp
            else:
                if opp.status == GONE:  # it came back: a new episode, to be confirmed and announced again
                    opp.episode += 1
                    opp.episode_started = now
                    opp.cycles_seen = 0
                    opp.base_generations = gens
                    opp.first_roi = arb.realized_profit_percent
                opp.cycles_seen += 1
            opp.last_seen = now
            opp.roi = arb.realized_profit_percent
            opp.peak_roi = max(opp.peak_roi, opp.roi)

            confirmed = self._confirmed(opp, gens)
            opp.validation = validate(arb, now, self.settings, self._finder)
            if not opp.validation.passed:
                opp.status = FAILED
            elif not confirmed:
                opp.status = PENDING
            else:
                opp.status = VERIFIED
                opp.verified_at = now
            opp.confidence = score_confidence(arb, opp.validation, confirmed, self.settings)
            opp.arb = replace(arb, confidence=opp.confidence.score, confidence_label=opp.confidence.label, status=opp.status)

        for fp, opp in list(self._opps.items()):
            if fp in seen:
                continue
            if opp.status != GONE and now - opp.last_seen >= self.settings.gone_after:
                opp.status = GONE
            elif opp.status == GONE and now - opp.last_seen >= self.settings.keep_gone_for:
                del self._opps[fp]

    def _confirmed(self, opp: Opportunity, gens: Mapping[str, int]) -> bool:
        need = self.settings.confirm_polls
        if need <= 0:
            return True
        return all(gens.get(b, 0) - opp.base_generations.get(b, 0) >= need for b in gens)

    # -- reading
    def get(self, fingerprint: str) -> Opportunity | None:
        return self._opps.get(fingerprint)

    def current(self) -> list[Opportunity]:
        """Opportunities seen in the latest cycle, best ROI first.

        One that vanished is no longer current at once; ``gone_after`` only decides whether its
        return counts as the same episode (silent) or a new one (announced again).
        """
        live = [o for o in self._opps.values() if o.fingerprint in self._latest and o.status != GONE]
        return sorted(live, key=lambda o: (-o.roi, o.fingerprint))

    def seen_now(self, fingerprint: str) -> bool:
        """Was this opportunity part of the most recent update()?"""
        return fingerprint in self._latest

    def history(self) -> list[Opportunity]:
        return sorted(self._opps.values(), key=lambda o: o.first_seen, reverse=True)

    # -- notifying
    def due(self, key: str, *, renotify: bool = True) -> list[Opportunity]:
        """Verified opportunities that notifier ``key`` has not yet been told about (this episode),
        plus - if ``renotify`` - ones whose ROI moved by >= the configured delta since it was told."""
        now = self._now
        out = []
        for opp in self.current():
            if opp.status != VERIFIED:
                continue
            state = opp.notified.get(key)
            if state is None or state.episode != opp.episode:
                out.append(opp)
            elif (renotify and now is not None and abs(opp.roi - state.roi) >= self.settings.renotify_roi_delta
                  and now - state.at >= self.settings.renotify_min_interval):
                out.append(opp)
        return out

    def ack(self, key: str, opps: Iterable[Opportunity]) -> None:
        """Record that notifier ``key`` delivered these (failed sends are simply not acked, so they retry)."""
        for opp in opps:
            if self._now is not None:
                opp.notified[key] = NotifyState(opp.roi, self._now, opp.episode)

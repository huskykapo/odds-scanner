"""Live engine with the opportunity registry: confirmation, notify-once, retries, RECHECK."""

import json
from datetime import timedelta

import pytest

from odds_scanner.engine import LiveEngine
from odds_scanner.errors import BlockedError, ProviderError
from odds_scanner.models import Arbitrage
from odds_scanner.notifiers import Notifier
from odds_scanner.opportunities import ValidationSettings
from odds_scanner.providers.sk import NikeProvider
from tests.conftest import NOW, load_sk
from tests.test_engine_e2e import SETTINGS, Fixed, Recorder, mono_nike_sources


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def make(**kw):
    clock = Clock()
    sources = list(mono_nike_sources())
    eng = LiveEngine(sources, SETTINGS, clock=clock, **kw)
    return eng, clock, {s.key: s for s in sources}


def poll_all(eng):
    for s in eng.sources:
        eng.poll(s)


def plain_nike_events():
    """Niké's untweaked prices: no arbitrage against MONACObet."""
    return NikeProvider.parse(load_sk("nike_football.json"), "football", NOW)


# ------------------------------------------------------------------ confirmation and notify-once
def test_arb_is_announced_only_after_a_fresh_poll_confirms_it():
    rec = Recorder()
    eng, clock, _ = make(notifiers=[rec])
    poll_all(eng)
    eng.analyze()
    assert rec.batches == [] and eng.arbs[0].status == "PENDING"  # first sight: not announced yet
    clock.advance(30)
    poll_all(eng)  # a fresh poll of both bookmakers still shows it
    eng.analyze()
    assert len(rec.batches) == 1 and rec.batches[0][0].status == "VERIFIED"
    for _ in range(3):
        clock.advance(30)
        poll_all(eng)
        eng.analyze()
    assert len(rec.batches) == 1  # still one opportunity: never announced again


def test_a_vanishing_candidate_is_never_announced():
    rec = Recorder()
    eng, clock, srcs = make(notifiers=[rec])
    poll_all(eng)
    eng.analyze()
    srcs["nike"].provider = Fixed(plain_nike_events())
    clock.advance(30)
    poll_all(eng)
    eng.analyze()
    assert rec.batches == [] and eng.arbs == []


def test_confirmation_can_be_switched_off():
    rec = Recorder()
    eng, _, _ = make(notifiers=[rec], validation=ValidationSettings(confirm_polls=0))
    poll_all(eng)
    eng.analyze()
    assert len(rec.batches) == 1


class Flaky(Notifier):
    def __init__(self, mode):
        self.mode, self.calls, self.delivered = mode, 0, []

    def notify(self, arbs):
        self.calls += 1
        if self.mode == "raise" and self.calls == 1:
            raise RuntimeError("boom")
        if self.mode == "partial-none" and self.calls == 1:
            return []  # could not deliver any
        self.delivered.append(list(arbs))
        return None


@pytest.mark.parametrize("mode", ["raise", "partial-none"])
def test_failed_or_undelivered_alerts_are_offered_again_next_cycle(mode):
    flaky, good = Flaky(mode), Recorder()
    eng, _, _ = make(notifiers=[flaky, good], validation=ValidationSettings(confirm_polls=0))
    poll_all(eng)
    eng.analyze()
    assert flaky.delivered == [] and len(good.batches) == 1  # one sink failing does not stop the other
    eng.analyze()
    assert len(flaky.delivered) == 1 and len(good.batches) == 1  # retried once; the good sink is not repeated
    eng.analyze()
    assert len(flaky.delivered) == 1


def test_logs_get_each_episode_once_without_roi_renotifies():
    log = Recorder()
    eng, _, _ = make(logs=[log], validation=ValidationSettings(confirm_polls=0))
    poll_all(eng)
    for _ in range(3):
        eng.analyze()
    assert len(log.batches) == 1


# ------------------------------------------------------------------ dashboard data
def test_snapshot_carries_status_confidence_checks_and_timestamps():
    eng, clock, _ = make(validation=ValidationSettings(confirm_polls=0))
    poll_all(eng)
    eng.analyze()
    snap = json.loads(json.dumps(eng.snapshot()))
    (arb,) = snap["arbs"]
    assert arb["status"] == "VERIFIED" and arb["confidence_label"] in ("HIGH", "MEDIUM", "LOW")
    assert arb["fingerprint"].startswith(arb["id"]) and arb["return"] == pytest.approx(arb["total_stake"] + arb["guaranteed_profit"], abs=0.01)
    assert arb["roi"] == arb["profit"] and arb["first_seen"] and arb["verified_at"] and arb["odds_age"] is not None
    assert {c["name"] for c in arb["checks"]} >= {"event identity", "odds freshness", "recalculation", "maximum stakes"}
    assert arb["legs"][0]["odds"] and arb["confidence_reasons"]


# ------------------------------------------------------------------ RECHECK
def verified(eng):
    poll_all(eng)
    eng.analyze()
    return eng.registry.current()[0].fingerprint


def test_recheck_still_available_refetches_only_the_involved_bookmakers():
    eng, clock, srcs = make(validation=ValidationSettings(confirm_polls=0))
    fp = verified(eng)
    calls = []
    for key, src in srcs.items():
        inner = src.provider
        src.provider = type("Spy", (), {"fetch_odds": lambda self, sport, _k=key, _i=inner, **kw: (calls.append((_k, sport)), _i.fetch_odds(sport, **kw))[1]})()
    clock.advance(10)
    result = eng.recheck(fp)
    assert result["status"] == "STILL_AVAILABLE" and sorted(result["refreshed"]) == ["MONACObet", "Niké"]
    assert sorted(calls) == [("monacobet", "football"), ("nike", "football")]  # exactly those two, one sport each
    assert result["opportunity"]["fingerprint"] == fp and result["errors"] == []
    assert "still available" in result["message"]


def test_recheck_no_longer_available_when_the_fresh_prices_lost_the_arb():
    eng, clock, srcs = make(validation=ValidationSettings(confirm_polls=0))
    fp = verified(eng)
    srcs["nike"].provider = Fixed(plain_nike_events())  # the bookmaker moved its odds
    clock.advance(10)
    result = eng.recheck(fp)
    assert result["status"] == "NO_LONGER_AVAILABLE" and "no longer available" in result["message"].lower()
    assert eng.arbs == []


def test_recheck_reports_changed_odds_and_stakes():
    eng, clock, srcs = make(validation=ValidationSettings(confirm_polls=0))
    fp = verified(eng)
    events = list(srcs["nike"].provider.script[0]) if hasattr(srcs["nike"].provider, "script") else None
    assert events is not None
    from dataclasses import replace

    def bump(ev):
        books = []
        for b in ev.bookmakers:
            mkts = tuple(replace(m, outcomes=tuple(replace(o, price=round(o.price + 0.2, 2)) if o.name in ("X", "2") else o for o in m.outcomes)) for m in b.markets)
            books.append(replace(b, markets=mkts))
        return replace(ev, bookmakers=tuple(books))

    srcs["nike"].provider = Fixed([bump(e) for e in events])
    clock.advance(10)
    result = eng.recheck(fp)
    assert result["status"] == "STILL_AVAILABLE" and result["changes"]
    assert all(c["new_odds"] > c["old_odds"] for c in result["changes"] if c["outcome"] in ("X", "2"))


@pytest.mark.parametrize("error, text", [(ProviderError("HTTP 500"), "HTTP 500"), (BlockedError("x"), "blocked")])
def test_recheck_never_claims_validity_when_a_refetch_fails(error, text):
    eng, clock, srcs = make(validation=ValidationSettings(confirm_polls=0))
    fp = verified(eng)
    srcs["nike"].provider = Fixed(error)
    clock.advance(10)
    result = eng.recheck(fp)
    assert result["status"] == "UNKNOWN" and text in result["message"] and result["errors"]
    assert "cannot be confirmed" in result["message"]


def test_recheck_cooldown_and_unknown_ids():
    eng, clock, _ = make(validation=ValidationSettings(confirm_polls=0))
    fp = verified(eng)
    clock.advance(10)
    assert eng.recheck(fp)["status"] == "STILL_AVAILABLE"
    assert eng.recheck(fp)["status"] == "COOLDOWN"  # same instant
    clock.advance(6)
    assert eng.recheck(fp)["status"] == "STILL_AVAILABLE"
    unknown = eng.recheck("nonsense/h2h/None/1@x|2@y")
    assert unknown["status"] == "UNKNOWN" and unknown["opportunity"] is None


def test_recheck_result_is_json_serialisable():
    eng, clock, _ = make(validation=ValidationSettings(confirm_polls=0))
    fp = verified(eng)
    clock.advance(10)
    json.dumps(eng.recheck(fp))

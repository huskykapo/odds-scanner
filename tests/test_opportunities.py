"""Opportunity registry: one record per opportunity, validation, confidence, notify-once rules."""

from dataclasses import replace
from datetime import timedelta

import pytest

from odds_scanner.arbitrage import BookRule, FinderSettings, find_arbitrages
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome
from odds_scanner.opportunities import (
    FAILED, GONE, PENDING, VERIFIED, OpportunityRegistry, ValidationSettings, score_confidence, validate,
)
from tests.conftest import NOW

FINDER = FinderSettings(bankroll=500, min_profit_percent=0.5, stake_rounding=0.01, stale_after=timedelta(minutes=30))
SETTINGS = ValidationSettings(confirm_polls=0, renotify_roi_delta=1.0, renotify_min_interval=timedelta(seconds=300),
                              gone_after=timedelta(seconds=30), max_odds_age=timedelta(seconds=180))


def book(key, odds, *, age=10, now=NOW):
    t = now - timedelta(seconds=age)
    outs = tuple(Outcome(n, o) for n, o in odds.items())
    return BookmakerOdds(key, key.title(), (MarketOdds("h2h", outs, t),), t)


def event(books, *, eid="br:111", start=NOW + timedelta(hours=5)):
    return Event(eid, "football", "Football", start, "Slovan", "DAC", tuple(books))


def arbs(a=2.05, b=2.04, *, ba="nike", bb="tipos", now=NOW, age=10, eid="br:111", start=NOW + timedelta(hours=5)):
    ev = event([book(ba, {"Slovan": a, "DAC": 1.5}, age=age, now=now), book(bb, {"Slovan": 1.5, "DAC": b}, age=age, now=now)], eid=eid, start=start)
    return find_arbitrages([ev], FINDER, now=now)


def registry(**kw):
    return OpportunityRegistry(replace(SETTINGS, **kw), FINDER)


def step(reg, seconds, *args, gens=None, **kw):
    now = NOW + timedelta(seconds=seconds)
    reg.update(arbs(*args, now=now, **kw), now, gens)
    return now


# ------------------------------------------------------------------ identity
def test_fingerprint_ignores_odds_but_not_bookmakers_or_event():
    (a1,), (a2,) = arbs(2.17, 2.08), arbs(2.30, 2.20)
    assert a1.fingerprint == a2.fingerprint
    (other_book,) = arbs(ba="doxxbet")
    assert other_book.fingerprint != a1.fingerprint  # different bookmaker = different bet instructions
    (other_event,) = arbs(eid="br:222")
    assert other_event.fingerprint != a1.fingerprint


def test_roi_wobbling_is_one_opportunity_notified_once():
    reg = registry()
    notified = 0
    for t, (a, b) in enumerate([(2.05, 2.04), (2.06, 2.04), (2.04, 2.04), (2.05, 2.05)]):
        now = step(reg, t * 5, a, b)
        due = reg.due("tg")
        notified += len(due)
        reg.ack("tg", due)
    assert notified == 1 and len(reg.current()) == 1
    opp = reg.current()[0]
    assert opp.cycles_seen == 4 and opp.first_seen == NOW and opp.status == VERIFIED
    assert opp.peak_roi >= opp.roi >= 0 and opp.verified_at == now


def test_big_roi_change_renotifies_but_not_before_the_minimum_interval():
    reg = registry()
    step(reg, 0)
    reg.ack("tg", reg.due("tg"))
    step(reg, 10, 2.40, 2.30)  # ROI jumped by well over 1 point, but only 10 s later
    assert reg.due("tg") == []
    step(reg, 400, 2.40, 2.30)
    assert len(reg.due("tg")) == 1
    reg.ack("tg", reg.due("tg"))
    step(reg, 800, 2.41, 2.30)  # tiny change from the last announced ROI
    assert reg.due("tg") == []


def test_gone_and_back_is_announced_again():
    reg = registry()
    step(reg, 0)
    reg.ack("tg", reg.due("tg"))
    now = NOW + timedelta(seconds=10)
    reg.update([], now)  # not seen: no longer current, but still the same episode (30 s grace)
    assert reg.current() == [] and reg.history()[0].status == VERIFIED
    now = NOW + timedelta(seconds=45)
    reg.update([], now)
    assert reg.current() == [] and reg.history()[0].status == GONE
    step(reg, 60)
    (opp,) = reg.current()
    assert opp.episode == 2 and opp.first_seen == NOW and opp.status == VERIFIED
    assert len(reg.due("tg")) == 1  # announced again


def test_changed_combination_is_a_new_opportunity():
    reg = registry()
    step(reg, 0)
    reg.ack("tg", reg.due("tg"))
    step(reg, 5, ba="doxxbet")  # same event, other bookmaker for the first outcome
    assert len(reg.history()) == 2 and len(reg.due("tg")) == 1


def test_notifiers_are_independent_and_unacked_sends_retry():
    reg = registry()
    step(reg, 0)
    reg.ack("console", reg.due("console"))
    assert reg.due("console") == [] and len(reg.due("telegram")) == 1  # telegram was never told
    step(reg, 5)
    assert len(reg.due("telegram")) == 1  # failed/unacked send comes back next cycle
    reg.ack("telegram", reg.due("telegram"))
    assert reg.due("telegram") == []


def test_logs_style_due_without_roi_renotify():
    reg = registry()
    step(reg, 0)
    reg.ack("log", reg.due("log", renotify=False))
    step(reg, 400, 2.60, 2.50)
    assert reg.due("log", renotify=False) == []


# ------------------------------------------------------------------ confirmation
def test_confirmation_needs_a_fresh_poll_of_every_involved_bookmaker():
    reg = registry(confirm_polls=1)
    step(reg, 0, gens={"nike": 1, "tipos": 1})
    assert reg.current()[0].status == PENDING and reg.due("tg") == []
    step(reg, 5, gens={"nike": 2, "tipos": 1})  # only one bookmaker re-polled
    assert reg.current()[0].status == PENDING
    step(reg, 10, gens={"nike": 2, "tipos": 2})
    assert reg.current()[0].status == VERIFIED and len(reg.due("tg")) == 1


def test_confirm_polls_zero_verifies_immediately():
    reg = registry(confirm_polls=0)
    step(reg, 0)
    assert reg.current()[0].status == VERIFIED


def test_unconfirmed_candidates_that_vanish_are_never_announced():
    reg = registry(confirm_polls=1)
    step(reg, 0, gens={"nike": 1, "tipos": 1})
    reg.update([], NOW + timedelta(seconds=60), {"nike": 2, "tipos": 2})
    assert reg.due("tg") == []


# ------------------------------------------------------------------ validation
def test_stale_price_fails_validation_and_blocks_notification():
    reg = registry()
    step(reg, 0, age=400)
    opp = reg.current()[0]
    assert opp.status == FAILED and reg.due("tg") == []
    assert [c.name for c in opp.validation.failures] == ["odds freshness"]


def test_started_event_fails_the_event_identity_check():
    (arb,) = arbs()
    v = validate(replace(arb, commence_time=NOW - timedelta(minutes=1)), NOW, SETTINGS, FINDER)
    assert "event identity" in [c.name for c in v.failures]


def test_missing_timestamp_fails_freshness():
    (arb,) = arbs()
    legs = (replace(arb.legs[0], odds_updated=None), arb.legs[1])
    v = validate(replace(arb, legs=legs), NOW, SETTINGS, FINDER)
    assert "odds freshness" in [c.name for c in v.failures]


def test_tampered_stakes_fail_recalculation():
    (arb,) = arbs()
    legs = (replace(arb.legs[0], stake=arb.legs[0].stake + 5), arb.legs[1])
    assert "recalculation" in [c.name for c in validate(replace(arb, legs=legs), NOW, SETTINGS, FINDER).failures]


def test_minimum_stake_check_and_informational_max_stake():
    (arb,) = arbs()
    strict = replace(FINDER, book_rules={"nike": BookRule(min_stake=10_000)})
    v = validate(arb, NOW, SETTINGS, strict)
    assert "minimum stakes" in [c.name for c in v.failures]
    ok = validate(arb, NOW, SETTINGS, FINDER)
    assert ok.passed and not next(c for c in ok.checks if c.name == "maximum stakes").blocking


def test_not_an_arbitrage_fails_the_complete_check():
    (arb,) = arbs()
    legs = tuple(replace(leg, odds=1.5, effective_odds=None) for leg in arb.legs)
    assert "complete arbitrage" in [c.name for c in validate(replace(arb, legs=legs), NOW, SETTINGS, FINDER).failures]


# ------------------------------------------------------------------ confidence
def test_confidence_exact_id_fresh_confirmed_is_high():
    (arb,) = arbs(ba="doxxbet", bb="tipos")
    v = validate(arb, NOW, SETTINGS, FINDER)
    c = score_confidence(arb, v, True, SETTINGS)
    assert c.label == "HIGH" and c.score == 100 and "Betradar" in c.reasons[0]


def test_confidence_penalties_are_explained():
    (arb,) = arbs(ba="nike", bb="tipos")  # Niké has no Betradar id -> fuzzy join
    v = validate(arb, NOW, SETTINGS, FINDER)
    c = score_confidence(arb, v, False, SETTINGS)
    assert c.score == 100 - 20 - 25 and any("fuzzy" in r for r in c.reasons) and any("not yet confirmed" in r for r in c.reasons)
    assert c.label == "MEDIUM"
    named = score_confidence(replace(arb, event_id="football:slovan|dac|x"), v, True, SETTINGS)
    assert named.score == 80
    high_roi = score_confidence(replace(arb, realized_profit_percent=12.0), v, True, SETTINGS)
    assert any("suspiciously high" in r for r in high_roi.reasons) and high_roi.score <= 40
    stale = score_confidence(replace(arb, found_at=NOW + timedelta(seconds=500)), v, True, SETTINGS)
    assert any("500s old" in r or "old: -30" in r for r in stale.reasons)


def test_failed_validation_caps_confidence():
    (arb,) = arbs(age=400)
    v = validate(arb, NOW, SETTINGS, FINDER)
    c = score_confidence(arb, v, True, SETTINGS)
    assert c.score <= 20 and c.label == "LOW" and "validation failed" in c.reasons[-1]


def test_registry_enriches_the_arb_with_status_and_confidence():
    reg = registry()
    step(reg, 0)
    arb = reg.current()[0].arb
    assert arb.status == VERIFIED and arb.confidence is not None and arb.confidence_label in ("HIGH", "MEDIUM", "LOW")


def test_odds_age_seconds():
    (arb,) = arbs(age=42)
    assert arb.odds_age_seconds == pytest.approx(42)
    assert replace(arb, legs=(replace(arb.legs[0], odds_updated=None), arb.legs[1])).odds_age_seconds is None


def test_old_gone_records_are_pruned():
    reg = registry(keep_gone_for=timedelta(hours=1))
    step(reg, 0)
    reg.update([], NOW + timedelta(seconds=60))
    assert len(reg.history()) == 1
    reg.update([], NOW + timedelta(hours=2))
    assert reg.history() == []


def test_briefly_unseen_then_back_within_grace_is_the_same_episode_and_silent():
    reg = registry()
    step(reg, 0)
    reg.ack("tg", reg.due("tg"))
    reg.update([], NOW + timedelta(seconds=10))  # one cycle without it
    step(reg, 20)  # back within 30 s
    (opp,) = reg.current()
    assert opp.episode == 1 and reg.due("tg") == []

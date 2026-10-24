"""Engine with a multi-bookmaker source (OddsPapi style) and per-channel bookmaker sets (Discord audiences)."""

from datetime import timedelta

from odds_scanner.adaptive import AdaptiveSettings
from odds_scanner.engine import LiveEngine, Source
from odds_scanner.markets import H2H_3_WAY
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome
from odds_scanner.opportunities import ValidationSettings
from tests.conftest import NOW
from tests.test_engine_e2e import SETTINGS, Fixed, Recorder
from tests.test_engine_validation import Clock

START = NOW + timedelta(hours=2)  # a match starts soon: adaptive polling would speed up


def event(eid, books):
    """books: {key: (home, draw, away)}"""
    bms = tuple(
        BookmakerOdds(k, k.title(), (MarketOdds(H2H_3_WAY, (Outcome("1", h), Outcome("X", d), Outcome("2", a)), NOW),), NOW)
        for k, (h, d, a) in books.items()
    )
    return Event(eid, "football", "Football", START, "Arsenal", "Chelsea", bms)


# Overall best: 1 Stake 2.30, X Tipos 3.60, 2 Roobet 4.50 (+6.97 %).
# Slovak books alone: 1 Niké 2.10, X Tipos 3.60, 2 Niké 4.30 (+1.36 %); crypto alone: Stake/Roobet (+2.08 %).
def sources():
    return [
        Source("nike", "Niké", Fixed([event("n1", {"nike": (2.10, 3.30, 4.30)})]), ["football"], 60),
        Source("tipos", "Tipos", Fixed([event("t1", {"tipos": (2.00, 3.60, 4.00)})]), ["football"], 60),
        Source("oddspapi", "OddsPapi", Fixed([event("p1", {"stake": (2.30, 3.00, 3.50), "roobet": (2.00, 3.10, 4.50)})]),
               ["football"], 1200, metered=True, homepages={"stake": "https://stake.com", "roobet": "https://roobet.com"}),
    ]


def books_of(arbs):
    return sorted(tuple(sorted({l.bookmaker_key for l in a.legs})) for a in arbs)


def make(**kw):
    clock = Clock()
    eng = LiveEngine(sources(), SETTINGS, clock=clock, **kw)
    return eng, clock


def poll_all(eng):
    for s in eng.sources:
        eng.poll(s)


def test_without_audiences_only_the_overall_best_combination_is_found():
    eng, _ = make()
    poll_all(eng)
    assert books_of(eng.analyze()) == [("roobet", "stake", "tipos")]


def test_each_audience_gets_its_own_best_combination():
    eng, _ = make(audiences=[{"nike", "tipos"}, {"stake", "roobet"}])
    poll_all(eng)
    arbs = eng.analyze()
    assert books_of(arbs) == [("nike", "tipos"), ("roobet", "stake"), ("roobet", "stake", "tipos")]
    sk = next(a for a in arbs if {l.bookmaker_key for l in a.legs} == {"nike", "tipos"})
    assert [(l.outcome, l.bookmaker_key, l.odds) for l in sk.legs] == [("1", "nike", 2.10), ("X", "tipos", 3.60), ("2", "nike", 4.30)]


def test_bookmakers_of_a_multi_bookmaker_source_get_confirmed_by_its_polls():
    rec = Recorder()
    eng, clock = make(notifiers=[rec], validation=ValidationSettings(confirm_polls=1))
    poll_all(eng)
    eng.analyze()
    assert rec.batches == []  # first sight: pending
    clock.advance(30)
    poll_all(eng)
    eng.analyze()
    assert len(rec.batches) == 1 and rec.batches[0][0].status == "VERIFIED"  # Stake/Roobet count as re-polled


def test_metered_source_is_never_polled_faster_than_configured():
    eng, _ = make(adaptive=AdaptiveSettings(enabled=True))
    nike, _, papi = eng.sources
    assert eng.poll(nike) == 30  # free site: faster before kick-off
    assert eng.poll(papi) == 1200  # metered API: never faster


def test_dashboard_card_links_each_bookmaker_of_a_multi_bookmaker_source():
    eng, _ = make(validation=ValidationSettings(confirm_polls=0))
    poll_all(eng)
    eng.analyze()
    card = eng.snapshot()["arbs"][0]
    pages = {leg["bookmaker_key"]: leg["homepage"] for leg in card["legs"]}
    assert pages["stake"] == "https://stake.com" and pages["roobet"] == "https://roobet.com"


def test_recheck_refetches_the_source_that_carries_the_bookmaker():
    eng, clock = make(validation=ValidationSettings(confirm_polls=0))
    poll_all(eng)
    (arb,) = eng.analyze()
    papi = eng.sources[2].provider
    before = papi.calls
    clock.advance(10)
    result = eng.recheck(arb.fingerprint)
    assert result["status"] == "STILL_AVAILABLE"
    assert papi.calls == before + 1  # re-fetched once, although two of the legs are on it
    assert "OddsPapi" not in " ".join(result["errors"])

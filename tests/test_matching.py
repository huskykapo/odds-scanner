from datetime import timedelta

import pytest

from odds_scanner.arbitrage import FinderSettings, find_arbitrages
from odds_scanner.matching import MatchSettings, match_events, name_similarity, normalize_team
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome
from odds_scanner.providers.sk import MonacobetProvider, NikeProvider
from tests.conftest import NOW, load_sk

T0 = NOW + timedelta(days=1)


def ev(book, home, away, start=T0, betradar=None, sport="football", prices=(2.0, 3.4, 3.6)):
    m = MarketOdds("h2h_3_way", tuple(Outcome(c, p) for c, p in zip("1X2", prices)), NOW)
    return Event(f"{book}:{home}", sport, sport, start, home, away, (BookmakerOdds(book, book.title(), (m,), NOW),), betradar)


def test_normalize_team_strips_accents_and_club_prefixes():
    assert normalize_team("ŠK Slovan Bratislava") == "slovan bratislava"
    assert normalize_team("AS Trenčín") == "trencin"
    assert normalize_team("KFC Komárno") == "komarno"
    assert normalize_team("MFK Ružomberok") == "ruzomberok"
    assert normalize_team("HC Košice") == "kosice"
    assert normalize_team("FC") == "fc"  # never normalise a name away entirely


def test_betradar_id_match_even_when_names_differ():
    a = ev("doxxbet", "Rakúsko", "Nemecko", betradar="68932732")
    b = ev("synot", "Austria", "Germany", start=T0 + timedelta(minutes=5), betradar="68932732")
    merged, stats = match_events([a, b])
    assert len(merged) == 1 and merged[0].id == "br:68932732"
    assert {bm.key for bm in merged[0].bookmakers} == {"doxxbet", "synot"}
    assert stats.by_betradar == 1 and stats.unmatched == 0


def test_different_betradar_ids_never_merge():
    a = ev("doxxbet", "Slovan Bratislava", "Žilina", betradar="1")
    b = ev("synot", "Slovan Bratislava", "Žilina", betradar="2")
    assert len(match_events([a, b])[0]) == 2


@pytest.mark.parametrize("x, y", [
    ("ŠK Slovan Bratislava", "Slovan Bratislava"),
    ("AS Trenčín", "Trenčín"),
    ("FC Košice", "Košice"),
    ("Spartak Trnava", "FC Spartak Trnava"),
    ("MFK Ružomberok", "Ruzomberok"),
])
def test_fuzzy_match_slovak_spellings(x, y):
    a = ev("nike", x, "MŠK Žilina")
    b = ev("monacobet", y, "Žilina", start=T0 + timedelta(minutes=10))
    merged, stats = match_events([a, b])
    assert len(merged) == 1 and stats.by_name == 1
    assert len(merged[0].bookmakers) == 2


def test_near_miss_does_not_match():
    # Same day, same city, different clubs.
    a = ev("nike", "Slovan Bratislava", "Žilina")
    b = ev("monacobet", "Inter Bratislava", "Žilina")
    c = ev("doxxbet", "Slovan Liberec", "Žilina")
    merged, stats = match_events([a, b, c])
    assert len(merged) == 3 and stats.unmatched == 3
    assert name_similarity(normalize_team("Slovan Bratislava"), normalize_team("Slovan Liberec")) < 0.8


def test_reserve_and_women_teams_never_match_first_team():
    assert name_similarity("slovan bratislava", "slovan bratislava b") == 0.0
    assert name_similarity("slovan bratislava", "slovan bratislava z") == 0.0


def test_time_tolerance_and_sport():
    a = ev("nike", "Slovan Bratislava", "Žilina")
    assert len(match_events([a, ev("monacobet", "Slovan Bratislava", "Žilina", start=T0 + timedelta(minutes=20))])[0]) == 2
    assert len(match_events([a, ev("monacobet", "Slovan Bratislava", "Žilina", sport="hockey")])[0]) == 2


def test_swapped_home_away_is_rejected():
    a = ev("nike", "Slovan Bratislava", "Žilina")
    b = ev("monacobet", "Žilina", "Slovan Bratislava")
    merged, stats = match_events([a, b])
    assert len(merged) == 2 and stats.rejected_swapped == 1


def test_manual_alias():
    a = ev("nike", "Rakúsko", "Nemecko")
    b = ev("the_odds_api", "Austria", "Germany")
    assert len(match_events([a, b])[0]) == 2
    s = MatchSettings.from_raw_aliases({"Austria": "Rakúsko", "Germany": "Nemecko"})
    assert len(match_events([a, b], s)[0]) == 1


def test_same_bookmaker_never_merged_twice():
    assert len(match_events([ev("nike", "A Team", "B Team"), ev("nike", "A Team", "B Team")])[0]) == 2


def test_odds_api_team_named_outcomes_become_codes():
    m = MarketOdds("h2h_3_way", (Outcome("Austria", 2.0), Outcome("Draw", 3.0), Outcome("Germany", 3.5)), NOW)
    api = Event("x", "soccer_uefa_nations_league", "UNL", T0, "Austria", "Germany", (BookmakerOdds("pinnacle", "Pinnacle", (m,), NOW),))
    s = MatchSettings.from_raw_aliases({"Austria": "Rakúsko", "Germany": "Nemecko"})
    merged, _ = match_events([ev("nike", "Rakúsko", "Nemecko"), api], s)
    (one,) = merged
    pin = next(b for b in one.bookmakers if b.key == "pinnacle")
    assert [o.name for o in pin.markets[0].outcomes] == ["1", "X", "2"]
    assert pin.event_name == "Austria vs Germany"


def test_samples_monacobet_and_nike_match_kosice_komarno():
    events = MonacobetProvider.parse(load_sk("monacobet_football_league.json"), "football", NOW) + \
        NikeProvider.parse(load_sk("nike_football.json"), "football", NOW)
    merged, stats = match_events(events)
    kosice = next(e for e in merged if "Košice" in e.home_team)
    assert {b.key for b in kosice.bookmakers} == {"monacobet", "nike"}
    assert kosice.id == "br:72042284"  # Monaco's Betradar id carried to the merged event
    assert stats.multi_bookmaker_events == 1 and stats.unmatched == 2  # Trenčín (Monaco), Podbrezová (Niké)
    assert stats.per_bookmaker["nike"] == {"events": 2, "matched": 1}

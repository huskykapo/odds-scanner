"""Recognise the same match across bookmakers and merge their odds into one event.

1. Exact match on the Betradar match id when both sides have one.
2. Otherwise: same sport, kick-off within ``time_tolerance`` and a fuzzy match of BOTH team names
   (accents stripped, lower-case, club prefixes like FC/FK/MFK/ŠK/HC/AS/KFC dropped; token overlap
   + difflib >= ``threshold``). A pairing that only works with home and away swapped is rejected:
   the "1" and "2" outcomes would be the wrong way round.
3. Manual aliases (``matching.aliases`` in config.yaml) map a bookmaker's spelling to another.

Merged events use the outcome codes ``1`` / ``X`` / ``2`` for result markets, so a bookmaker's
own spelling of the team names (kept per bookmaker in ``BookmakerOdds.event_name``) no longer
matters to the finder.
"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable, Mapping

from odds_scanner.markets import AWAY, DRAW, H2H, H2H_3_WAY, HOME, split_period, sport_family
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome

log = logging.getLogger(__name__)

# Club-type prefixes/suffixes that bookmakers add or leave out ("ŠK Slovan" vs "Slovan").
STOPWORDS = frozenset({
    "fc", "fk", "mfk", "sk", "msk", "hc", "hk", "as", "kfc", "afc", "ac", "tj", "ofk", "fbc", "bk", "sc",
    "cf", "cd", "ud", "sd", "fsv", "tsv", "vfb", "vfl", "sv", "ss", "ssc", "ks", "nk", "sfc", "bc", "club",
    "klub", "1", "spolok",
})
# Tokens that make a different team: reserve, youth and women's sides. Never fuzzy-match across them.
MARKERS = frozenset({"b", "ii", "iii", "u17", "u18", "u19", "u20", "u21", "u23", "w", "z", "zeny", "women", "res", "reserves", "jun", "juniors", "dorast"})

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_team(name: str, aliases: Mapping[str, str] | None = None) -> str:
    """``ŠK Slovan Bratislava`` -> ``slovan bratislava``; aliases are applied after normalising."""
    tokens = _NON_ALNUM.sub(" ", strip_accents(name).lower()).split()
    kept = [t for t in tokens if t not in STOPWORDS] or tokens
    norm = " ".join(kept)
    if aliases:
        norm = aliases.get(norm, norm)
    return norm


def name_similarity(a: str, b: str) -> float:
    """Similarity of two *normalised* team names in [0, 1]."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    if (ta & MARKERS) != (tb & MARKERS):
        return 0.0  # "Slovan" vs "Slovan B" / "Slovan (Ž)": different teams
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    overlap = len(ta & tb) / max(len(ta), len(tb))
    containment = 0.9 if small <= big and sum(len(t) for t in small) >= 4 else 0.0  # "Trnava" in "Spartak Trnava"
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return max(overlap, containment, ratio)


@dataclass(frozen=True)
class MatchSettings:
    time_tolerance: timedelta = timedelta(minutes=15)
    threshold: float = 0.8
    aliases: Mapping[str, str] = field(default_factory=dict)  # normalised alias -> normalised name

    @classmethod
    def from_raw_aliases(cls, aliases: Mapping[str, str], **kw) -> "MatchSettings":
        """Aliases as written by a user ("Slovan" -> "ŠK Slovan Bratislava"); both sides normalised."""
        norm = {normalize_team(k): normalize_team(v) for k, v in aliases.items()}
        return cls(aliases=norm, **kw)


@dataclass
class MatchStats:
    events_in: int = 0
    merged_events: int = 0
    multi_bookmaker_events: int = 0  # merged events quoted by >= 2 bookmakers
    by_betradar: int = 0  # joins made on the Betradar id
    by_name: int = 0  # joins made by fuzzy name + time
    rejected_swapped: int = 0  # looked like the same match with home/away swapped
    unmatched: int = 0  # events no other bookmaker has
    per_bookmaker: dict[str, dict[str, int]] = field(default_factory=dict)  # key -> {events, matched}

    def as_dict(self) -> dict:
        return {
            "events_in": self.events_in,
            "merged_events": self.merged_events,
            "multi_bookmaker_events": self.multi_bookmaker_events,
            "by_betradar": self.by_betradar,
            "by_name": self.by_name,
            "rejected_swapped": self.rejected_swapped,
            "unmatched": self.unmatched,
            "per_bookmaker": self.per_bookmaker,
        }


@dataclass
class _Cluster:
    sport: str
    first: Event
    home: str  # normalised
    away: str
    members: list[Event] = field(default_factory=list)
    books: set[str] = field(default_factory=set)
    betradar_id: str | None = None


def _bucket(event: Event, width: float) -> int:
    return int(event.commence_time.timestamp() // width)


def match_events(events: Iterable[Event], settings: MatchSettings | None = None) -> tuple[list[Event], MatchStats]:
    """Group the same match from different bookmakers; return merged events and quality counts."""
    settings = settings or MatchSettings()
    stats = MatchStats()
    clusters: list[_Cluster] = []
    by_betradar: dict[tuple[str, str], _Cluster] = {}
    by_time: dict[tuple[str, int], list[_Cluster]] = defaultdict(list)
    width = max(settings.time_tolerance.total_seconds(), 60.0)

    for ev in events:
        stats.events_in += 1
        sport = sport_family(ev.sport_key)
        books = {b.key for b in ev.bookmakers}
        home, away = normalize_team(ev.home_team, settings.aliases), normalize_team(ev.away_team, settings.aliases)
        target: _Cluster | None = None

        if ev.betradar_id:
            c = by_betradar.get((sport, ev.betradar_id))
            if c is not None and not (c.books & books):
                target = c
                stats.by_betradar += 1
        if target is None:
            best_score = 0.0
            b = _bucket(ev, width)
            for nb in (b - 1, b, b + 1):
                for c in by_time.get((sport, nb), ()):
                    if c.books & books:
                        continue
                    if ev.betradar_id and c.betradar_id and ev.betradar_id != c.betradar_id:
                        continue  # both have ids and they differ: definitely different matches
                    if abs(c.first.commence_time - ev.commence_time) > settings.time_tolerance:
                        continue
                    h, a = name_similarity(home, c.home), name_similarity(away, c.away)
                    if h >= settings.threshold and a >= settings.threshold:
                        if h + a > best_score:
                            best_score, target = h + a, c
                    elif name_similarity(home, c.away) >= settings.threshold and name_similarity(away, c.home) >= settings.threshold:
                        stats.rejected_swapped += 1
                        log.debug("not matching %s with %s: home/away swapped", ev.name, c.first.name)
            if target is not None:
                stats.by_name += 1

        if target is None:
            target = _Cluster(sport, ev, home, away)
            clusters.append(target)
            by_time[(sport, _bucket(ev, width))].append(target)
        target.members.append(ev)
        target.books |= books
        if ev.betradar_id and target.betradar_id is None:
            target.betradar_id = ev.betradar_id
            by_betradar.setdefault((sport, ev.betradar_id), target)

    merged = [_merge(c) for c in clusters]
    stats.merged_events = len(merged)
    for c, m in zip(clusters, merged):
        matched = len(c.members) > 1
        if len(m.bookmakers) >= 2:
            stats.multi_bookmaker_events += 1
        if not matched:
            stats.unmatched += 1
            log.debug("unmatched: %s (%s, %s, %s)", c.first.name, c.sport, c.first.commence_time, ",".join(sorted(c.books)))
        for ev in c.members:
            for key in {b.key for b in ev.bookmakers}:
                row = stats.per_bookmaker.setdefault(key, {"events": 0, "matched": 0})
                row["events"] += 1
                row["matched"] += int(matched)
    return merged, stats


def _merge(c: _Cluster) -> Event:
    first = c.first
    if c.betradar_id:
        event_id = f"br:{c.betradar_id}"
    else:
        event_id = f"{c.sport}:{c.home}|{c.away}|{first.commence_time:%Y%m%d%H%M}"
    bookmakers = tuple(_canonical_book(b, ev, first) for ev in c.members for b in ev.bookmakers)
    return Event(
        id=event_id,
        sport_key=c.sport,
        sport_title=c.sport.title(),
        commence_time=first.commence_time,
        home_team=first.home_team,
        away_team=first.away_team,
        bookmakers=bookmakers,
        betradar_id=c.betradar_id,
    )


def _canonical_book(book: BookmakerOdds, member: Event, canonical: Event) -> BookmakerOdds:
    """Rename team-named outcomes (The Odds API style) to codes / the canonical team names."""

    def rename(market: MarketOdds) -> MarketOdds:
        base, _ = split_period(market.key)
        if base in (H2H, H2H_3_WAY):
            names = {member.home_team: HOME, member.away_team: AWAY, "Draw": DRAW}
        elif base == "spreads":
            names = {member.home_team: canonical.home_team, member.away_team: canonical.away_team}
        else:
            return market
        return MarketOdds(market.key, tuple(Outcome(names.get(o.name, o.name), o.price, o.point) for o in market.outcomes), market.last_update)

    return BookmakerOdds(
        key=book.key,
        title=book.title,
        markets=tuple(rename(m) for m in book.markets),
        last_update=book.last_update,
        url=book.url,
        event_name=book.event_name or member.name,
        event_id=book.event_id or member.id,
    )

"""Find the site-specific sport ids that are missing from the config - automatically, once.

DOXXbet (``sport``), Tipos and Synot (``CategoryID``) identify sports by numbers that were only
known for football. This module tries candidate numbers one by one (through the provider's normal
polite client: at most one request per second per site) and recognises the sport:

* Tipos / Synot replies contain the category name ("Hokej", "Basketbal", "Tenis");
* DOXXbet sends Betradar's sport number with every match ("BetradarSportID": 1 = football);
* otherwise the Betradar match ids in the reply are compared with matches whose sport is already
  known (e.g. MONACObet's hockey list) - the sport with the clear majority wins.

Results are stored in ``data/discovered_sports.json`` and used on every later start; values set in
``config.yaml`` always take precedence. Sports not found are retried after a day.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from odds_scanner.errors import BlockedError, ProviderError
from odds_scanner.matching import strip_accents
from odds_scanner.providers.sk.common import betradar_digits
from odds_scanner.providers.sk.doxxbet import URL as DOXX_URL, DoxxbetProvider
from odds_scanner.providers.sk.protobuf import Message
from odds_scanner.providers.sk.tipos import LANGUAGE_SK, PATH as TIPOS_PATH, TiposProvider, iter_event_messages, parse_return_value

log = logging.getLogger(__name__)

CACHE_PATH = Path("data") / "discovered_sports.json"
DISCOVERABLE = ("doxxbet", "tipos", "synot")
MAX_CANDIDATE = 300
RETRY_AFTER = timedelta(days=1)
MAX_CONSECUTIVE_ERRORS = 25

# Exact (accent-free, lower-case) sport names as the sites write them. Exact on purpose: "stolný
# tenis", "pozemný hokej", "hokejbal" or "americký futbal" must not be taken for our sports.
SPORT_NAMES = {
    "futbal": "football",
    "hokej": "hockey",
    "ladovy hokej": "hockey",
    "lad. hokej": "hockey",
    "basketbal": "basketball",
    "tenis": "tennis",
}


# Betradar's own sport numbers, sent by DOXXbet with every match ("BetradarSportID").
BETRADAR_SPORTS = {"1": "football", "2": "basketball", "4": "hockey", "5": "tennis"}


def sport_from_name(name: str | None) -> str | None:
    if not name:
        return None
    if name in BETRADAR_SPORTS.values():
        return name
    return SPORT_NAMES.get(" ".join(strip_accents(name).lower().split()))


def classify(ids: Iterable[str], reference: Mapping[str, set[str]]) -> str | None:
    """The sport whose known Betradar ids clearly dominate the overlap with ``ids``."""
    ids = set(ids)
    scores = sorted(((len(ids & known), sport) for sport, known in reference.items()), reverse=True)
    if not scores or scores[0][0] == 0:
        return None
    best, sport = scores[0]
    second = scores[1][0] if len(scores) > 1 else 0
    if (second == 0) or (best >= 3 and best >= 3 * second):
        return sport
    return None


# ------------------------------------------------------------------ one candidate per site
def probe_doxxbet(provider: DoxxbetProvider, candidate: int) -> tuple[str | None, set[str]]:
    dates = provider.options.get("dates") or ["TM"]
    top = next(iter(provider.request_choice.values()), provider.top_candidates()[0])  # what polling settled on
    body = provider.body_for(candidate, dates[-1], top)
    payload = provider._client.post_json(DOXX_URL, body)  # noqa: SLF001 - same polite client as polling
    ects = payload.get("EventChanceTypes") or [] if isinstance(payload, dict) else []
    name = None
    ids = set()
    br_sports: Counter[str] = Counter()
    for ect in ects:
        if not isinstance(ect, dict):
            continue
        if ect.get("SportID") not in (None, candidate, str(candidate)):
            continue  # the site ignored our sport filter
        sport = BETRADAR_SPORTS.get(str(ect.get("BetradarSportID")))
        if sport:
            br_sports[sport] += 1
        for key, value in ect.items():
            if name is None and isinstance(value, str) and "sport" in key.lower() and "name" in key.lower():
                name = value
        br = betradar_digits(ect.get("BetradarStatisticsUrn"))
        if br:
            ids.add(br)
    if br_sports:  # every DOXXbet match says which Betradar sport it is: the surest answer
        return br_sports.most_common(1)[0][0], ids
    return name, ids


def _category_name(root: Message, candidate: str, depth: int = 0) -> str | None:
    if depth > 5:
        return None
    for child in root.submessages():
        if child.text(1) == candidate and child.text(2):
            return child.text(2)
        found = _category_name(child, candidate, depth + 1)
        if found:
            return found
    return None


def probe_tipos(provider: TiposProvider, candidate: int) -> tuple[str | None, set[str]]:
    import secrets

    body = {"LanguageID": LANGUAGE_SK, "Token": secrets.token_hex(16), "CategoryID": str(candidate), "Top": 20,
            "IncludeLiveCategories": False}
    payload = provider._client.post_json(provider.base_url + TIPOS_PATH, body)  # noqa: SLF001
    if not isinstance(payload, dict) or payload.get("Result") not in (1, "1") or not payload.get("ReturnValue"):
        return None, set()
    root = parse_return_value(str(payload["ReturnValue"]))
    ids = {br for br in (betradar_digits(e.text(5)) for e in iter_event_messages(root)) if br}
    return _category_name(root, str(candidate)), ids


PROBES: dict[str, Callable[[Any, int], tuple[str | None, set[str]]]] = {
    "doxxbet": probe_doxxbet,
    "tipos": probe_tipos,
    "synot": probe_tipos,
}


def discover(
    provider: Any,
    probe: Callable[[Any, int], tuple[str | None, set[str]]],
    wanted: Iterable[str],
    reference: Mapping[str, set[str]],
    *,
    skip: Iterable[Any] = (),
    max_candidate: int = MAX_CANDIDATE,
    on_found: Callable[[str, int], None] | None = None,
    stop: threading.Event | None = None,
) -> dict[str, int]:
    """Try candidate ids 1..max_candidate until every wanted sport is found. Returns {sport: id}."""
    wanted = set(wanted)
    skip = {str(s) for s in skip}
    found: dict[str, int] = {}
    errors = 0
    for candidate in range(1, max_candidate + 1):
        if not wanted - set(found) or (stop is not None and stop.is_set()):
            break
        if str(candidate) in skip:
            continue
        try:
            name, ids = probe(provider, candidate)
            errors = 0
        except BlockedError:
            raise
        except (ProviderError, ValueError, KeyError, TypeError, AttributeError) as exc:
            errors += 1
            log.debug("%s: sport id %d: %s", getattr(provider, "title", provider), candidate, exc)
            if errors >= MAX_CONSECUTIVE_ERRORS:
                log.warning("%s: too many errors while looking up sport ids; giving up for now", getattr(provider, "title", provider))
                break
            continue
        sport = sport_from_name(name) or classify(ids, reference)
        if sport in wanted and sport not in found:
            log.info("%s: %s has sport id %d", getattr(provider, "title", provider), sport, candidate)
            found[sport] = candidate
            if on_found:
                on_found(sport, candidate)
    return found


# ------------------------------------------------------------------ cache
class DiscoveryCache:
    """``{"doxxbet": {"found": {"hockey": 67}, "tried_v3": {"tennis": "<iso time>"}}}`` on disk."""

    def __init__(self, path: Path | str = CACHE_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self.data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def found(self, provider: str) -> dict[str, Any]:
        return dict((self.data.get(provider) or {}).get("found") or {})

    def due(self, provider: str, sport: str, now: datetime) -> bool:
        # "tried_v3": lookups made before DOXXbet's BetradarSportID was used are retried.
        tried = ((self.data.get(provider) or {}).get("tried_v3") or {}).get(sport)
        try:
            return tried is None or now - datetime.fromisoformat(tried) > RETRY_AFTER
        except (TypeError, ValueError):
            return True

    def record(self, provider: str, *, found: Mapping[str, Any] | None = None, tried: Iterable[str] = (), now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            entry = self.data.setdefault(provider, {})
            entry.setdefault("found", {}).update(found or {})
            for sport in tried:
                entry.setdefault("tried_v3", {})[sport] = now.isoformat()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError as exc:
                log.warning("cannot save %s: %s", self.path, exc)

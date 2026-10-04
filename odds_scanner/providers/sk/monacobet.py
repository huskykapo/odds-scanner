"""MONACObet (dualsoft platform): ``GET /restapi/offer/sk/sport/{S|H|B|T}/mob``.

The whole-sport feed is large (~5 MB for football), so the default poll interval is 120 s; set
``options.league_ids: {football: [2529497, ...]}`` to fetch only some leagues instead.

``betMap`` keys are tip types: 1 = home, 2 = draw, 3 = away, 227 = over, 228 = under (line in
``sv``, e.g. ``total=2.5``). More tip types (e.g. double chance) can be mapped with
``options.tip_types: {<tip type>: "1X"}``. ``kickOffTime`` is epoch ms UTC and ``brMatchId`` the
Betradar match id.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterator

from odds_scanner.markets import OVER, UNDER, usable_price
from odds_scanner.models import Event
from odds_scanner.providers.sk.common import SlovakProvider, betradar_digits, epoch_ms_to_utc, make_event

log = logging.getLogger(__name__)

BASE_URL = "https://ibet-monaco.dualsoft.bet/restapi/offer/sk"
QUERY = {"annex": "4", "mobileVersion": "2.3.22", "locale": "sk"}

DEFAULT_TIP_TYPES = {"1": "1", "2": "X", "3": "2", "227": OVER, "228": UNDER}
# Status codes that mean "not bettable right now". Unknown codes are accepted.
LOCKED_STATUSES = frozenset({"L", "B", "S", "D", "C", "X"})


def _iter_matches(node: Any) -> Iterator[dict]:
    """Every match object anywhere in the response (league and whole-sport feeds differ)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "betMap" in cur and "home" in cur and "away" in cur:
                yield cur
                continue
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def _line(sv: Any) -> float | None:
    text = str(sv or "")
    if "=" not in text:
        return None
    try:
        return float(text.split("=", 1)[1].split("|", 1)[0])
    except ValueError:
        return None


class MonacobetProvider(SlovakProvider):
    name = "monacobet"
    title = "MONACObet"
    homepage = "https://www.monacobet.sk"
    SPORT_OPTION = "sport_codes"
    DEFAULT_SPORTS = {"football": "S", "hockey": "H", "basketball": "B", "tennis": "T"}

    def _fetch_payloads(self, sport: str, code: Any) -> list[Any]:
        leagues = (self.options.get("league_ids") or {}).get(sport)
        if leagues:
            return [self._client.get_json(f"{BASE_URL}/sport/{code}/league/{lid}/mob", QUERY) for lid in leagues]
        return [self._client.get_json(f"{BASE_URL}/sport/{code}/mob", QUERY)]

    @classmethod
    def parse(cls, payload: Any, sport: str, fetched_at: datetime, tip_types: dict | None = None) -> list[Event]:
        tips = {**DEFAULT_TIP_TYPES, **{str(k): v for k, v in (tip_types or {}).items()}}
        events = []
        for m in _iter_matches(payload):
            if m.get("blocked") or m.get("live"):
                continue
            prices: dict[str, float] = {}
            totals: dict[float, dict[str, float]] = {}
            for tip_type, by_special in (m.get("betMap") or {}).items():
                code = tips.get(str(tip_type))
                if code is None or not isinstance(by_special, dict):
                    continue
                for sv, odd in by_special.items():
                    if not isinstance(odd, dict) or str(odd.get("s", "")).upper() in LOCKED_STATUSES:
                        continue
                    price = usable_price(odd.get("ov"))
                    if price is None:
                        continue
                    if code in (OVER, UNDER):
                        line = _line(odd.get("sv", sv))
                        if line is not None:
                            totals.setdefault(line, {})[code] = price
                    elif str(sv) in ("NULL", "", "None"):
                        prices[code] = price
            ev = make_event(
                bookmaker=cls.name, title=cls.title, native_id=m.get("id"), sport=sport,
                start=epoch_ms_to_utc(m["kickOffTime"]), home=str(m["home"]), away=str(m["away"]),
                prices=prices, totals=totals, fetched_at=fetched_at, betradar_id=betradar_digits(m.get("brMatchId")),
            )
            if ev is not None:
                events.append(ev)
        return events

    def _parse(self, payload: Any, sport: str, fetched_at: datetime) -> list[Event]:
        return self.parse(payload, sport, fetched_at, self.options.get("tip_types"))

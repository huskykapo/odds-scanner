"""DOXXbet: ``POST https://www.doxxbet.sk/offer/GetOfferList``.

``EventChanceTypes`` are markets of events; their odds live in ``Odds`` keyed
``"<EventChanceTypeID>_<TipType>"``. ``EventDate`` is Europe/Bratislava local time without an
offset. The Betradar id is in ``BetradarStatisticsUrn`` (``sr:match:<id>``).

``ChanceTypeID`` follows Betradar's market ids: ``uf:1`` = 1X2 (also carries the 1X/12/X2
tips), ``uf:10`` = double chance, ``uf:186`` = two-way winner (tennis).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from odds_scanner.errors import BlockedError, ProviderError
from odds_scanner.markets import (
    BTTS,
    DRAW_NO_BET,
    EVEN,
    FIRST_GOAL,
    FIRST_HALF,
    H2H_3_WAY,
    HANDICAP,
    HANDICAP_3WAY,
    MOST_CORNERS,
    NO,
    ODD,
    ODD_EVEN,
    OVER,
    SECOND_HALF,
    TEAM_TOTALS_AWAY,
    TEAM_TOTALS_HOME,
    TOTALS,
    UNDER,
    YES,
    usable_price,
)
from odds_scanner.models import Event, MarketOdds
from odds_scanner.providers.sk.common import (
    SlovakProvider,
    betradar_digits,
    bratislava_to_utc,
    group_markets,
    half_line,
    make_event,
    split_teams,
)

log = logging.getLogger(__name__)

URL = "https://www.doxxbet.sk/offer/GetOfferList"
DETAIL_URL = "https://www.doxxbet.sk/offer/GetOfferEventDetail"  # one match's full bet list
BASE_BODY = {"sportEvent": -1, "sport": 54, "region": -1, "leaugeCup": -1, "live": -1, "date": "TM", "top": 1, "streamOnly": -1}

RESULT_MARKETS = {"uf:1", "uf:10", "uf:186"}
TIPS = {"1": "1", "X": "X", "0": "X", "2": "2", "1X": "1X", "10": "1X", "12": "12", "X2": "X2", "02": "X2"}
ACTIVE = "active"


# Match-detail markets by Betradar (UOF) market id -> (market, period, kind). Only markets whose
# outcomes are exclusive and exhaustive and that other bookmakers also offer. Football.
_FT, _1H, _2H = None, FIRST_HALF, SECOND_HALF
DETAIL_MARKETS = {
    "uf:1": (H2H_3_WAY, _FT, "1x2"), "uf:60": (H2H_3_WAY, _1H, "1x2"), "uf:83": (H2H_3_WAY, _2H, "1x2"),
    "uf:11": (DRAW_NO_BET, _FT, "12"), "uf:64": (DRAW_NO_BET, _1H, "12"), "uf:86": (DRAW_NO_BET, _2H, "12"),
    "uf:18": (TOTALS, _FT, "ou"), "uf:68": (TOTALS, _1H, "ou"), "uf:90": (TOTALS, _2H, "ou"),
    "uf:19": (TEAM_TOTALS_HOME, _FT, "ou"), "uf:20": (TEAM_TOTALS_AWAY, _FT, "ou"),
    "uf:69": (TEAM_TOTALS_HOME, _1H, "ou"), "uf:70": (TEAM_TOTALS_AWAY, _1H, "ou"),
    "uf:91": (TEAM_TOTALS_HOME, _2H, "ou"), "uf:92": (TEAM_TOTALS_AWAY, _2H, "ou"),
    "uf:29": (BTTS, _FT, "yn"), "uf:75": (BTTS, _1H, "yn"), "uf:95": (BTTS, _2H, "yn"),
    "uf:26": (ODD_EVEN, _FT, "oe"), "uf:74": (ODD_EVEN, _1H, "oe"), "uf:94": (ODD_EVEN, _2H, "oe"),
    "uf:16": (HANDICAP, _FT, "hcp2"), "uf:66": (HANDICAP, _1H, "hcp2"), "uf:88": (HANDICAP, _2H, "hcp2"),
    "uf:14": (HANDICAP_3WAY, _FT, "hcp3"), "uf:65": (HANDICAP_3WAY, _1H, "hcp3"), "uf:87": (HANDICAP_3WAY, _2H, "hcp3"),
    "uf:8": (FIRST_GOAL, _FT, "1x2"),
    "uf:162": (MOST_CORNERS, _FT, "1x2"),
}
# Two-way tips: "1" is the first-named side (over / yes / odd / home), "2" the other.
_TWO_WAY = {"ou": (OVER, UNDER), "yn": (YES, NO), "oe": (ODD, EVEN), "12": ("1", "2"), "hcp2": ("1", "2")}
_NUM = re.compile(r"[+-]?\d+(?:[.,]\d+)?")
_SCORE = re.compile(r"(\d+)\s*:\s*(\d+)")


def _detail_line(kind: str, ect: dict) -> tuple[bool, float | None]:
    """(usable, line) of one market line. Lines are the home side's (handicaps) or the total."""
    limit, name = str(ect.get("ParamLimit") or ""), str(ect.get("ChanceTypeName") or "")
    if kind == "ou":
        nums = _NUM.findall(limit) or _NUM.findall(name)
        line = half_line(nums[-1]) if nums else None
        return line is not None, line
    if kind == "hcp2":  # "Handicap góly -1.5/+1.5": the first number is the home team's
        m = re.search(r"([+-]?\d+(?:[.,]\d+)?)\s*/\s*([+-]?\d+(?:[.,]\d+)?)", name)
        line = half_line(m.group(1)) if m else None
        return line is not None and line != 0, line
    if kind == "hcp3":  # "0:1" = home starts 0, away 1 -> home line -1
        m = _SCORE.search(limit) or _SCORE.search(name)
        return (m is not None), (float(int(m.group(1)) - int(m.group(2))) if m else None)
    return True, None


class DoxxbetProvider(SlovakProvider):
    name = "doxxbet"
    title = "DOXXbet"
    homepage = "https://www.doxxbet.sk"
    SPORT_OPTION = "sport_ids"
    # Only football (54) was verified; add the others from the site's network requests if needed.
    DEFAULT_SPORTS = {"football": 54}

    # "top": 1 is what the site's own page sends (probably "highlighted matches only"); -1 is
    # "any" for the other fields, so it likely means all matches. The one giving more is kept.
    DEFAULT_TOP_VALUES = (-1, 1)

    def top_candidates(self) -> list[Any]:
        body = self.options.get("body") or {}
        if "top" in body:
            return [body["top"]]
        return list(self.options.get("top_values") or self.DEFAULT_TOP_VALUES)

    def _fetch_payloads(self, sport: str, sport_id: Any) -> list[Any]:
        return self._fetch_best(sport, self.top_candidates(), lambda top: self._fetch_days(sport, sport_id, top), "top")

    def fetch_detail_raw(self, event_id: Any) -> Any:
        """The match page's full bet list (over/under, handicaps, ...), as the site sends it."""
        return self._client.post_json(DETAIL_URL, {"eventId": int(event_id)})

    def fetch_detail(self, event_id: Any, sport: str) -> list[MarketOdds]:
        try:
            return self.parse_detail(self.fetch_detail_raw(event_id), sport, self._clock())
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError(f"{self.title}: unexpected match detail format ({type(exc).__name__}: {exc})") from exc

    @staticmethod
    def parse_detail(payload: Any, sport: str, fetched_at: datetime) -> list[MarketOdds]:
        """Markets of one match page (``GetOfferEventDetail``). Football only (mapping verified there)."""
        if sport != "football" or not isinstance(payload, dict):
            return []
        ects = payload.get("EventChanceTypes") or {}
        ects = list(ects.values()) if isinstance(ects, dict) else list(ects)
        odds_by_ect: dict[Any, list[dict]] = {}
        for raw in payload.get("Odds") or []:
            if isinstance(raw, dict):
                odds_by_ect.setdefault(raw.get("EventChanceTypeID"), []).append(raw)
        rows = []
        for ect in ects:
            spec = DETAIL_MARKETS.get(str(ect.get("ChanceTypeID")))
            if spec is None or str(ect.get("EventChanceTypeStatus", ACTIVE)).lower() != ACTIVE:
                continue
            if str(ect.get("LiveBetting", "N")).upper() == "Y":
                continue
            base, period, kind = spec
            usable, line = _detail_line(kind, ect)
            if not usable:
                continue
            for raw in odds_by_ect.get(ect.get("EventChanceTypeID"), []):
                if str(raw.get("Status", ACTIVE)).lower() != ACTIVE:
                    continue
                price = usable_price(raw.get("OddsRate"))
                tip = str(raw.get("TipType", "")).upper()
                if price is None:
                    continue
                if kind in ("1x2", "hcp3"):
                    outcome = TIPS.get(tip) if kind == "1x2" else {"1": "1", "X": "X", "0": "X", "2": "2"}.get(tip)
                else:
                    pair = _TWO_WAY[kind]
                    outcome = pair[0] if tip == "1" else pair[1] if tip == "2" else None
                if outcome is not None:
                    rows.append((base, period, line, outcome, price))
        return group_markets(rows, fetched_at)

    def body_for(self, sport_id: Any, date: str, top: Any) -> dict[str, Any]:
        return {**BASE_BODY, **(self.options.get("body") or {}), "sport": sport_id, "date": date, "top": top}

    def _fetch_days(self, sport: str, sport_id: Any, top: Any) -> list[Any]:
        payloads, failures = [], []
        for date in self.options.get("dates") or ["TD", "TM"]:
            body = self.body_for(sport_id, date, top)
            try:
                payloads.append(self._client.post_json(URL, body))
            except BlockedError:
                raise
            except ProviderError as exc:
                log.warning("%s: %s date=%s failed: %s", self.title, sport, date, exc)
                failures.append(exc)
        if not payloads and failures:
            raise failures[-1]
        return payloads

    @classmethod
    def parse(cls, payload: Any, sport: str, fetched_at: datetime) -> list[Event]:
        odds_by_ect: dict[int, dict[str, float]] = {}
        for raw in (payload.get("Odds") or {}).values():
            if not isinstance(raw, dict) or str(raw.get("Status", ACTIVE)).lower() != ACTIVE:
                continue
            code = TIPS.get(str(raw.get("TipType", "")).upper())
            price = usable_price(raw.get("OddsRate"))
            if code and price is not None:
                odds_by_ect.setdefault(raw.get("EventChanceTypeID"), {})[code] = price

        grouped: dict[Any, dict[str, Any]] = {}
        for ect in payload.get("EventChanceTypes") or []:
            if str(ect.get("ChanceTypeID")) not in RESULT_MARKETS:
                continue
            if str(ect.get("EventChanceTypeStatus", ACTIVE)).lower() != ACTIVE or str(ect.get("LiveBetting", "N")).upper() == "Y":
                continue
            teams = split_teams(str(ect.get("EventName", "")))
            if teams is None:
                continue
            entry = grouped.setdefault(ect["EventID"], {"ect": ect, "teams": teams, "prices": {}})
            entry["prices"].update(odds_by_ect.get(ect.get("EventChanceTypeID"), {}))

        events = []
        for event_id, entry in grouped.items():
            ect = entry["ect"]
            start = bratislava_to_utc(datetime.fromisoformat(str(ect["EventDate"])[:19]))
            ev = make_event(
                bookmaker=cls.name, title=cls.title, native_id=event_id, sport=sport, start=start,
                home=entry["teams"][0], away=entry["teams"][1], prices=entry["prices"], fetched_at=fetched_at,
                betradar_id=betradar_digits(ect.get("BetradarStatisticsUrn")),
            )
            if ev is not None:
                events.append(ev)
        return events

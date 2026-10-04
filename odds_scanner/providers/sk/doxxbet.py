"""DOXXbet: ``POST https://www.doxxbet.sk/offer/GetOfferList``.

``EventChanceTypes`` are markets of events; their odds live in ``Odds`` keyed
``"<EventChanceTypeID>_<TipType>"``. ``EventDate`` is Europe/Bratislava local time without an
offset. The Betradar id is in ``BetradarStatisticsUrn`` (``sr:match:<id>``).

``ChanceTypeID`` follows Betradar's market ids: ``uf:1`` = 1X2 (also carries the 1X/12/X2
tips), ``uf:10`` = double chance, ``uf:186`` = two-way winner (tennis).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from odds_scanner.errors import BlockedError, ProviderError
from odds_scanner.models import Event
from odds_scanner.markets import usable_price
from odds_scanner.providers.sk.common import (
    SlovakProvider,
    betradar_digits,
    bratislava_to_utc,
    make_event,
    split_teams,
)

log = logging.getLogger(__name__)

URL = "https://www.doxxbet.sk/offer/GetOfferList"
BASE_BODY = {"sportEvent": -1, "sport": 54, "region": -1, "leaugeCup": -1, "live": -1, "date": "TM", "top": 1, "streamOnly": -1}

RESULT_MARKETS = {"uf:1", "uf:10", "uf:186"}
TIPS = {"1": "1", "X": "X", "0": "X", "2": "2", "1X": "1X", "10": "1X", "12": "12", "X2": "X2", "02": "X2"}
ACTIVE = "active"


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

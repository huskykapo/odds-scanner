"""Niké: ``GET https://www.nike.sk/api-gw/nikeone/v1/boxes/search/portal?...&menu=/futbal``.

Bets with header ``Zápas``: the first ``selectionGrid`` row is 1 / remíza / 2 (two cells for
tennis), the second row 1X / 12 / X2. ``expirationTime`` is the kick-off (ISO with offset). Niké
has no Betradar id, so its events are matched by team names and start time.

Paging: when ``hasMoreBets`` is true the next page is requested with ``options.page_param``
(default ``page``) = 2, 3, ... up to ``options.max_pages``; paging stops as soon as a page adds no
new bets, so an ignored parameter costs one extra request, not a loop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

from odds_scanner.markets import DC_CODES, TWO_WAY_SPORTS, usable_price
from odds_scanner.models import Event
from odds_scanner.providers.parsing import parse_datetime
from odds_scanner.providers.sk.common import SlovakProvider, make_event

log = logging.getLogger(__name__)

URL = "https://www.nike.sk/api-gw/nikeone/v1/boxes/search/portal"
QUERY = "betNumbers&date&live=false&menu={menu}&minutes&order&prematch=true&results=false"
MATCH_HEADER = "zápas"
WINNER_HEADER = "víťaz zápasu"  # tennis: two-way match winner


def _selections(row: Any) -> list[dict]:
    return [c for c in (row or []) if isinstance(c, dict) and c.get("type") == "selection"]


def _price(cell: dict) -> float | None:
    if cell.get("enabled") is False or cell.get("locked") is True:
        return None
    return usable_price(cell.get("odds"))


class NikeProvider(SlovakProvider):
    name = "nike"
    title = "Niké"
    homepage = "https://www.nike.sk"
    SPORT_OPTION = "menus"
    DEFAULT_SPORTS = {"football": "/futbal", "hockey": "/hokej", "basketball": "/basketbal", "tennis": "/tenis"}

    def _url(self, menu: str, page: int) -> str:
        url = f"{URL}?{QUERY.format(menu=quote(menu, safe='/'))}"
        if page > 1:
            url += f"&{self.options.get('page_param', 'page')}={page}"
        return url

    def _fetch_payloads(self, sport: str, menu: Any) -> list[Any]:
        menus = menu if isinstance(menu, (list, tuple)) else [menu]
        max_pages = int(self.options.get("max_pages", 5))
        payloads: list[Any] = []
        for m in menus:
            seen: set[str] = set()
            for page in range(1, max_pages + 1):
                payload = self._client.get_json(self._url(str(m), page))
                ids = {str(b.get("betId")) for b in (payload.get("bets") or []) if isinstance(b, dict)}
                if not ids - seen:
                    break  # nothing new: last page, or the site ignores the page parameter
                seen |= ids
                payloads.append(payload)
                if not payload.get("hasMoreBets"):
                    break
        return payloads

    @classmethod
    def parse(cls, payload: Any, sport: str, fetched_at: datetime) -> list[Event]:
        sport_events = {str(e.get("sportEventId")): e for e in payload.get("sportEvents") or [] if isinstance(e, dict)}
        events = []
        for bet in payload.get("bets") or []:
            header = str(bet.get("header", "")).strip().lower()
            # "Víťaz zápasu" only for no-draw sports: in basketball it would be the overtime-inclusive
            # winner, which must never be mixed with the regulation-time "Zápas" 1X2.
            if header != MATCH_HEADER and not (header == WINNER_HEADER and sport in TWO_WAY_SPORTS):
                continue
            if bet.get("game", "Prematch") != "Prematch" or bet.get("bettingState", "RUNNING") != "RUNNING":
                continue
            info = sport_events.get(str(bet.get("sportEventId")), {})
            if info.get("isLive"):
                continue
            participants = bet.get("participants") or info.get("participants") or []
            grid = bet.get("selectionGrid") or []
            if len(participants) != 2 or not grid:
                continue
            prices: dict[str, float] = {}
            first = _selections(grid[0])
            codes = ("1", "X", "2") if len(first) == 3 else ("1", "2") if len(first) == 2 else ()
            for code, cell in zip(codes, first):
                price = _price(cell)
                if price is not None:
                    prices[code] = price
            for row in grid[1:]:
                for cell in _selections(row):
                    name = str(cell.get("name", "")).upper()
                    price = _price(cell)
                    if name in DC_CODES and price is not None:
                        prices[name] = price
            start = parse_datetime(bet.get("expirationTime") or info.get("expiration"))
            if start is None:
                continue
            ev = make_event(
                bookmaker=cls.name, title=cls.title, native_id=bet.get("sportEventId"), sport=sport, start=start,
                home=str(participants[0]), away=str(participants[1]), prices=prices, fetched_at=fetched_at,
            )
            if ev is not None:
                events.append(ev)
        return events

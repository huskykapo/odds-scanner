"""Tipos: ``POST https://tipkurz.etipos.sk/WebServices/Api/SportsBettingService.svc/GetWebStandardEvents``.

The JSON reply wraps a base64 protobuf in ``ReturnValue``; it is read with the schema-less
decoder in :mod:`odds_scanner.providers.sk.protobuf`. Layout (field numbers) found in the data:

    event:  1 = id, 2 = "Home - Away", 4 = {1: kick-off epoch ms}, 5 = Betradar id, 6 = market groups
    market: 2 = name ("Zápas"), 4 = tips
    tip:    1 = id, 2 = tip ("1", "0" = draw, "2", "10"/"12"/"02" = double chance), 3 = float32 odds

Rather than hard-coding the nesting path, the parser looks for messages with that shape, so an
extra wrapper level in other categories does not break it. Synot runs the same platform.
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
from datetime import datetime
from typing import Any, Iterator

from odds_scanner.errors import ProviderError
from odds_scanner.markets import usable_price
from odds_scanner.models import Event
from odds_scanner.providers.sk.common import SlovakProvider, betradar_digits, epoch_ms_to_utc, make_event, split_teams
from odds_scanner.providers.sk.protobuf import Message

log = logging.getLogger(__name__)

PATH = "/WebServices/Api/SportsBettingService.svc/GetWebStandardEvents"
DETAIL_PATH = "/WebServices/Api/SportsBettingService.svc/GetWebStandardEventExt"  # one match's full bet list
LANGUAGE_SK = 17

MATCH_MARKET = "zápas"
TIPS = {"1": "1", "0": "X", "X": "X", "2": "2", "10": "1X", "1X": "1X", "12": "12", "02": "X2", "X2": "X2"}
DC_ONLY = {"1X", "12", "X2"}
_MS_MIN, _MS_MAX = 10**12, 10**13  # plausible epoch-millisecond range


def _is_dc_market(name: str) -> bool:
    low = name.lower()
    return ("dvojit" in low or "šanc" in low or "dvojtip" in low) and "polč" not in low


def _looks_like_event(m: Message) -> bool:
    name = m.text(2)
    when = m.message(4)
    start = when.int(1) if when else None
    return bool(name and " - " in name and start and _MS_MIN <= start < _MS_MAX and 6 in m.fields)


def iter_event_messages(root: Message, depth: int = 0) -> Iterator[Message]:
    if depth > 12:
        return
    for child in root.submessages():
        if _looks_like_event(child):
            yield child
        else:
            yield from iter_event_messages(child, depth + 1)


def _iter_markets(node: Message, depth: int = 0) -> Iterator[tuple[str, list[Message]]]:
    """(market name, tip messages) for every message that has odds-bearing tips in field 4."""
    if depth > 8:
        return
    tips = [t for t in node.messages(4) if t.text(2) is not None and t.float32(3) is not None]
    if tips:
        yield node.text(2) or "", tips
        return
    for child in node.submessages():
        yield from _iter_markets(child, depth + 1)


def parse_return_value(return_value: str) -> Message:
    try:
        raw = base64.b64decode(return_value, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"ReturnValue is not base64: {exc}") from exc
    return Message(raw, lenient=True)


def parse_events(payload: Any, sport: str, fetched_at: datetime, *, bookmaker: str, title: str) -> list[Event]:
    if payload.get("Result") not in (1, "1", None):
        raise ValueError(f"API Result={payload.get('Result')!r}")
    root = parse_return_value(str(payload.get("ReturnValue") or ""))
    events = []
    for em in iter_event_messages(root):
        teams = split_teams(em.text(2) or "", (" - ",))
        if teams is None:
            continue
        prices: dict[str, float] = {}
        for group in em.messages(6):
            for name, tips in _iter_markets(group):
                is_match = name.strip().lower() == MATCH_MARKET
                if not is_match and not _is_dc_market(name):
                    continue
                for tip in tips:
                    code = TIPS.get((tip.text(2) or "").strip().upper())
                    if code is None or (not is_match and code not in DC_ONLY):
                        continue
                    f32 = tip.float32(3)
                    price = usable_price(round(f32, 2)) if f32 is not None else None  # float32 noise: 3.8199999
                    if price is not None:
                        prices.setdefault(code, price)
        ev = make_event(
            bookmaker=bookmaker, title=title, native_id=em.int(1), sport=sport,
            start=epoch_ms_to_utc(em.message(4).int(1)),  # type: ignore[union-attr]
            home=teams[0], away=teams[1], prices=prices, fetched_at=fetched_at,
            betradar_id=betradar_digits(em.text(5)),
        )
        if ev is not None:
            events.append(ev)
    return events


class TiposProvider(SlovakProvider):
    name = "tipos"
    title = "Tipos"
    homepage = "https://tipkurz.etipos.sk"
    base_url = "https://tipkurz.etipos.sk"
    SPORT_OPTION = "category_ids"
    # Only football ("28") was verified; add the other sports' CategoryID from the site's requests.
    DEFAULT_SPORTS = {"football": "28"}

    # The site's own page asks for Top 50; bigger pages are tried and the best one is kept.
    DEFAULT_TOP_VALUES = (500, 200, 50)

    def top_candidates(self) -> list[int]:
        if "top" in self.options:
            return [int(self.options["top"])]
        return [int(v) for v in (self.options.get("top_values") or self.DEFAULT_TOP_VALUES)]

    def _fetch_payloads(self, sport: str, category_id: Any) -> list[Any]:
        return self._fetch_best(sport, self.top_candidates(), lambda top: self._fetch_top(category_id, top), "Top")

    def _fetch_top(self, category_id: Any, top: int) -> list[Any]:
        body = {
            "LanguageID": LANGUAGE_SK,
            "Token": secrets.token_hex(16),  # the API wants any 32 hex chars
            "CategoryID": str(category_id),
            "Top": top,
            "IncludeLiveCategories": False,
        }
        payload = self._client.post_json(self.base_url + PATH, body)
        if not isinstance(payload, dict):
            raise ProviderError(f"{self.title}: unexpected response type {type(payload).__name__}")
        if payload.get("Result") not in (1, "1"):
            raise ProviderError(f"{self.title}: the API refused the request (Result={payload.get('Result')!r})")
        return [payload]

    def fetch_detail_raw(self, event_id: Any, long_polling: bool = False) -> Any:
        """The match page's full bet list, as the site sends it (base64 protobuf in ReturnValue)."""
        body = {"EventID": int(event_id), "LanguageID": LANGUAGE_SK, "Token": secrets.token_hex(16),
                "UseLongPolling": long_polling}
        return self._client.post_json(self.base_url + DETAIL_PATH, body)

    @classmethod
    def parse(cls, payload: Any, sport: str, fetched_at: datetime) -> list[Event]:
        return parse_events(payload, sport, fetched_at, bookmaker=cls.name, title=cls.title)

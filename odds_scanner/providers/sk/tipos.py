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
import re
import secrets
from datetime import datetime
from typing import Any, Iterator

from odds_scanner.errors import ProviderError
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
    epoch_ms_to_utc,
    group_markets,
    half_line,
    make_event,
    split_teams,
)
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


# Match-detail markets by the platform's market code (the part of field 1 before "d") ->
# (market, period, kind). Same codes on Tipos and Synot. Checked against DOXXbet for the same
# Betradar matches (captured 2026-10-04). Football.
DETAIL_MARKETS = {
    "19": (H2H_3_WAY, None, "1x2"), "20": (H2H_3_WAY, None, "1x2"), "64": (H2H_3_WAY, FIRST_HALF, "1x2"),
    "21": (DRAW_NO_BET, None, "12"),
    "25": (TOTALS, None, "ou"), "69": (TOTALS, FIRST_HALF, "ou"), "89": (TOTALS, SECOND_HALF, "ou"),
    "27": (TEAM_TOTALS_HOME, None, "ou"), "28": (TEAM_TOTALS_AWAY, None, "ou"),
    "70": (TEAM_TOTALS_HOME, FIRST_HALF, "ou"), "71": (TEAM_TOTALS_AWAY, FIRST_HALF, "ou"),
    "90": (TEAM_TOTALS_HOME, SECOND_HALF, "ou"), "91": (TEAM_TOTALS_AWAY, SECOND_HALF, "ou"),
    "36": (BTTS, None, "yn"), "74": (BTTS, FIRST_HALF, "yn"), "94": (BTTS, SECOND_HALF, "yn"),
    "33": (ODD_EVEN, None, "oe"),
    "24": (HANDICAP, None, "hcp2"), "68": (HANDICAP, FIRST_HALF, "hcp2"),
    "22": (HANDICAP_3WAY, None, "hcp3"), "169": (HANDICAP_3WAY, FIRST_HALF, "hcp3"),
    "79": (FIRST_GOAL, None, "1x2"),
    "209": (MOST_CORNERS, None, "1x2"),
}
_DETAIL_TIPS = {
    "1x2": {"1": "1", "0": "X", "X": "X", "2": "2", "10": "1X", "1X": "1X", "12": "12", "02": "X2", "X2": "X2"},
    "12": {"1": "1", "2": "2"},
    "yn": {"ÁNO": YES, "NIE": NO},
    "oe": {"NEPÁR": ODD, "PÁR": EVEN},
}
_TIP_LINE = re.compile(r"\(\s*([+-]?\d+(?:[.,]\d+)?)\s*\)")
_SCORE = re.compile(r"(\d+)\s*:\s*(\d+)")


def _detail_rows(code: str, bet_name: str, tips: list[Message]) -> list[tuple]:
    base, period, kind = DETAIL_MARKETS[code]
    rows = []
    hcp3_line = None
    if kind == "hcp3":
        m = _SCORE.search(bet_name)
        if m is None:
            return []
        hcp3_line = float(int(m.group(1)) - int(m.group(2)))
    for tip in tips:
        label = (tip.text(2) or "").strip()
        f32 = tip.float32(3)
        price = usable_price(round(f32, 2)) if f32 is not None else None
        if price is None:
            continue
        upper = label.upper()
        if kind == "ou":  # "Nad (2.5)" / "Pod (2.5)"
            m = _TIP_LINE.search(label)
            line = half_line(m.group(1)) if m else None
            outcome = OVER if upper.startswith("NAD") else UNDER if upper.startswith("POD") else None
            if line is not None and outcome:
                rows.append((base, period, line, outcome, price))
        elif kind == "hcp2":  # "Tím 1 (-1.5)" / "Tím 2 (+1.5)": the line is stored as the home side's
            m = _TIP_LINE.search(label)
            value = half_line(m.group(1)) if m else None
            if value is None or value == 0:
                continue
            if upper.startswith("TÍM 1"):
                rows.append((base, period, value, "1", price))
            elif upper.startswith("TÍM 2"):
                rows.append((base, period, -value, "2", price))
        elif kind == "hcp3":  # "Tím 1" / "Remíza" / "Tím 2" (sometimes followed by "(0:1)")
            outcome = "1" if upper.startswith("TÍM 1") else "2" if upper.startswith("TÍM 2") else \
                "X" if upper.startswith(("REMÍZA", "X")) else None
            if outcome:
                rows.append((base, period, hcp3_line, outcome, price))
        else:
            outcome = _DETAIL_TIPS[kind].get(upper)
            if outcome:
                rows.append((base, period, None, outcome, price))
    return rows


def parse_detail_markets(payload: Any, sport: str, fetched_at: datetime, event_id: Any = None) -> list[MarketOdds]:
    """Markets of one match page (``GetWebStandardEventExt``). Football only (mapping verified there)."""
    if sport != "football" or not isinstance(payload, dict):
        return []
    if payload.get("Result") not in (1, "1"):
        raise ValueError(f"API Result={payload.get('Result')!r}")
    root = parse_return_value(str(payload.get("ReturnValue") or ""))
    rows: list[tuple] = []
    for em in iter_event_messages(root):
        if event_id is not None and str(em.int(1)) != str(event_id):
            continue
        for group in em.messages(6):
            for market in group.messages(3):
                code = (market.text(1) or "").split("d", 1)[0]
                if code not in DETAIL_MARKETS:
                    continue
                for bet in market.messages(6):
                    rows.extend(_detail_rows(code, bet.text(2) or "", bet.messages(4)))
        break  # one match per page
    return group_markets(rows, fetched_at)


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

    def fetch_detail(self, event_id: Any, sport: str) -> list[MarketOdds]:
        try:
            return parse_detail_markets(self.fetch_detail_raw(event_id), sport, self._clock(), event_id)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError(f"{self.title}: unexpected match detail format ({type(exc).__name__}: {exc})") from exc

    @classmethod
    def parse(cls, payload: Any, sport: str, fetched_at: datetime) -> list[Event]:
        return parse_events(payload, sport, fetched_at, bookmaker=cls.name, title=cls.title)

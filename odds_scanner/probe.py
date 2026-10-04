"""``probe``: check each live bookmaker endpoint once. ``probe-fortuna``: does Fortuna serve data?"""

from __future__ import annotations

import re
import time
from typing import Callable

import requests

from odds_scanner.config import SK_SOURCES, Config
from odds_scanner.errors import BlockedError, ProviderError
from odds_scanner.providers.sk import SK_PROVIDERS
from odds_scanner.providers.sk.http import DEFAULT_HEADERS, SiteClient, looks_like_challenge

FORTUNA_URL = "https://www.ifortuna.sk/stavkovanie/futbal"

# Markers of server-rendered match/odds data on a betting page.
FORTUNA_MARKERS = ("odds-value", "data-odd", "odds_button", "odds-button", "\"odds\"", "event-name", "\"eventName\"", "market-", "betslip")
_ODDS_NUMBER = re.compile(r">\s*\d{1,3}[.,]\d{2}\s*<")


def probe(cfg: Config, out: Callable[[str], None] = print, providers: dict | None = None) -> int:
    """Fetch one sport from every Slovak provider (enabled or not) and print OK / BLOCKED / ERROR."""
    providers = providers if providers is not None else SK_PROVIDERS
    width = max(len(n) for n in SK_SOURCES)
    all_ok = True
    for name in SK_SOURCES:
        sc = cfg.providers[name]
        options = {k: v for k, v in sc.options.items() if k != "sample_files"}  # always hit the live site
        provider = providers[name](
            options=options, timeout=sc.timeout_seconds, max_retries=0, min_interval=sc.min_request_interval_seconds
        )
        sports = [s for s in (sc.sports or cfg.sports) if provider.supports(s)]
        sport = "football" if "football" in sports else (sports[0] if sports else None)
        label = name.ljust(width)
        if sport is None:
            out(f"{label}  SKIPPED  no configured sport")
            continue
        started = time.monotonic()
        try:
            n = len(provider.fetch_odds(sport).events)
            out(f"{label}  OK       {n} {sport} event(s) with usable odds ({time.monotonic() - started:.1f}s)")
        except BlockedError as exc:
            all_ok = False
            out(f"{label}  BLOCKED  {exc}")
        except ProviderError as exc:
            all_ok = False
            out(f"{label}  ERROR    {exc}")
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not crash
            all_ok = False
            out(f"{label}  ERROR    {type(exc).__name__}: {exc}")
        finally:
            provider.close()
    return 0 if all_ok else 1


def probe_fortuna(out: Callable[[str], None] = print, session: requests.Session | None = None) -> int:
    """One plain GET of Fortuna's football page; report only whether it contains match data."""
    if session is None:
        session = requests.Session()
        session.headers.update({**DEFAULT_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    client = SiteClient("Fortuna", session=session, max_retries=0)
    try:
        response = client.request("GET", FORTUNA_URL)
    except BlockedError as exc:
        out(f"fortuna  BLOCKED  {exc}")
        return 1
    except ProviderError as exc:
        out(f"fortuna  ERROR    {exc}")
        return 1
    finally:
        client.close()
    text = response.text or ""
    markers = [m for m in FORTUNA_MARKERS if m in text]
    numbers = len(_ODDS_NUMBER.findall(text))
    has_data = numbers >= 10 or len(markers) >= 2
    out(f"fortuna  HTTP {response.status_code}, {len(text):,} characters, {numbers} odds-like numbers, markers: {', '.join(markers) or 'none'}")
    if looks_like_challenge(text):
        out("fortuna  the page looks like a bot-check / captcha page")
        has_data = False
    out(f"fortuna  contains match data: {'YES' if has_data else 'NO'}")
    return 0 if has_data else 1

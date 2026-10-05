"""Coverage checks for paid/free odds aggregators: *which bookmakers can my key actually see?*

``py start.py diagnostics oddspapi`` / ``diagnostics sportmonks`` call the aggregator's own
"list bookmakers" endpoint with your key and report whether Roobet, Stake, Tipsport, Chance (and a few
reference books) are in the list. That is the cheap test to run BEFORE any adapter is written, because
vendors advertise far more than a given plan returns.

* Keys come from environment variables only (``ODDSPAPI_API_KEY``, ``SPORTMONKS_API_TOKEN``) and are
  never printed or logged. Network errors are reported by type only, since a message can contain the URL.
* Being listed does not prove that odds are returned for your plan and sports; the report says so.
* Read-only: a handful of list requests, >= ``MIN_INTERVAL`` apart.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

import requests

from odds_scanner.diagnostics import BookmakerReport, new_session

MIN_INTERVAL = 1.2
TIMEOUT = 20.0
MAX_PAGES = 20

TARGETS: dict[str, tuple[str, ...]] = {
    "Roobet": ("roobet",),
    "Stake": ("stake",),
    "Tipsport": ("tipsport",),
    "Chance": ("chance",),
    "Fortuna": ("fortuna",),
    "Synot": ("synot",),
    "Pinnacle": ("pinnacle",),
}
NAME_KEYS = frozenset({"name", "title", "slug", "key", "bookmaker", "bookmakername", "displayname", "display_name", "label"})


def bookmaker_names(data: Any, *, limit: int = 200_000) -> set[str]:
    """Every bookmaker-name-like string in a parsed JSON answer, whatever its exact shape."""
    names: set[str] = set()
    stack = [data]
    seen = 0
    while stack and seen < limit:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and str(key).lower() in NAME_KEYS and value.strip():
                    names.add(value.strip())
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, str) and item.strip():
                    names.add(item.strip())
                elif isinstance(item, (dict, list)):
                    stack.append(item)
    return names


def match_targets(names: set[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for label, needles in TARGETS.items():
        hits = sorted(n for n in names if any(needle in n.lower() for needle in needles))
        found[label] = hits
    return found


def _summarise(source: str, title: str, names: set[str], requests_sent: int, extra: list[str]) -> BookmakerReport:
    found = match_targets(names)
    report = BookmakerReport(title, source, "OK", "200", events="n/a", markets="n/a", odds="n/a")
    report.details = [f"bookmakers visible to this key: {len(names)}   (requests sent: {requests_sent})"]
    for label, hits in found.items():
        report.details.append(f"  {label:<9} {'FOUND: ' + ', '.join(hits[:6]) if hits else 'not in the list'}")
    report.details += extra
    report.details.append("listed is not the same as 'returns odds on your plan': fetch one fixture's odds before building an adapter")
    return report


def _failure(title: str, source: str, status: str, http: str, error: str) -> BookmakerReport:
    return BookmakerReport(title, source, status, http, error=error)


def _no_key(title: str, source: str, env: str, where: str) -> BookmakerReport:
    return BookmakerReport(
        title, source, "NO_KEY",
        error=f"environment variable {env} is not set. Get a key at {where}, then in PowerShell:  $env:{env} = \"YOUR-KEY\"  (never paste it into chat or a file)",
    )


def _classify_status(status: int) -> tuple[str, str]:
    if status in (401, 403):
        return "AUTH_FAILED", "the aggregator rejected the key (wrong, expired or not allowed for this endpoint)"
    if status == 429:
        return "RATE_LIMITED", "the aggregator's rate/quota limit was hit; wait and try again"
    return "ERROR", f"HTTP {status}"


# ---------------------------------------------------------------------- OddsPapi
ODDSPAPI_URL = "https://api.oddspapi.io/v4/bookmakers"


def check_oddspapi(*, environ: Any = None, session: Any = None) -> BookmakerReport:
    env = os.environ if environ is None else environ
    title, source = "OddsPapi (coverage)", "https://api.oddspapi.io/v4/bookmakers"
    key = env.get("ODDSPAPI_API_KEY")
    if not key:
        return _no_key(title, source, "ODDSPAPI_API_KEY", "https://oddspapi.io")
    session = session or new_session()
    try:
        response = session.get(ODDSPAPI_URL, params={"apiKey": key}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return _failure(title, source, "UNREACHABLE", "n/a", f"no HTTP answer ({type(exc).__name__}); says nothing about the service")
    if response.status_code != 200:
        status, error = _classify_status(response.status_code)
        return _failure(title, source, status, str(response.status_code), error)
    try:
        data = response.json()
    except ValueError:
        return _failure(title, source, "ERROR", "200", "the answer was not JSON")
    return _summarise(source, title, bookmaker_names(data), 1, [])


# ---------------------------------------------------------------------- SportMonks
SPORTMONKS_URL = "https://api.sportmonks.com/v3/odds/bookmakers"


def check_sportmonks(
    *, environ: Any = None, session: Any = None, sleep: Callable[[float], None] = time.sleep
) -> BookmakerReport:
    env = os.environ if environ is None else environ
    title, source = "SportMonks (coverage)", "https://api.sportmonks.com/v3/odds/bookmakers"
    token = env.get("SPORTMONKS_API_TOKEN")
    if not token:
        return _no_key(title, source, "SPORTMONKS_API_TOKEN", "https://www.sportmonks.com (14-day trial on paid plans)")
    session = session or new_session()
    names: set[str] = set()
    pages = 0
    note: list[str] = []
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            sleep(MIN_INTERVAL)
        try:
            response = session.get(SPORTMONKS_URL, params={"api_token": token, "per_page": 50, "page": page}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            return _failure(title, source, "UNREACHABLE", "n/a", f"no HTTP answer ({type(exc).__name__}); says nothing about the service")
        if response.status_code != 200:
            status, error = _classify_status(response.status_code)
            return _failure(title, source, status, str(response.status_code), error)
        try:
            data = response.json()
        except ValueError:
            return _failure(title, source, "ERROR", "200", "the answer was not JSON")
        pages += 1
        before = len(names)
        names |= bookmaker_names(data.get("data", data) if isinstance(data, dict) else data)
        has_more = bool((data.get("pagination") or {}).get("has_more")) if isinstance(data, dict) else False
        if not has_more or len(names) == before:
            break
    else:
        note.append(f"stopped after {MAX_PAGES} pages; the list may be longer")
    return _summarise(source, title, names, pages, note)

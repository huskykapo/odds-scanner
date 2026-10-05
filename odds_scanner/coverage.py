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
    "MyStake": ("mystake",),
    "Tipsport": ("tipsport",),
    "Chance": ("chance",),
    "Fortuna": ("fortuna",),
    "Synot": ("synot",),
    "Pinnacle": ("pinnacle",),
}
EXCLUDE: dict[str, tuple[str, ...]] = {"Stake": ("mystake",)}  # a different brand that merely contains "stake"
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
        skip = EXCLUDE.get(label, ())
        hits = sorted(n for n in names if any(needle in n.lower() for needle in needles) and not any(x in n.lower() for x in skip))
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


def _snippet(response: Any, secret: str) -> str:
    """The service's own error text (first ~160 chars), with the key removed and HTML ignored."""
    try:
        text = response.text if isinstance(response.text, str) else ""
    except Exception:  # noqa: BLE001
        return ""
    text = " ".join(text.split())
    if not text or text.lstrip().startswith("<"):
        return ""
    return text.replace(secret, "***")[:160]


def _classify_status(status: int, detail: str = "") -> tuple[str, str]:
    status_text, error = _classify_status_base(status)
    return status_text, error + (f" - service says: {detail}" if detail else "")


def _classify_status_base(status: int) -> tuple[str, str]:
    if status in (401, 403):
        return "AUTH_FAILED", "the aggregator rejected the key (wrong, expired or not allowed for this endpoint)"
    if status == 429:
        return "RATE_LIMITED", "the aggregator's rate/quota limit was hit; wait and try again"
    return "ERROR", f"HTTP {status}"


def _get_retrying(
    session: Any, url: str, params: dict[str, Any], sleep: Callable[[float], None], *, retries: int = 2, pause: float = 5.0,
) -> tuple[Any, str, int]:
    """GET with a couple of polite retries for transient failures (network error or HTTP 5xx).

    Returns (response or None, problem text if no response, requests actually sent). 4xx answers are
    never retried: they are answers (bad key, quota), not hiccups. Each retry may count against a
    metered quota, so there are only ``retries`` of them and they are spaced ``pause`` seconds apart.
    """
    problem, response = "", None
    for attempt in range(retries + 1):
        if attempt:
            sleep(pause * attempt)
        try:
            response = session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            response, problem = None, f"no HTTP answer ({type(exc).__name__})"
            continue
        problem = ""
        if response.status_code < 500:
            return response, "", attempt + 1
    return response, problem, retries + 1


# ---------------------------------------------------------------------- OddsPapi
ODDSPAPI_URL = "https://api.oddspapi.io/v4/bookmakers"


def check_oddspapi(
    *, environ: Any = None, session: Any = None, save_dir: Any = None,
    sleep: Callable[[float], None] = time.sleep, today: Any = None,
) -> BookmakerReport:
    env = os.environ if environ is None else environ
    title, source = "OddsPapi (coverage)", "https://api.oddspapi.io/v4/bookmakers"
    key = env.get("ODDSPAPI_API_KEY")
    if not key:
        return _no_key(title, source, "ODDSPAPI_API_KEY", "https://oddspapi.io")
    session = session or new_session()
    response, problem, attempts = _get_retrying(session, ODDSPAPI_URL, {"apiKey": key}, sleep)
    if response is None:
        return _failure(title, source, "UNREACHABLE", "n/a", f"{problem}; says nothing about the service (tried {attempts} times)")
    if response.status_code != 200:
        status, error = _classify_status(response.status_code, _snippet(response, key))
        if attempts > 1:
            error += f" (tried {attempts} times)"
        return _failure(title, source, status, str(response.status_code), error)
    try:
        data = response.json()
    except ValueError:
        return _failure(title, source, "ERROR", "200", "the answer was not JSON")
    names = bookmaker_names(data)
    extra: list[str] = []
    requests_sent = attempts
    if save_dir:
        extra, used = sample_oddspapi(save_dir, key=key, session=session, names=names, sleep=sleep, today=today)
        requests_sent += used
    return _summarise(source, title, names, requests_sent, extra)


# ---------------------------------------------------------------------- OddsPapi: one real sample
FIXTURES_URL = "https://api.oddspapi.io/v4/fixtures"
ODDS_URL = "https://api.oddspapi.io/v4/odds"
FOOTBALL_SPORT_ID = 10  # "soccer" in the vendor's own fixtures example
SAMPLE_BOOKMAKERS = ("roobet", "stake", "mystake", "pinnacle")  # slugs as they appear in the bookmaker list


def find_key(data: Any, key: str, *, limit: int = 200_000) -> Any:
    """First value stored under ``key`` anywhere in parsed JSON (breadth-first)."""
    stack, seen = [data], 0
    while stack and seen < limit:
        node = stack.pop(0)
        seen += 1
        if isinstance(node, dict):
            if key in node:
                return node[key]
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def sample_oddspapi(
    save_dir: Any, *, key: str, session: Any, names: set[str], sleep: Callable[[float], None], today: Any = None,
) -> tuple[list[str], int]:
    """Fetch the next days' football fixtures and ONE fixture's odds for Roobet/Stake/Pinnacle; save both.

    Costs two requests of the vendor quota. Saved files hold public odds data only (the key is never
    written). Returns (report lines, requests sent).
    """
    import json
    from datetime import date, timedelta
    from pathlib import Path

    lines: list[str] = []
    sent = 0
    day = today or date.today()
    wanted = [slug for slug in SAMPLE_BOOKMAKERS if slug in {n.lower() for n in names}] or list(SAMPLE_BOOKMAKERS)

    def get(url: str, params: dict[str, Any]) -> Any:
        nonlocal sent
        sleep(MIN_INTERVAL)  # also before the first one: the bookmaker-list request just went out
        response, problem, attempts = _get_retrying(session, url, {**params, "apiKey": key}, sleep)
        sent += attempts
        if response is None:
            raise _SampleError(problem)
        if response.status_code != 200:
            detail = _snippet(response, key)
            raise _SampleError(f"HTTP {response.status_code}" + (f" - service says: {detail}" if detail else ""))
        try:
            return response.json()
        except ValueError:
            raise _SampleError("the answer was not JSON") from None

    def save(name: str, data: Any) -> None:
        target = Path(save_dir)
        target.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False)
        (target / name).write_text(text, encoding="utf-8")
        lines.append(f"saved {name} ({len(text)} characters; public odds data only, no key)")

    try:
        fixtures = get(FIXTURES_URL, {"sportId": FOOTBALL_SPORT_ID, "from": day.isoformat(),
                                      "to": (day + timedelta(days=3)).isoformat(), "hasOdds": "true"})
        save("oddspapi_fixtures.json", fixtures)
        count = _count_key(fixtures, "fixtureId")
        lines.append(f"fixtures with odds in the next 3 days (football): {count}")
        fixture_id = find_key(fixtures, "fixtureId")
        if not isinstance(fixture_id, (str, int)):
            lines.append("no fixture id found in the answer: send me oddspapi_fixtures.json so I can read its structure")
            return lines, sent
        odds = get(ODDS_URL, {"fixtureId": fixture_id, "bookmakers": ",".join(wanted), "oddsFormat": "decimal", "verbosity": 3})
        save("oddspapi_odds.json", odds)
        book_odds = find_key(odds, "bookmakerOdds")
        if isinstance(book_odds, dict):
            for slug in wanted:
                node = book_odds.get(slug)
                if node is None:
                    lines.append(f"  {slug:<9} NO ODDS returned for this fixture on this plan")
                else:
                    markets = node.get("markets") if isinstance(node, dict) else None
                    lines.append(f"  {slug:<9} odds returned" + (f" ({len(markets)} markets)" if isinstance(markets, (dict, list)) else ""))
        else:
            lines.append("odds saved, but the 'bookmakerOdds' structure was not recognised: send me oddspapi_odds.json")
    except _SampleError as exc:
        lines.append(f"sample failed: {exc} (requests used: {sent})")
    return lines, sent


class _SampleError(Exception):
    pass


def _count_key(data: Any, key: str, *, limit: int = 200_000) -> int:
    stack, seen, found = [data], 0, 0
    while stack and seen < limit:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            found += key in node
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return found


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
            status, error = _classify_status(response.status_code, _snippet(response, token))
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


# ---------------------------------------------------------------------- Odds-API.io
ODDSAPIIO_URL = "https://api.odds-api.io/v3/bookmakers"


def check_oddsapiio(*, environ: Any = None, session: Any = None) -> BookmakerReport:
    """Which bookmakers can this Odds-API.io key use? (``GET /v3/bookmakers``, one request)"""
    env = os.environ if environ is None else environ
    title, source = "Odds-API.io (coverage)", ODDSAPIIO_URL
    key = env.get("ODDSAPIIO_API_KEY")
    if not key:
        return _no_key(title, source, "ODDSAPIIO_API_KEY", "https://odds-api.io")
    session = session or new_session()
    try:
        response = session.get(ODDSAPIIO_URL, params={"apiKey": key}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return _failure(title, source, "UNREACHABLE", "n/a", f"no HTTP answer ({type(exc).__name__}); says nothing about the service")
    if response.status_code != 200:
        status, error = _classify_status(response.status_code, _snippet(response, key))
        if response.status_code == 404:
            error += " (the endpoint may have changed: tell the developer)"
        return _failure(title, source, status, str(response.status_code), error)
    try:
        data = response.json()
    except ValueError:
        return _failure(title, source, "ERROR", "200", "the answer was not JSON")
    return _summarise(source, title, bookmaker_names(data), 1, [
        "on a plan with a fixed number of bookmakers (e.g. 2), you choose which ones: check Roobet/Stake/MyStake can be selected"])

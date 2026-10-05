"""Read-only diagnostics for every bookmaker: ``python -m odds_scanner.diagnostics tipsport``.

Two kinds of check, one report format:

* **Site probe** (Tipsport SK, Chance SK - no adapter yet): a handful of plain GET/POST requests to
  the public offer endpoints, to learn whether anonymous access works, whether a security challenge
  is returned, and whether the answer contains structured odds. At most ~10 requests per site, at
  least ``MIN_INTERVAL`` seconds apart, sent with an honest User-Agent.
  **The probe stops at the first 403 / 429 / captcha or bot-check page and never retries with other
  headers, cookies or tricks.** That is a verdict, not an obstacle to engineer around.
* **Provider check** (the five working Slovak providers): one normal ``fetch_odds`` for football.

Nothing here logs in, places or prepares a bet. No secret is ever printed: only cookie *names* are
counted, URLs are shown without query values, and API keys are only reported as "set" / "not set".
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit

import requests

from odds_scanner.errors import BlockedError, ProviderError
from odds_scanner.providers.sk.http import looks_like_challenge

log = logging.getLogger(__name__)

USER_AGENT = "odds-scanner-diagnostics/1.0 (read-only check of public odds endpoints)"
MIN_INTERVAL = 1.5  # seconds between requests to a site
TIMEOUT = 15.0

# Result of one request.
OK_DATA = "OK_DATA"  # 200, JSON, contains odds-like numbers
OK_NO_ODDS = "OK_NO_ODDS"  # 200, JSON, but no odds-like numbers
HTML_PAGE = "HTML_PAGE"  # 200 but not JSON
BLOCKED = "BLOCKED"  # 403 / captcha / bot-check / geo page  -> STOP
AUTH_REQUIRED = "AUTH_REQUIRED"  # 401
RATE_LIMITED = "RATE_LIMITED"  # 429 -> STOP
NEEDS_PARAMS = "NEEDS_PARAMS"  # 400 / 422: reachable, request shape unknown
NOT_FOUND = "NOT_FOUND"  # 404
BAD_METHOD = "BAD_METHOD"  # 405
SERVER_ERROR = "SERVER_ERROR"  # 5xx
UNREACHABLE = "UNREACHABLE"  # no HTTP answer at all (network, DNS, proxy): says nothing about the site
SKIPPED = "SKIPPED"

STOP_VERDICTS = frozenset({BLOCKED, RATE_LIMITED})

ODDS_KEY_PARTS = ("odd", "price", "coef", "kurz", "rate")
EVENT_KEYS = frozenset({"hometeam", "awayteam", "home", "away", "participants", "namefull", "matchname", "teams", "competitors"})
GEO_PATH_HINTS = ("blocked", "not-available", "unavailable", "geo", "restricted", "country")
LOGIN_PATH_HINTS = ("login", "prihlas", "signin", "sign-in")


# ---------------------------------------------------------------------- data
@dataclass(frozen=True)
class Endpoint:
    label: str
    method: str  # GET / POST
    path: str  # may contain {competition_id} / {match_id}
    params: tuple[tuple[str, str], ...] = ()
    body: Any = None
    needs: str | None = None  # "competition_id" | "match_id": an id found by an earlier probe


@dataclass(frozen=True)
class SiteSpec:
    key: str
    title: str
    base: str
    endpoints: tuple[Endpoint, ...]


def _lead_endpoints() -> tuple[Endpoint, ...]:
    """Endpoint shapes from the older open-source client ``stepankarlovec/tipsport`` (tipsport.cz).

    Leads only: they may be gone or different on tipsport.sk / chance.sk. The documented flow is an
    ordinary anonymous visit - GET the home page, the site hands out its own session cookie, then the
    REST calls follow with it (a ``requests`` session does exactly this by itself).
    """
    return (
        Endpoint("sports", "GET", "/rest/offer/v4/sports"),
        Endpoint("top competitions", "GET", "/rest/offer/v1/competitions/top"),
        Endpoint("offer", "POST", "/rest/offer/v2/offer", params=(("limit", "75"),),
                 body={"results": False, "highlightAnyTime": False, "limit": 75, "fulltexts": [], "matchIds": [], "matchViewFilters": []}),
        Endpoint("search", "GET", "/rest/offer/v2/search",
                 params=(("searchText", "slovan"), ("includePrematch", "true"), ("includeResults", "false"))),
        Endpoint("competition matches", "GET", "/rest/offer/v3/sports/COMPETITION/{competition_id}/matches",
                 params=(("fromResults", "false"),), needs="competition_id"),
        Endpoint("match community stats", "GET", "/rest/offer/v3/matches/{match_id}/communityStats",
                 params=(("withOpportunitiesStats", "true"), ("withAnalysesStats", "false"), ("withTicketsStats", "false"),
                         ("withMatchForumData", "false"), ("withMilestones", "false"), ("fromResults", "false")),
                 needs="match_id"),
    )


SITES: dict[str, SiteSpec] = {
    "tipsport": SiteSpec("tipsport", "Tipsport SK", "https://www.tipsport.sk", _lead_endpoints()),
    "chance": SiteSpec("chance", "Chance SK", "https://www.chance.sk", _lead_endpoints()),
}


@dataclass
class ProbeResult:
    label: str
    method: str
    url: str  # scheme + host + path + parameter NAMES only
    verdict: str
    status: int | None = None
    content_type: str = ""
    size: int = 0
    redirects: list[str] = field(default_factory=list)  # "301 www.x.sk/a -> www.y.sk/b" (hosts and paths only)
    odds_values: int = 0
    event_like: int = 0
    note: str = ""
    data: Any = None  # parsed JSON, kept in memory only (never printed)


@dataclass
class SiteReport:
    site: SiteSpec
    homepage: ProbeResult | None = None
    cookie_names_set: int = 0  # how many cookies the site set on an anonymous visit (names/values never shown)
    probes: list[ProbeResult] = field(default_factory=list)
    cookies_required: bool | None = None  # None = could not be determined
    stopped: str = ""  # why the probe ended early ("" = ran to the end)
    verdict: str = "INCONCLUSIVE"
    requests_sent: int = 0


@dataclass
class BookmakerReport:
    """The one report format every diagnostic produces."""

    bookmaker: str
    source: str
    status: str
    http_status: str = "n/a"
    events: str = "n/a"
    markets: str = "n/a"
    odds: str = "n/a"
    oldest: str = "n/a"
    newest: str = "n/a"
    error: str = ""
    details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- JSON inspection
def analyse_json(data: Any, *, node_limit: int = 200_000) -> tuple[int, int]:
    """Return (odds-like numbers, event-like objects) found in parsed JSON. Content is never printed."""
    odds = events = seen = 0
    stack = [data]
    while stack and seen < node_limit:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            if EVENT_KEYS & {str(k).lower() for k in node}:
                events += 1
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    stack.append(value)
                elif (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and 1.01 <= value <= 1000
                    and any(part in str(key).lower() for part in ODDS_KEY_PARTS)
                ):
                    odds += 1
        elif isinstance(node, list):
            stack.extend(x for x in node if isinstance(x, (dict, list)))
    return odds, events


def find_id(data: Any, *, where: Callable[[dict], bool], limit: int = 50_000) -> Any:
    """First integer ``id`` of an object for which ``where(obj)`` is true (breadth-first)."""
    stack: list[Any] = [data]
    seen = 0
    while stack and seen < limit:
        node = stack.pop(0)
        seen += 1
        if isinstance(node, dict):
            node_id = node.get("id")
            if isinstance(node_id, int) and not isinstance(node_id, bool) and where(node):
                return node_id
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(x for x in node if isinstance(x, (dict, list)))
    return None


def _is_competition(node: dict) -> bool:
    return str(node.get("type", "")).upper() == "COMPETITION"


def _is_match(node: dict) -> bool:
    return bool(EVENT_KEYS & {str(k).lower() for k in node}) or str(node.get("type", "")).upper() == "MATCH"


# ---------------------------------------------------------------------- one request
def _clean_url(url: str, params: Iterable[tuple[str, str]] = ()) -> str:
    parts = urlsplit(url)
    names = "&".join(f"{k}=..." for k, _ in params)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" + (f"?{names}" if names else "")


def _redirect_chain(response: Any) -> list[str]:
    chain = []
    for hop in getattr(response, "history", None) or []:
        a, b = urlsplit(str(hop.url)), urlsplit(str(hop.headers.get("Location", "")))
        chain.append(f"{hop.status_code} {a.netloc}{a.path} -> {b.netloc or a.netloc}{b.path}")
    return chain


def _is_geo_or_login(response: Any) -> str:
    final = urlsplit(str(getattr(response, "url", ""))).path.lower()
    if any(h in final for h in GEO_PATH_HINTS):
        return "geo"
    if any(h in final for h in LOGIN_PATH_HINTS):
        return "login"
    return ""


def classify(response: Any, label: str, method: str, url: str) -> ProbeResult:
    """Turn an HTTP answer into a verdict. Never raises, never exposes headers/cookies/bodies."""
    status = response.status_code
    ctype = str(response.headers.get("Content-Type", "")).split(";")[0].strip().lower()
    body = getattr(response, "content", b"") or b""
    result = ProbeResult(label, method, url, UNREACHABLE, status=status, content_type=ctype, size=len(body),
                         redirects=_redirect_chain(response))
    is_json = "json" in ctype
    text = ""
    if not is_json:
        try:
            text = response.text if isinstance(response.text, str) else ""
        except Exception:  # noqa: BLE001
            text = ""

    redirect_kind = _is_geo_or_login(response)
    if status == 403 or (status < 300 and not is_json and looks_like_challenge(text)):
        result.verdict, result.note = BLOCKED, "403 / captcha or bot-check page: access is refused to automated clients"
    elif redirect_kind == "geo":
        result.verdict, result.note = BLOCKED, "redirected to a geo-restriction / unavailable page"
    elif status == 401 or redirect_kind == "login":
        result.verdict, result.note = AUTH_REQUIRED, "requires login"
    elif status == 429:
        result.verdict, result.note = RATE_LIMITED, "HTTP 429"
    elif status in (400, 422):
        result.verdict, result.note = NEEDS_PARAMS, f"HTTP {status}: reachable, but the request shape is not known"
    elif status == 404:
        result.verdict, result.note = NOT_FOUND, "no such endpoint"
    elif status == 405:
        result.verdict, result.note = BAD_METHOD, "method not allowed"
    elif status >= 500:
        result.verdict, result.note = SERVER_ERROR, f"HTTP {status}"
    elif 200 <= status < 300:
        if is_json:
            try:
                result.data = response.json()
            except ValueError:
                result.verdict, result.note = HTML_PAGE, "claims JSON but is not valid JSON"
                return result
            result.odds_values, result.event_like = analyse_json(result.data)
            result.verdict = OK_DATA if result.odds_values >= 2 else OK_NO_ODDS
            result.note = f"{result.odds_values} odds-like values, {result.event_like} event-like objects"
        else:
            result.verdict, result.note = HTML_PAGE, "not JSON (a web page)"
    else:
        result.verdict, result.note = NOT_FOUND, f"HTTP {status}"
    return result


def send(session: Any, ep: Endpoint, base: str, *, path_vars: dict[str, Any] | None = None) -> ProbeResult:
    path = ep.path.format(**(path_vars or {})) if "{" in ep.path else ep.path
    url = base + path
    shown = _clean_url(url, ep.params)
    try:
        if ep.method == "GET":
            response = session.get(url, params=dict(ep.params) or None, timeout=TIMEOUT)
        else:
            response = session.post(url, params=dict(ep.params) or None, json=ep.body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        # Type only: messages can embed URLs. A proxy/DNS/TLS failure is NOT a statement about the site.
        return ProbeResult(ep.label, ep.method, shown, UNREACHABLE, note=f"no HTTP answer ({type(exc).__name__})")
    return classify(response, ep.label, ep.method, shown)


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.8, */*;q=0.5"})
    return session


# ---------------------------------------------------------------------- site probe
def diagnose_site(
    site: SiteSpec,
    *,
    session_factory: Callable[[], Any] = new_session,
    sleep: Callable[[float], None] = time.sleep,
    max_requests: int = 10,
) -> SiteReport:
    report = SiteReport(site)
    session = session_factory()
    ids: dict[str, Any] = {}

    def pace() -> None:
        sleep(MIN_INTERVAL)

    # 1. the public home page, as any browser visit
    report.homepage = send(session, Endpoint("home page", "GET", "/"), site.base)
    report.requests_sent += 1
    report.cookie_names_set = len(getattr(session, "cookies", None) or [])
    if report.homepage.verdict in STOP_VERDICTS | {UNREACHABLE}:
        report.stopped = "home page: " + report.homepage.verdict
        report.verdict = BLOCKED if report.homepage.verdict in STOP_VERDICTS else "INCONCLUSIVE"
        return report

    # 2. the lead endpoints, same session, no header or cookie tricks
    for ep in site.endpoints:
        if report.requests_sent >= max_requests:
            report.stopped = "request budget reached"
            break
        if ep.needs and ids.get(ep.needs) is None:
            report.probes.append(ProbeResult(ep.label, ep.method, _clean_url(site.base + ep.path), SKIPPED,
                                             note=f"no {ep.needs.replace('_', ' ')} found by an earlier probe"))
            continue
        pace()
        result = send(session, ep, site.base, path_vars={ep.needs: ids[ep.needs]} if ep.needs else None)
        report.requests_sent += 1
        report.probes.append(result)
        if result.verdict in STOP_VERDICTS:
            report.stopped = f"{ep.label}: {result.verdict} - not retried, no workaround attempted"
            break
        if result.verdict in (OK_DATA, OK_NO_ODDS) and result.data is not None:
            if ep.label in ("sports", "top competitions") and ids.get("competition_id") is None:
                ids["competition_id"] = find_id(result.data, where=_is_competition)
            if ep.label in ("offer", "search", "competition matches") and ids.get("match_id") is None:
                ids["match_id"] = find_id(result.data, where=_is_match)

    # 3. do anonymous probes also work with no cookies at all? (only checked after a success)
    first_ok = next((p for p in report.probes if p.verdict == OK_DATA), None)
    if first_ok is not None and not report.stopped and report.requests_sent < max_requests:
        ep = next(e for e in site.endpoints if e.label == first_ok.label)
        pace()
        bare = send(session_factory(), ep, site.base, path_vars={ep.needs: ids[ep.needs]} if ep.needs else None)
        report.requests_sent += 1
        report.cookies_required = bare.verdict != OK_DATA
        if bare.verdict in STOP_VERDICTS:
            report.stopped = f"cookie-less {ep.label}: {bare.verdict}"

    report.verdict = _overall(report)
    return report


def _overall(report: SiteReport) -> str:
    results = [r for r in [report.homepage, *report.probes] if r is not None]
    verdicts = {r.verdict for r in results}
    if OK_DATA in verdicts:
        return "PARTIAL" if verdicts & STOP_VERDICTS else "WORKING"
    if verdicts & STOP_VERDICTS:
        return "BLOCKED"
    if verdicts <= {UNREACHABLE, SKIPPED}:
        return "INCONCLUSIVE"
    if AUTH_REQUIRED in verdicts:
        return "AUTH_REQUIRED"
    return "NO_USABLE_ENDPOINT"


def site_to_report(report: SiteReport) -> BookmakerReport:
    site = report.site
    best = max((p for p in report.probes if p.verdict == OK_DATA), key=lambda p: p.odds_values, default=None)
    statuses = [str(p.status) for p in [report.homepage, *report.probes] if p and p.status]
    out = BookmakerReport(
        bookmaker=site.title,
        source=f"{site.base} public offer endpoints (probe only - no adapter yet)",
        status=report.verdict,
        http_status=", ".join(dict.fromkeys(statuses)) or "none (no HTTP answer)",
        events=str(best.event_like) if best else "0",
        markets="n/a (no adapter yet)",
        odds=str(best.odds_values) if best else "0",
    )
    if report.verdict == "INCONCLUSIVE":
        out.error = "could not reach the site from this machine/network - this says nothing about the bookmaker"
    elif report.verdict == "BLOCKED":
        out.error = report.stopped or "access refused to automated clients; no workaround attempted"
    out.details = _site_details(report)
    return out


def _site_details(report: SiteReport) -> list[str]:
    lines = [f"requests sent: {report.requests_sent}   (honest User-Agent, >= {MIN_INTERVAL}s apart)"]
    for p in [report.homepage, *report.probes]:
        if p is None:
            continue
        status = p.status if p.status else "-"
        lines.append(f"  {p.method:4} {p.url}")
        lines.append(f"       -> {p.verdict:13} HTTP {status}  {p.content_type or '-'}  {p.size} bytes  {p.note}".rstrip())
        for hop in p.redirects:
            lines.append(f"       redirect: {hop}")
    if report.homepage and report.homepage.status:
        lines.append(f"cookies set on an anonymous home-page visit: {report.cookie_names_set} (names/values not shown)")
    if report.cookies_required is not None:
        lines.append(f"cookies required for the working endpoint: {'yes' if report.cookies_required else 'no'}")
    requires_auth = any(p.verdict == AUTH_REQUIRED for p in report.probes)
    lines.append(f"authentication required: {'yes' if requires_auth else 'not observed'}")
    challenged = any(p is not None and p.verdict == BLOCKED for p in [report.homepage, *report.probes])
    lines.append(f"security challenge returned: {'YES - probe stopped, no workaround attempted' if challenged else 'no'}")
    if report.stopped:
        lines.append(f"stopped early: {report.stopped}")
    return lines


MAX_SAVE_BYTES = 8_000_000


def save_responses(report: SiteReport, directory: str | os.PathLike[str]) -> list[str]:
    """Write the JSON bodies of the successful probes to ``directory`` (for writing a parser offline).

    Only the public response *bodies* are saved: never headers, cookies, request data or URLs with
    query values. Returns human-readable lines about what was (not) saved.
    """
    from pathlib import Path
    import re

    target = Path(directory)
    lines: list[str] = []
    for probe in report.probes:
        if probe.verdict not in (OK_DATA, OK_NO_ODDS) or probe.data is None:
            continue
        name = f"{report.site.key}_{re.sub(r'[^a-z0-9]+', '_', probe.label.lower()).strip('_')}.json"
        text = json.dumps(probe.data, ensure_ascii=False)
        if len(text.encode()) > MAX_SAVE_BYTES:
            lines.append(f"not saved (over {MAX_SAVE_BYTES // 1_000_000} MB): {name}")
            continue
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(text, encoding="utf-8")
        lines.append(f"saved {name} ({len(text)} characters)")
    if not lines:
        lines.append("nothing saved: no probe returned JSON data")
    else:
        lines.append("saved files hold public response bodies only - no cookies, headers or credentials")
    return lines


# ---------------------------------------------------------------------- provider check (the 5 working ones)
def check_provider(name: str, *, provider: Any = None, sport: str = "football") -> BookmakerReport:
    from odds_scanner.providers.sk import SK_PROVIDERS

    cls = SK_PROVIDERS[name]
    out = BookmakerReport(cls.title, f"{cls.homepage} public JSON endpoint (existing adapter, sport={sport})", "ERROR")
    try:
        provider = provider or cls()
        result = provider.fetch_odds(sport)
    except BlockedError as exc:
        out.status, out.http_status, out.error = "BLOCKED", "403 / bot-check", str(exc)
        return out
    except ProviderError as exc:
        text = str(exc)
        out.status = "UNREACHABLE" if "network error" in text else "ERROR"
        out.error = text
        return out
    except Exception as exc:  # noqa: BLE001 - a diagnostic must report, not crash
        out.error = f"unexpected {type(exc).__name__}"
        return out

    stamps = [m.last_update for e in result.events for b in e.bookmakers for m in b.markets if m.last_update]
    out.status, out.http_status = "OK", "200"
    out.events = str(len(result.events))
    out.markets = str(sum(len(b.markets) for e in result.events for b in e.bookmakers))
    out.odds = str(sum(len(m.outcomes) for e in result.events for b in e.bookmakers for m in b.markets))
    if stamps:
        out.oldest, out.newest = min(stamps).isoformat(timespec="seconds"), max(stamps).isoformat(timespec="seconds")
    if not result.events:
        out.status, out.error = "EMPTY", "the endpoint answered but no usable events were parsed"
    return out


def check_unimplemented(name: str) -> BookmakerReport:
    title = {"roobet": "Roobet", "stake": "Stake"}[name]
    return BookmakerReport(
        title, "no legitimate source selected yet", "NOT_IMPLEMENTED",
        error="no adapter: see docs/ODDS_SOURCES.md for what is known and what is needed (an API key via environment variable)",
    )


def check_odds_api() -> BookmakerReport:
    key_set = bool(os.environ.get("ODDS_API_KEY"))
    return BookmakerReport(
        "The Odds API", "https://api.the-odds-api.com/v4 (existing provider, off by default)",
        "CONFIGURED" if key_set else "NO_KEY",
        error="" if key_set else "ODDS_API_KEY is not set (not checked live: every request costs quota)",
    )


# ---------------------------------------------------------------------- rendering / entry point
AGGREGATORS = ("oddspapi", "sportmonks", "oddsapiio")  # key-based coverage checks; only run when named, never by "all"

REPORT_FIELDS = (
    ("BOOKMAKER", "bookmaker"), ("SOURCE", "source"), ("STATUS", "status"), ("HTTP/API STATUS", "http_status"),
    ("EVENT COUNT", "events"), ("MARKET COUNT", "markets"), ("ODDS COUNT", "odds"),
    ("OLDEST ODDS", "oldest"), ("NEWEST ODDS", "newest"), ("ERROR", "error"),
)


def render(report: BookmakerReport) -> str:
    lines = [f"{label:<16}{getattr(report, attr) or '-'}" for label, attr in REPORT_FIELDS]
    if report.details:
        lines += ["", "DETAILS", *report.details]
    return "\n".join(lines)


def available_targets() -> list[str]:
    from odds_scanner.providers.sk import SK_PROVIDERS

    return [*SITES, "roobet", "stake", *SK_PROVIDERS, "the_odds_api", *AGGREGATORS]


def run(target: str, *, out: Callable[[str], None] = print, session_factory: Callable[[], Any] = new_session,
        sleep: Callable[[float], None] = time.sleep, save_dir: str | None = None) -> int:
    """Run one diagnostic (or ``all``). Exit code 0 = reachable and usable, 1 = not usable, 2 = unknown target."""
    from odds_scanner.providers.sk import SK_PROVIDERS

    targets = [t for t in available_targets() if t not in AGGREGATORS] if target == "all" else [target]
    code = 0
    for i, name in enumerate(targets):
        if name in SITES:
            site_report = diagnose_site(SITES[name], session_factory=session_factory, sleep=sleep)
            report = site_to_report(site_report)
            if save_dir:
                report.details += save_responses(site_report, save_dir)
        elif name in SK_PROVIDERS:
            report = check_provider(name)
        elif name in ("roobet", "stake"):
            report = check_unimplemented(name)
        elif name == "the_odds_api":
            report = check_odds_api()
        elif name in AGGREGATORS:
            from odds_scanner import coverage

            report = (coverage.check_oddspapi(save_dir=save_dir) if name == "oddspapi"
                      else coverage.check_oddsapiio() if name == "oddsapiio" else coverage.check_sportmonks())
        else:
            out(f"unknown bookmaker {name!r}; choose one of: all, {', '.join(available_targets())}")
            return 2
        if i:
            out("")
            out("-" * 60)
        out(render(report))
        if report.status not in ("OK", "WORKING", "PARTIAL", "CONFIGURED"):
            code = 1
    return code


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m odds_scanner.diagnostics", description=__doc__.split("\n\n")[0])
    ap.add_argument("target", help="all | " + " | ".join(available_targets()))
    ap.add_argument("--save", metavar="DIR", help="save public JSON samples to DIR (Tipsport/Chance probes; for oddspapi: one real fixture's odds, costs 2 requests)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    return run(args.target, save_dir=args.save)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

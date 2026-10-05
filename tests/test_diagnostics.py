"""Diagnostics: classification, stop-at-first-block, no secrets, proxy failure != site verdict."""

import json

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from odds_scanner import diagnostics as d
from odds_scanner.errors import BlockedError, ProviderError
from odds_scanner.models import BookmakerOdds, Event, MarketOdds, Outcome
from odds_scanner.providers.base import FetchResult
from tests.conftest import NOW


class Resp:
    def __init__(self, status=200, body=None, ctype="application/json", text=None, url="https://www.tipsport.sk/x", history=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict({"Content-Type": ctype})
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.content = self.text.encode()
        self.url = url
        self.history = history or []

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Session:
    """Scripted by URL path; records every request. Cookies are a plain list (len() only is used)."""

    def __init__(self, routes=None, cookies=0):
        self.routes = routes or {}
        self.calls = []
        self.cookies = [f"SECRETCOOKIE{i}" for i in range(cookies)]
        self.headers = {}

    def _answer(self, method, url, params):
        self.calls.append((method, url, dict(params or {})))
        path = url.split(".sk", 1)[1]
        item = self.routes.get(path, Resp(404, text="nope", ctype="text/html"))
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, params=None, timeout=None):
        return self._answer("GET", url, params)

    def post(self, url, params=None, json=None, timeout=None):
        return self._answer("POST", url, params)


ODDS_PAYLOAD = {"matches": [{"id": 77, "nameFull": "A - B", "participants": ["A", "B"], "odds": [{"rate": 1.9}, {"rate": 2.1}, {"rate": 3.4}]}]}
SPORTS_PAYLOAD = {"sports": [{"name": "Football", "competitions": [{"id": 4242, "name": "Liga"}]}]}


def site(routes, **kw):
    sessions = []

    def factory():
        s = Session(routes, **kw)
        sessions.append(s)
        return s

    sleeps = []
    report = d.diagnose_site(d.SITES["tipsport"], session_factory=factory, sleep=sleeps.append)
    return report, sessions, sleeps


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize(
    "resp, verdict",
    [
        (Resp(403, text="Forbidden", ctype="text/html"), d.BLOCKED),
        (Resp(200, text="<html>Just a moment... cf-chl</html>", ctype="text/html"), d.BLOCKED),
        (Resp(200, text="<div class='g-recaptcha captcha'>", ctype="text/html"), d.BLOCKED),
        (Resp(401, text="login", ctype="text/html"), d.AUTH_REQUIRED),
        (Resp(429, text="slow down", ctype="text/html"), d.RATE_LIMITED),
        (Resp(400, body={"error": "bad"}), d.NEEDS_PARAMS),
        (Resp(422, body={"error": "bad"}), d.NEEDS_PARAMS),
        (Resp(404, text="x", ctype="text/html"), d.NOT_FOUND),
        (Resp(405, text="x", ctype="text/html"), d.BAD_METHOD),
        (Resp(503, text="x", ctype="text/html"), d.SERVER_ERROR),
        (Resp(200, body=ODDS_PAYLOAD), d.OK_DATA),
        (Resp(200, body={"sports": [{"name": "x"}]}), d.OK_NO_ODDS),
        (Resp(200, text="<html>welcome</html>", ctype="text/html"), d.HTML_PAGE),
        (Resp(200, text="{broken", ctype="application/json"), d.HTML_PAGE),
        (Resp(200, text="ok", ctype="text/html", url="https://www.tipsport.sk/geo-blocked"), d.BLOCKED),
        (Resp(200, text="ok", ctype="text/html", url="https://www.tipsport.sk/prihlasenie"), d.AUTH_REQUIRED),
    ],
)
def test_classify(resp, verdict):
    assert d.classify(resp, "t", "GET", "https://www.tipsport.sk/x").verdict == verdict


def test_json_that_mentions_captcha_is_data_not_a_block():
    body = {"note": "captcha", "odds": [{"rate": 1.5}, {"rate": 2.5}]}
    assert d.classify(Resp(200, body=body), "t", "GET", "u").verdict == d.OK_DATA


def test_redirect_chain_shows_hosts_and_paths_only_never_queries():
    hop = Resp(301, text="", ctype="text/html", url="https://www.tipsport.sk/a?token=SECRET")
    hop.headers["Location"] = "https://www.tipsport.cz/b?token=SECRET"
    result = d.classify(Resp(200, body=ODDS_PAYLOAD, history=[hop]), "t", "GET", "u")
    assert result.redirects == ["301 www.tipsport.sk/a -> www.tipsport.cz/b"]
    assert "SECRET" not in repr(result.redirects)


def test_url_shown_without_query_values():
    assert d._clean_url("https://www.tipsport.sk/rest/x?key=SECRET", (("limit", "75"),)) == "https://www.tipsport.sk/rest/x?limit=..."


def test_json_analysis_and_id_discovery():
    assert d.analyse_json(ODDS_PAYLOAD) == (3, 1)
    assert d.analyse_json({"odds": [True, 0.5, "1.9"], "price": 5000}) == (0, 0)  # bool, <1, string, >1000 ignored
    assert d.find_id(SPORTS_PAYLOAD, under=("competition",)) == 4242
    assert d.find_id(SPORTS_PAYLOAD, under=("nothing",)) is None
    assert d.find_id(ODDS_PAYLOAD, needs_keys=frozenset({"namefull"})) == 77


# ------------------------------------------------------------------ the probe: stop rules
def test_unreachable_site_is_inconclusive_and_sends_one_request_only():
    report, sessions, _ = site({"/": requests.exceptions.ProxyError("tunnel failed: https://x?apiKey=SECRET")})
    assert report.verdict == "INCONCLUSIVE"
    assert report.requests_sent == 1 and len(sessions[0].calls) == 1
    assert "SECRET" not in d.render(d.site_to_report(report))
    assert "says nothing about the bookmaker" in d.site_to_report(report).error


def test_home_page_403_stops_everything_and_is_never_retried():
    report, sessions, sleeps = site({"/": Resp(403, text="blocked", ctype="text/html")})
    assert report.verdict == "BLOCKED" and report.requests_sent == 1
    assert sum(len(s.calls) for s in sessions) == 1  # no second attempt, no other endpoint, no fresh session
    assert report.stopped.startswith("home page")


def test_challenge_page_on_an_endpoint_stops_the_rest():
    routes = {"/": Resp(200, text="<html>home</html>", ctype="text/html"),
              "/rest/offer/v4/sports": Resp(200, text="<html>Just a moment... cf-chl</html>", ctype="text/html")}
    report, sessions, _ = site(routes)
    assert report.verdict == "BLOCKED"
    assert [c[1].split(".sk")[1] for c in sessions[0].calls] == ["/", "/rest/offer/v4/sports"]  # stopped right there
    assert "not retried, no workaround attempted" in report.stopped
    assert "YES - probe stopped" in "\n".join(d.site_to_report(report).details)


def test_429_stops_too():
    routes = {"/": Resp(200, text="home", ctype="text/html"), "/rest/offer/v4/sports": Resp(429, text="slow", ctype="text/html")}
    report, sessions, _ = site(routes)
    assert report.verdict == "BLOCKED" and len(sessions[0].calls) == 2


def test_working_site_discovers_ids_and_checks_cookie_requirement():
    routes = {
        "/": Resp(200, text="home", ctype="text/html"),
        "/rest/offer/v4/sports": Resp(200, body=SPORTS_PAYLOAD),
        "/rest/offer/v2/offer": Resp(200, body=ODDS_PAYLOAD),
        "/rest/offer/v2/search": Resp(400, body={"e": 1}),
        "/rest/offer/v3/sports/COMPETITION/4242/matches": Resp(200, body=ODDS_PAYLOAD),
        "/rest/offer/v3/matches/77/communityStats": Resp(404, text="x", ctype="text/html"),
    }
    report, sessions, sleeps = site(routes, cookies=2)
    paths = [c[1].split(".sk")[1] for c in sessions[0].calls]
    assert "/rest/offer/v3/sports/COMPETITION/4242/matches" in paths  # id found by the sports probe
    assert "/rest/offer/v3/matches/77/communityStats" in paths  # id found by the offer probe
    assert report.verdict == "WORKING" and report.cookies_required is False  # cookie-less repeat also worked
    assert len(sessions) == 2 and not sessions[1].calls[0][1].endswith("/")  # the bare request skipped the home page
    assert report.cookie_names_set == 2
    assert all(s >= d.MIN_INTERVAL for s in sleeps)  # polite spacing before every later request


def test_cookies_required_detected():
    class Picky(Session):
        def _answer(self, method, url, params):
            if not self.cookies and url.endswith("/offer"):
                return Resp(401, text="x", ctype="text/html")
            return super()._answer(method, url, params)

    routes = {"/": Resp(200, text="home", ctype="text/html"), "/rest/offer/v2/offer": Resp(200, body=ODDS_PAYLOAD)}
    made = []

    def factory():
        s = Picky(routes, cookies=0 if made else 1)
        made.append(s)
        return s

    report = d.diagnose_site(d.SITES["chance"], session_factory=factory, sleep=lambda s: None)
    assert report.cookies_required is True


def test_missing_ids_skip_dependent_probes_without_requests():
    routes = {"/": Resp(200, text="home", ctype="text/html"), "/rest/offer/v4/sports": Resp(200, body={"sports": []})}
    report, sessions, _ = site(routes)
    skipped = [p for p in report.probes if p.verdict == d.SKIPPED]
    assert {p.label for p in skipped} == {"competition matches", "match community stats"}
    assert all("v3" not in c[1] for c in sessions[0].calls)


def test_request_budget_is_respected():
    routes = {"/": Resp(200, text="home", ctype="text/html"), "/rest/offer/v4/sports": Resp(200, body=SPORTS_PAYLOAD)}
    report = d.diagnose_site(d.SITES["tipsport"], session_factory=lambda: Session(routes), sleep=lambda s: None, max_requests=3)
    assert report.requests_sent <= 3 and report.stopped == "request budget reached"


def test_auth_required_and_no_usable_endpoint_verdicts():
    routes = {"/": Resp(200, text="home", ctype="text/html"), "/rest/offer/v4/sports": Resp(401, text="x", ctype="text/html")}
    assert site(routes)[0].verdict == "AUTH_REQUIRED"
    assert site({"/": Resp(200, text="home", ctype="text/html")})[0].verdict == "NO_USABLE_ENDPOINT"


# ------------------------------------------------------------------ no secrets, honest identity
def test_report_never_contains_cookie_values_or_query_values():
    routes = {"/": Resp(200, text="home", ctype="text/html"), "/rest/offer/v2/offer": Resp(200, body=ODDS_PAYLOAD)}
    report, _, _ = site(routes, cookies=3)
    text = d.render(d.site_to_report(report))
    assert "SECRETCOOKIE" not in text
    assert "cookies set on an anonymous home-page visit: 3 (names/values not shown)" in text
    assert "fulltext=..." in text and "slovan" not in text


def test_session_sends_an_honest_user_agent():
    ua = d.new_session().headers["User-Agent"]
    assert ua.startswith("odds-scanner-diagnostics/") and "Mozilla" not in ua


# ------------------------------------------------------------------ provider checks (existing adapters)
def fake_provider(events=None, error=None):
    class P:
        def fetch_odds(self, sport, **kw):
            if error:
                raise error
            return FetchResult(events=events or [])

    return P()


def an_event():
    m = MarketOdds("h2h_3_way", (Outcome("1", 2.0), Outcome("X", 3.0), Outcome("2", 4.0)), NOW)
    return Event("e", "football", "Football", NOW, "A", "B", (BookmakerOdds("nike", "Niké", (m,), NOW),))


def test_provider_check_counts_everything():
    r = d.check_provider("nike", provider=fake_provider([an_event()]))
    assert (r.status, r.http_status, r.events, r.markets, r.odds) == ("OK", "200", "1", "1", "3")
    assert r.oldest == r.newest == NOW.isoformat(timespec="seconds")


@pytest.mark.parametrize(
    "error, status",
    [(BlockedError("Niké: HTTP 403 - access refused"), "BLOCKED"), (ProviderError("Niké: network error: ProxyError"), "UNREACHABLE"),
     (ProviderError("Niké: HTTP 500"), "ERROR"), (RuntimeError("secret detail"), "ERROR")],
)
def test_provider_check_errors(error, status):
    r = d.check_provider("nike", provider=fake_provider(error=error))
    assert r.status == status and "secret detail" not in r.error


def test_provider_check_reports_empty_answers():
    assert d.check_provider("nike", provider=fake_provider([])).status == "EMPTY"


# ------------------------------------------------------------------ command
def test_run_unknown_and_unimplemented_targets():
    lines = []
    assert d.run("nope", out=lines.append) == 2 and "choose one of" in lines[0]
    lines.clear()
    assert d.run("roobet", out=lines.append) == 1
    text = "\n".join(lines)
    assert "NOT_IMPLEMENTED" in text and "ODDS_SOURCES.md" in text


def test_report_has_every_requested_field_in_order():
    lines = []
    d.run("stake", out=lines.append)
    labels = [ln[:16].strip() for ln in "\n".join(lines).splitlines()[:10]]
    assert labels == ["BOOKMAKER", "SOURCE", "STATUS", "HTTP/API STATUS", "EVENT COUNT", "MARKET COUNT",
                      "ODDS COUNT", "OLDEST ODDS", "NEWEST ODDS", "ERROR"]


def test_odds_api_check_never_prints_the_key(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "TOPSECRETKEY")
    r = d.check_odds_api()
    assert r.status == "CONFIGURED" and "TOPSECRETKEY" not in d.render(r)


def test_run_site_target_end_to_end_offline():
    lines = []
    code = d.run("chance", out=lines.append, session_factory=lambda: Session({"/": Resp(403, text="x", ctype="text/html")}), sleep=lambda s: None)
    text = "\n".join(lines)
    assert code == 1 and "BLOCKED" in text and "Chance SK" in text and "no workaround attempted" in text


def test_cli_command(capsys):
    from odds_scanner import cli

    assert cli.main(["diagnostics"]) == 2 and "usage: diagnostics" in capsys.readouterr().out
    assert cli.main(["diagnostics", "roobet"]) == 1 and "NOT_IMPLEMENTED" in capsys.readouterr().out
    assert cli.main(["diagnostics", "nonsense"]) == 2

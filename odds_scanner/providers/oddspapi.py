"""OddsPapi (https://oddspapi.io): Stake, Roobet, MyStake and other international books in one request.

Licensed aggregator API, read-only, key from an environment variable (never logged: errors are built
from the HTTP status / exception type only, and the service's own message has the key removed).

How a poll works (endpoint names and fields from the vendor's public docs; NOT yet checked against a
real answer, so the parser is defensive and reports what it could not read):

* ``GET /v4/odds-by-tournaments?tournamentIds=17,8,..&bookmakers=stake,roobet,mystake`` - odds of every
  fixture of the listed leagues for up to 3 bookmakers, ONE request.
  Prices: ``bookmakerOdds[slug].markets[marketId].outcomes[outcomeId].players["0"].price``.
* If those fixtures carry no team names, ``GET /v4/fixtures?sportId=..&from=..&to=..`` once (cached for
  a few hours) to look them up.

Markets: football full-time result is market ``101`` with outcomes ``101/102/103`` = 1/X/2 (vendor docs).
Other sports are opt-in (``options.sport_ids`` / ``options.market_ids``); outcome ids are assumed to
follow the same pattern (market id, +1, +2), which is only verified for football.

The free plan has 250 requests a month, so every request is counted in ``data/oddspapi_usage.json`` and
the source stops itself at ``options.monthly_budget`` (default 240). Use a long poll interval.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests

from odds_scanner.errors import AuthenticationError, ConfigError, ProviderError, QuotaExhaustedError, RateLimitError
from odds_scanner.markets import MIN_ODDS, TWO_WAY_SPORTS
from odds_scanner.models import BookmakerOdds, Event
from odds_scanner.providers.base import FetchResult, OddsProvider
from odds_scanner.providers.sk.common import build_markets

log = logging.getLogger(__name__)

BASE_URL = "https://api.oddspapi.io/v4"
MAX_BOOKMAKERS_PER_CALL = 3  # vendor docs: the bookmakers parameter takes at most 3

DEFAULT_BOOKMAKERS = ("stake", "roobet", "mystake")
DEFAULT_SPORT_IDS = {"football": 10}
DEFAULT_MARKET_IDS = {"football": 101}
# Big-five leagues + Champions League + Europa League (Sportradar-style ids; the docs' own example uses 17,8).
DEFAULT_TOURNAMENTS = {"football": [17, 8, 23, 35, 34, 7, 679]}
DEFAULT_BUDGET = 240

TITLES = {"stake": "Stake", "roobet": "Roobet", "mystake": "MyStake", "pinnacle": "Pinnacle", "bcgame": "BC.Game",
          "1xbet": "1xBet", "cloudbet": "Cloudbet", "rollbit": "Rollbit"}
HOMEPAGES = {"stake": "https://stake.com/sports/home", "roobet": "https://roobet.com/sports",
             "mystake": "https://mystake.com", "pinnacle": "https://www.pinnacle.com", "cloudbet": "https://www.cloudbet.com"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------- request budget
class RequestBudget:
    """Requests sent this calendar month, persisted so restarts do not reset the count."""

    def __init__(self, path: str | Path, limit: int, *, today: Callable[[], date] = date.today) -> None:
        self.path, self.limit, self._today = Path(path), int(limit), today

    def _month(self) -> str:
        return self._today().strftime("%Y-%m")

    def _load(self) -> dict[str, int]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def used(self) -> int:
        return self._load().get(self._month(), 0)

    def remaining(self) -> int:
        return max(0, self.limit - self.used())

    def spend(self, n: int = 1) -> None:
        data = self._load()
        month = self._month()
        data[month] = data.get(month, 0) + n
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            log.warning("OddsPapi: could not save the request count (%s)", type(exc).__name__)


# ---------------------------------------------------------------------- parsing (pure, tested offline)
def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iter_fixtures(data: Any, *, limit: int = 100_000) -> Iterable[dict]:
    """Every dict that has a ``fixtureId`` (the answer may be a list, or wrapped in an object)."""
    stack, seen = [data], 0
    while stack and seen < limit:
        node = stack.pop(0)
        seen += 1
        if isinstance(node, dict):
            if "fixtureId" in node:
                yield node
                continue
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))


def fixture_names(data: Any) -> dict[str, tuple[str, str]]:
    """fixtureId -> (home, away) from a ``/v4/fixtures`` (or odds) answer."""
    names: dict[str, tuple[str, str]] = {}
    for fx in iter_fixtures(data):
        home, away = fx.get("participant1Name"), fx.get("participant2Name")
        if isinstance(home, str) and isinstance(away, str) and home.strip() and away.strip():
            names[str(fx["fixtureId"])] = (home.strip(), away.strip())
    return names


def outcome_codes(sport: str, market_id: int) -> dict[str, str]:
    """Outcome id -> our code. Football 101: 101/102/103 = 1/X/2 (vendor docs); others assume the same pattern."""
    codes = ("1", "2") if sport in TWO_WAY_SPORTS else ("1", "X", "2")
    return {str(market_id + i): code for i, code in enumerate(codes)}


def _price(outcome: Any) -> float | None:
    if not isinstance(outcome, dict):
        return None
    players = outcome.get("players")
    cell = players.get("0") if isinstance(players, dict) else None
    if not isinstance(cell, dict):
        return None
    if cell.get("active") is False:
        return None
    try:
        price = float(cell.get("price"))
    except (TypeError, ValueError):
        return None
    return price if price > MIN_ODDS else None


def parse_odds(
    data: Any,
    *,
    sport: str,
    market_id: int,
    fetched_at: datetime,
    names: Mapping[str, tuple[str, str]] | None = None,
    bookmakers: Sequence[str] = (),
) -> tuple[list[Event], int]:
    """Events from an odds answer. Returns (events, fixtures skipped because their team names are unknown)."""
    codes = outcome_codes(sport, market_id)
    wanted = {b.lower() for b in bookmakers}
    events: list[Event] = []
    nameless = 0
    for fx in iter_fixtures(data):
        status = fx.get("statusId")
        if status not in (None, 0, "0"):
            continue  # live or finished: not what we scan
        start = _parse_time(fx.get("startTime") or fx.get("trueStartTime"))
        if start is None:
            continue
        fid = str(fx["fixtureId"])
        home, away = fx.get("participant1Name"), fx.get("participant2Name")
        if not (isinstance(home, str) and isinstance(away, str) and home.strip() and away.strip()):
            found = (names or {}).get(fid)
            if found is None:
                nameless += 1
                continue
            home, away = found
        home, away = home.strip(), away.strip()
        books: list[BookmakerOdds] = []
        book_odds = fx.get("bookmakerOdds")
        for slug, node in (book_odds.items() if isinstance(book_odds, dict) else ()):
            slug = str(slug).lower()
            if (wanted and slug not in wanted) or not isinstance(node, dict) or node.get("bookmakerIsActive") is False:
                continue
            markets = node.get("markets")
            market = markets.get(str(market_id)) if isinstance(markets, dict) else None
            outcomes = market.get("outcomes") if isinstance(market, dict) else None
            if not isinstance(outcomes, dict):
                continue
            prices = {code: p for oid, code in codes.items() if (p := _price(outcomes.get(oid))) is not None}
            built = build_markets(sport, prices, None, fetched_at)
            if not built:
                continue
            link = node.get("fixturePath")
            url = link if isinstance(link, str) and link.startswith("https://") else HOMEPAGES.get(slug)
            books.append(BookmakerOdds(
                key=slug, title=TITLES.get(slug, slug.title()), markets=built, last_update=fetched_at,
                url=url, event_name=f"{home} - {away}", event_id=f"oddspapi:{fid}",
            ))
        if books:
            events.append(Event(
                id=f"oddspapi:{fid}", sport_key=sport, sport_title=sport.title(), commence_time=start,
                home_team=home, away_team=away, bookmakers=tuple(books),
            ))
    return events, nameless


# ---------------------------------------------------------------------- provider
class OddsPapiProvider(OddsProvider):
    name = "oddspapi"
    title = "OddsPapi"
    homepage = "https://oddspapi.io"

    def __init__(
        self,
        *,
        options: Mapping[str, Any] | None = None,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        max_retries: int = 1,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        budget: RequestBudget | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        opts = dict(options or {})
        env = os.environ if environ is None else environ
        key_env = str(opts.get("api_key_env", "ODDSPAPI_API_KEY"))
        key = env.get(key_env)
        if not key:
            raise ConfigError(f"OddsPapi is enabled but the {key_env} environment variable is not set (free key: https://oddspapi.io)")
        self._key = key
        books = [str(b).lower() for b in (opts.get("bookmakers") or DEFAULT_BOOKMAKERS)]
        if not books:
            raise ConfigError("providers.oddspapi.options.bookmakers must list at least one bookmaker")
        self.bookmakers = books
        self.sport_ids = {**DEFAULT_SPORT_IDS, **{str(k): int(v) for k, v in (opts.get("sport_ids") or {}).items()}}
        self.market_ids = {**DEFAULT_MARKET_IDS, **{str(k): int(v) for k, v in (opts.get("market_ids") or {}).items()}}
        raw_tournaments = {**DEFAULT_TOURNAMENTS, **(opts.get("tournament_ids") or {})}
        self.tournaments = {str(k): [int(t) for t in v] for k, v in raw_tournaments.items() if v}
        self.budget = budget or RequestBudget(
            opts.get("usage_file", "data/oddspapi_usage.json"), int(opts.get("monthly_budget", DEFAULT_BUDGET))
        )
        self._names_ttl = timedelta(hours=float(opts.get("names_refresh_hours", 6)))
        self._session = session or requests.Session()
        self._timeout, self._max_retries = timeout, max_retries
        self._clock, self._sleep = clock, sleep
        self._base = base_url.rstrip("/")
        self._names: dict[str, tuple[str, str]] = {}
        self._names_at: dict[str, datetime] = {}  # sport -> last fixtures lookup
        self._last_request: float | None = None
        self.homepages = {b: HOMEPAGES.get(b, "") for b in self.bookmakers}
        self.titles = {b: TITLES.get(b, b.title()) for b in self.bookmakers}

    def supports(self, sport: str) -> bool:
        return sport in self.sport_ids and sport in self.market_ids and sport in self.tournaments

    # -- polling
    def fetch_odds(self, sport: str, *, regions: Sequence[str] = (), markets: Sequence[str] = (),
                   bookmakers: Sequence[str] = ()) -> FetchResult:
        if not self.supports(sport):
            raise ProviderError(f"OddsPapi: sport {sport!r} is not configured (options.sport_ids / market_ids / tournament_ids)")
        books = [b.lower() for b in bookmakers] or self.bookmakers
        fetched_at = self._clock()
        answers = []
        for i in range(0, len(books), MAX_BOOKMAKERS_PER_CALL):
            chunk = books[i:i + MAX_BOOKMAKERS_PER_CALL]
            answers.append(self._get("odds-by-tournaments", {
                "tournamentIds": ",".join(str(t) for t in self.tournaments[sport]),
                "bookmakers": ",".join(chunk), "oddsFormat": "decimal", "verbosity": 3,
            }))
        market_id = self.market_ids[sport]
        events, nameless = self._parse_all(answers, sport, market_id, fetched_at, books)
        if nameless and self._names_due(sport, fetched_at):
            self._refresh_names(sport, fetched_at)
            events, nameless = self._parse_all(answers, sport, market_id, fetched_at, books)
        if nameless:
            log.info("OddsPapi %s: %d fixture(s) skipped, team names unknown", sport, nameless)
        if not events and not any(True for a in answers for _ in iter_fixtures(a)):
            log.warning("OddsPapi %s: no fixtures recognised in the answer (format may differ from the docs)", sport)
        log.info("OddsPapi %s: %d event(s); %d of %d monthly requests left", sport, len(events),
                 self.budget.remaining(), self.budget.limit)
        return FetchResult(events=events)

    def _parse_all(self, answers: list[Any], sport: str, market_id: int, fetched_at: datetime,
                   books: Sequence[str]) -> tuple[list[Event], int]:
        merged: dict[str, Event] = {}
        nameless = 0
        for answer in answers:
            try:
                events, missing = parse_odds(answer, sport=sport, market_id=market_id, fetched_at=fetched_at,
                                             names=self._names, bookmakers=books)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise ProviderError(f"OddsPapi: unexpected response format ({type(exc).__name__})") from exc
            nameless += missing
            for ev in events:  # one call per 3 bookmakers: join the books of the same fixture
                old = merged.get(ev.id)
                merged[ev.id] = ev if old is None else replace(old, bookmakers=old.bookmakers + ev.bookmakers)
        return list(merged.values()), nameless

    def _names_due(self, sport: str, now: datetime) -> bool:
        last = self._names_at.get(sport)
        return last is None or now - last >= self._names_ttl

    def _refresh_names(self, sport: str, now: datetime) -> None:
        self._names_at[sport] = now
        day = now.date()
        try:
            data = self._get("fixtures", {"sportId": self.sport_ids[sport], "from": day.isoformat(),
                                          "to": (day + timedelta(days=7)).isoformat(), "statusId": 0})
        except (RateLimitError, ProviderError) as exc:
            if isinstance(exc, (AuthenticationError, QuotaExhaustedError)):
                raise
            log.warning("OddsPapi: team-name lookup failed: %s", exc)
            return
        self._names.update(fixture_names(data))

    # -- HTTP
    def _get(self, path: str, params: dict[str, Any]) -> Any:
        last = "unknown error"
        for attempt in range(self._max_retries + 1):
            if self.budget.remaining() <= 0:
                raise QuotaExhaustedError(
                    f"OddsPapi monthly request budget used up ({self.budget.limit}); "
                    "raise providers.oddspapi.options.monthly_budget only if your plan allows it"
                )
            if attempt:
                self._sleep(5.0 * attempt)
            if self._last_request is not None:  # at most one request per second
                wait = 1.0 - (time.monotonic() - self._last_request)
                if wait > 0:
                    self._sleep(wait)
            self._last_request = time.monotonic()
            self.budget.spend()
            try:
                response = self._session.get(f"{self._base}/{path}", params={**params, "apiKey": self._key},
                                             timeout=self._timeout)
            except requests.RequestException as exc:  # not str(exc): it can hold the URL, which holds the key
                last = f"network error: {type(exc).__name__}"
                continue
            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError:
                    raise ProviderError(f"OddsPapi {path}: the answer was not JSON") from None
            detail = self._detail(response)
            if status in (401, 403):
                raise AuthenticationError(f"OddsPapi refused the key or this plan (HTTP {status}){detail}")
            if status == 429:
                raise RateLimitError(f"OddsPapi rate limit (HTTP 429){detail}", retry_after=self._retry_after(response))
            if status >= 500:
                last = f"server error HTTP {status}"
                continue
            raise ProviderError(f"OddsPapi {path}: HTTP {status}{detail}")
        raise ProviderError(f"OddsPapi {path}: failed after {self._max_retries + 1} attempts ({last})")

    def _detail(self, response: Any) -> str:
        try:
            text = response.text or ""
        except Exception:  # noqa: BLE001
            return ""
        text = " ".join(text.replace(self._key, "***").split())[:200]
        return f" - service says: {text}" if text else ""

    @staticmethod
    def _retry_after(response: Any) -> float | None:
        value = response.headers.get("Retry-After") if hasattr(response, "headers") else None
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None

    def close(self) -> None:
        self._session.close()


# ---------------------------------------------------------------------- setup helper
def list_tournaments(provider: OddsPapiProvider, sport: str = "football", *, out: Callable[[str], None] = print,
                     limit: int = 40) -> int:
    """Print the leagues with the most upcoming fixtures and their ids (1 request), for ``tournament_ids``."""
    sport_id = provider.sport_ids.get(sport)
    if sport_id is None:
        out(f"no OddsPapi sport id known for {sport!r} (set providers.oddspapi.options.sport_ids)")
        return 1
    data = provider._get("tournaments", {"sportId": sport_id})
    rows = [r for r in (data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else [])
            if isinstance(r, dict) and "tournamentId" in r]

    def count(r: dict) -> int:
        try:
            return int(r.get("futureFixtures") or 0) + int(r.get("upcomingFixtures") or 0)
        except (TypeError, ValueError):
            return 0

    rows.sort(key=count, reverse=True)
    configured = set(provider.tournaments.get(sport, []))
    out(f"{'ID':>8}  {'FIXTURES':>8}  LEAGUE   (* = already in your config)")
    for r in rows[:limit]:
        mark = "*" if _int(r.get("tournamentId")) in configured else " "
        name = f"{r.get('categoryName') or ''} - {r.get('tournamentName') or ''}".strip(" -")
        out(f"{r.get('tournamentId')!s:>8}{mark} {count(r):>8}  {name}")
    if not rows:
        out("no leagues recognised in the answer")
    out(f"\n{provider.budget.remaining()} of {provider.budget.limit} monthly requests left. Put the ids you want in")
    out("config.yaml -> providers: oddspapi: options: tournament_ids: {football: [..]}")
    return 0 if rows else 1


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

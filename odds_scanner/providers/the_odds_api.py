"""Live provider for The Odds API v4 (https://the-odds-api.com)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Mapping, Sequence

import requests

from odds_scanner.errors import (
    AuthenticationError,
    ConfigError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitError,
)
from odds_scanner.models import QuotaInfo
from odds_scanner.providers.base import FetchResult, OddsProvider
from odds_scanner.providers.parsing import parse_events

log = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def parse_quota(headers: Mapping[str, str]) -> QuotaInfo:
    """Read the ``x-requests-remaining/used/last`` headers (requests' headers are case-insensitive)."""
    return QuotaInfo(
        remaining=_int_header(headers, "x-requests-remaining"),
        used=_int_header(headers, "x-requests-used"),
        last_cost=_int_header(headers, "x-requests-last"),
    )


class TheOddsApiProvider(OddsProvider):
    """Fetches decimal odds from The Odds API.

    The API key is read from the environment (``api_key_env``, default ``ODDS_API_KEY``).
    The API only accepts the key as a query parameter, so every error raised here is
    built from the exception *type* and HTTP status only - never from a URL - to keep
    the key out of logs and tracebacks.
    """

    name = "the_odds_api"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        api_key_env: str = "ODDS_API_KEY",
        session: requests.Session | None = None,
        timeout: float = 15.0,
        max_retries: int = 2,
        retry_backoff: float = 2.0,
        base_url: str = BASE_URL,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = api_key or os.environ.get(api_key_env)
        if not key:
            raise ConfigError(
                f"no API key: set the {api_key_env} environment variable "
                "(get a key at https://the-odds-api.com)"
            )
        self._key = key
        self._session = session or requests.Session()
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep

    def fetch_odds(
        self,
        sport: str,
        *,
        regions: Sequence[str],
        markets: Sequence[str],
        bookmakers: Sequence[str] = (),
    ) -> FetchResult:
        params: dict[str, str] = {
            "apiKey": self._key,
            "regions": ",".join(regions),
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)

        response = self._get(f"{self._base_url}/sports/{sport}/odds", params, sport)
        quota = parse_quota(response.headers)
        log.info(
            "Odds API quota after %s: remaining=%s used=%s last_cost=%s",
            sport, quota.remaining, quota.used, quota.last_cost,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"{sport}: response was not valid JSON") from exc
        if not isinstance(payload, list):
            raise ProviderError(f"{sport}: expected a JSON list of events, got {type(payload).__name__}")
        return FetchResult(events=parse_events(payload), quota=quota)

    # ------------------------------------------------------------------ HTTP
    def _get(self, url: str, params: dict[str, str], sport: str) -> requests.Response:
        last_problem = "unknown error"
        for attempt in range(self._max_retries + 1):
            if attempt:
                delay = self._retry_backoff * 2 ** (attempt - 1)
                log.warning("retrying %s in %.1fs (%s)", sport, delay, last_problem)
                self._sleep(delay)
            try:
                response = self._session.get(url, params=params, timeout=self._timeout)
            except requests.RequestException as exc:
                # Deliberately not str(exc): it can embed the URL, and the URL embeds the key.
                last_problem = f"network error: {type(exc).__name__}"
                continue

            status = response.status_code
            if status == 200:
                return response
            if status == 401:
                code = self._error_code(response)
                if code == "OUT_OF_USAGE_CREDITS":
                    raise QuotaExhaustedError("Odds API usage quota is exhausted")
                raise AuthenticationError(f"Odds API rejected the API key (HTTP 401, {code or 'no error code'})")
            if status == 429:
                raise RateLimitError(
                    f"Odds API rate limit hit for {sport} (HTTP 429)",
                    retry_after=self._retry_after(response),
                )
            if status >= 500:
                last_problem = f"server error HTTP {status}"
                continue
            # 404 unknown sport, 422 invalid parameters, ...: retrying cannot fix these.
            raise ProviderError(f"Odds API request for {sport} failed: HTTP {status} {self._error_code(response) or ''}".strip())
        raise ProviderError(f"Odds API request for {sport} failed after {self._max_retries + 1} attempts: {last_problem}")

    @staticmethod
    def _error_code(response: requests.Response) -> str | None:
        try:
            body: Any = response.json()
        except ValueError:
            return None
        return body.get("error_code") if isinstance(body, dict) else None

    @staticmethod
    def _retry_after(response: requests.Response) -> float | None:
        value = response.headers.get("Retry-After")
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None

    def close(self) -> None:
        self._session.close()

"""Polite HTTP client shared by the Slovak bookmaker providers.

* sends a normal browser User-Agent;
* never more than one request per ``min_interval`` (>= 1 s) per host, across all threads;
* retries network errors, HTTP 5xx and 429 with exponential backoff;
* HTTP 401/403 and captcha / bot-check pages raise :class:`BlockedError`. The provider is then
  marked "blocked" and no longer polled - this client never tries to get around such checks.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from odds_scanner.errors import BlockedError, ProviderError, RateLimitError

log = logging.getLogger(__name__)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
}

# Text that identifies a bot-check / captcha / WAF page rather than real data.
CHALLENGE_MARKERS = (
    "captcha",
    "cf-chl",
    "challenge-platform",
    "just a moment...",
    "attention required",
    "access denied",
    "request unsuccessful. incapsula",
    "_incapsula_resource",
    "ddos-guard",
    "bot detection",
    "are you a robot",
    "px-captcha",
)

MIN_INTERVAL_FLOOR = 1.0  # never more than 1 request/second per site, whatever the config says


def looks_like_challenge(text: str) -> bool:
    low = text[:20000].lower()
    return any(marker in low for marker in CHALLENGE_MARKERS)


class HostThrottle:
    """Spaces requests to the same host at least ``interval`` seconds apart (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def reserve(self, host: str, interval: float, now: float) -> float:
        """Book the next slot for ``host``; return how long to wait for it."""
        with self._lock:
            slot = max(now, self._next.get(host, now))
            self._next[host] = slot + interval
            return slot - now


GLOBAL_THROTTLE = HostThrottle()


class SiteClient:
    def __init__(
        self,
        name: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
        retry_backoff: float = 2.0,
        min_interval: float = 1.0,
        throttle: HostThrottle | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        if session is None:
            session = requests.Session()
            session.headers.update(DEFAULT_HEADERS)
        self._session = session
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._min_interval = max(MIN_INTERVAL_FLOOR, min_interval)
        self._throttle = throttle or GLOBAL_THROTTLE
        self._sleep = sleep
        self._monotonic = monotonic

    # ------------------------------------------------------------------ public
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._json(self.request("GET", url, params=params))

    def post_json(self, url: str, body: Any) -> Any:
        return self._json(self.request("POST", url, json_body=body))

    def request(self, method: str, url: str, *, params: dict[str, Any] | None = None, json_body: Any = None) -> requests.Response:
        """Send with throttling and retries. Returns a 2xx response or raises a ProviderError subclass."""
        host = urlsplit(url).netloc
        problem = "unknown error"
        for attempt in range(self._max_retries + 1):
            if attempt:
                delay = self._retry_backoff * 2 ** (attempt - 1)
                log.info("%s: retrying in %.0fs (%s)", self.name, delay, problem)
                self._sleep(delay)
            wait = self._throttle.reserve(host, self._min_interval, self._monotonic())
            if wait > 0:
                self._sleep(wait)
            try:
                if method == "GET":
                    response = self._session.get(url, params=params, timeout=self._timeout)
                else:
                    response = self._session.post(url, json=json_body, timeout=self._timeout)
            except requests.RequestException as exc:
                problem = f"network error: {type(exc).__name__}"
                continue

            status = response.status_code
            text = _text(response)
            if status in (401, 403):
                raise BlockedError(f"{self.name}: HTTP {status} - access refused (bot protection?)")
            if looks_like_challenge(text) and not _is_json(response):
                raise BlockedError(f"{self.name}: HTTP {status} returned a captcha / bot-check page")
            if 200 <= status < 300:
                return response
            if status == 429:
                retry_after = _retry_after(response)
                if attempt < self._max_retries and (retry_after is None or retry_after <= 30):
                    problem = "HTTP 429"
                    if retry_after:
                        self._sleep(retry_after)
                    continue
                raise RateLimitError(f"{self.name}: rate limited (HTTP 429)", retry_after=retry_after)
            if status >= 500:
                problem = f"server error HTTP {status}"
                continue
            raise ProviderError(f"{self.name}: HTTP {status}")
        raise ProviderError(f"{self.name}: failed after {self._max_retries + 1} attempts: {problem}")

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------ helpers
    def _json(self, response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            if looks_like_challenge(_text(response)):
                raise BlockedError(f"{self.name}: got a captcha / bot-check page instead of data") from None
            raise ProviderError(f"{self.name}: response was not valid JSON") from None


def _text(response: requests.Response) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - fakes / undecodable bodies
        return ""
    return text if isinstance(text, str) else ""


def _is_json(response: requests.Response) -> bool:
    headers = getattr(response, "headers", None) or {}
    return "json" in str(headers.get("Content-Type", "")).lower()


def _retry_after(response: requests.Response) -> float | None:
    headers = getattr(response, "headers", None) or {}
    value = headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None

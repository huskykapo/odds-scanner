"""Discord alerts through channel webhooks (alerts only; no bot account, no gateway connection).

One :class:`DiscordNotifier` per channel. A channel can be limited to a set of bookmakers - an arb is
posted there only if EVERY leg is at one of them - so e.g. a "slovakia" channel never shows a bet the
members cannot place. ``must_include`` additionally requires at least one leg at one of those books
(e.g. a "slovakia + crypto" channel that only shows the mixed arbs, not the ones already in "slovakia").

A webhook URL is a password (anyone with it can post): it comes from an environment variable and is
never logged; errors carry the HTTP status / exception type only.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import timezone
from typing import Callable, Collection, Sequence

import requests

from odds_scanner.errors import ConfigError, NotificationError
from odds_scanner.models import Arbitrage
from odds_scanner.notifiers.base import Notifier
from odds_scanner.notifiers.console import leg_label, market_label
from odds_scanner.notifiers.telegram import _age_text, _money

log = logging.getLogger(__name__)

WEBHOOK_PREFIXES = tuple(
    f"https://{host}/api/webhooks/" for host in ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")
)
COLORS = {"HIGH": 0x2ECC71, "MEDIUM": 0xF1C40F, "LOW": 0x95A5A6}
VERIFY_COLOR = 0xE74C3C
_MD_SPECIAL = "\\*_~`|>[]()#"


def escape_md(text: str) -> str:
    """Neutralise Discord markdown and mentions in third-party text (team / bookmaker names)."""
    out = "".join(f"\\{c}" if c in _MD_SPECIAL else c for c in str(text))
    return out.replace("@", "@​")


def _safe_link(url: str | None) -> bool:
    return bool(url) and str(url).startswith(("https://", "http://")) and not any(c in str(url) for c in " ()<>\n")


def valid_webhook(url: str | None) -> bool:
    return bool(url) and str(url).startswith(WEBHOOK_PREFIXES) and len(str(url)) < 300


def allowed(arb: Arbitrage, bookmakers: Collection[str] | None, must_include: Collection[str] | None = None) -> bool:
    """Every leg at one of ``bookmakers`` (None/empty = any), and at least one at ``must_include`` (if set)."""
    keys = [leg.bookmaker_key for leg in arb.legs]
    if bookmakers and not all(k in bookmakers for k in keys):
        return False
    return not must_include or any(k in must_include for k in keys)


def build_embed(arb: Arbitrage, currency: str = "", *, channel: str = "", dashboard_url: str | None = None) -> dict:
    """One arbitrage as a Discord embed (Discord shows ``<t:..>`` in each reader's own time zone)."""
    start = int(arb.commence_time.astimezone(timezone.utc).timestamp())
    ret = arb.total_stake + arb.guaranteed_profit
    fields = []
    for leg in arb.legs:
        book = escape_md(leg.bookmaker_title)
        value = f"{escape_md(leg_label(arb, leg))} @ **{leg.odds:.2f}**\nStake: **{_money(leg.stake, currency)}**"
        if _safe_link(leg.url):
            value += f"\n[Open {book}]({leg.url})"
        fields.append({"name": book, "value": value[:1024], "inline": True})
    fields.append({
        "name": "Total",
        "value": (f"Stake {_money(arb.total_stake, currency)} → return {_money(ret, currency)}\n"
                  f"Profit **{_money(arb.guaranteed_profit, currency)}**")[:1024],
        "inline": False,
    })
    lines = [
        f"**{escape_md(arb.sport_key.title())}** · starts <t:{start}:f> (<t:{start}:R>)",
        f"Market: **{escape_md(market_label(arb))}**",
    ]
    if arb.verify_manually:
        lines.append("⚠️ **VERIFY MANUALLY** - unusually high profit, likely a pricing error or stale odds")
    if _safe_link(dashboard_url):
        lines.append(f"[Open dashboard]({dashboard_url})")
    footer = [f"Odds age {_age_text(arb.odds_age_seconds)}"]
    if arb.confidence is not None:
        footer.append(f"Confidence {arb.confidence_label} {arb.confidence}/100")
    if channel:
        footer.append(channel)
    footer.append("Check every price on the bookmaker's site before betting")
    color = VERIFY_COLOR if arb.verify_manually else COLORS.get(arb.confidence_label, COLORS["MEDIUM"])
    return {
        "title": f"\U0001F6A8 +{arb.realized_profit_percent:.2f}% · {escape_md(arb.event_name)}"[:256],
        "description": "\n".join(lines)[:4000],
        "color": color,
        "fields": fields[:25],
        "footer": {"text": " · ".join(footer)[:2048]},
    }


class DiscordNotifier(Notifier):
    def __init__(
        self,
        webhook_url: str,
        *,
        name: str = "",
        bookmakers: Collection[str] | None = None,
        must_include: Collection[str] | None = None,
        currency: str = "",
        max_per_cycle: int = 10,
        min_profit_percent: float | None = None,
        dashboard_url: str | None = None,
        username: str = "Arb Scanner",
        session: requests.Session | None = None,
        timeout: float = 10.0,
        pause_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not valid_webhook(webhook_url):
            raise ConfigError(f"Discord channel {name or '?'}: the webhook URL must start with https://discord.com/api/webhooks/")
        self._url = webhook_url
        self.name = name
        self.bookmakers = frozenset(b.lower() for b in bookmakers or ())
        self.must_include = frozenset(b.lower() for b in must_include or ())
        self._currency = currency
        self._max = max_per_cycle
        self._min_profit = min_profit_percent
        self._dashboard_url = dashboard_url
        self._username = username
        self._session = session or requests.Session()
        self._timeout = timeout
        self._pause = pause_seconds
        self._sleep = sleep

    @classmethod
    def from_env(cls, webhook_env: str, **kwargs) -> "DiscordNotifier":
        url = os.environ.get(webhook_env)
        if not url:
            raise ConfigError(f"Discord channel {kwargs.get('name') or webhook_env}: environment variable {webhook_env} is not set")
        return cls(url, **kwargs)

    def wants(self, arb: Arbitrage) -> bool:
        if self._min_profit is not None and arb.realized_profit_percent < self._min_profit:
            return False
        return allowed(arb, self.bookmakers, self.must_include)

    def notify(self, arbs: Sequence[Arbitrage]) -> list[Arbitrage]:
        handled: list[Arbitrage] = []
        sent = 0
        for arb in arbs:
            if not self.wants(arb):
                handled.append(arb)  # not for this channel: deliberately skipped, never retried
                continue
            if sent >= self._max:
                log.info("Discord %s: per-cycle cap of %d reached; the rest wait for the next cycle", self.name, self._max)
                break
            if sent:
                self._sleep(self._pause)  # stay well under Discord's webhook rate limit
            try:
                self.post({"embeds": [build_embed(arb, self._currency, channel=self.name, dashboard_url=self._dashboard_url)]})
            except NotificationError as exc:
                log.warning("Discord %s: alert failed: %s", self.name, exc)
                break  # not acked: offered again next cycle
            sent += 1
            handled.append(arb)
        return handled

    def post(self, payload: dict) -> None:
        body = {"username": self._username, "allowed_mentions": {"parse": []}, **payload}
        try:
            response = self._session.post(self._url, json=body, timeout=self._timeout)
        except requests.RequestException as exc:  # not str(exc): it can contain the webhook URL
            raise NotificationError(f"network error: {type(exc).__name__}") from None
        if response.status_code in (200, 204):
            return
        if response.status_code == 429:
            raise NotificationError("rate limited by Discord (HTTP 429); retrying next cycle")
        if response.status_code in (401, 404):
            raise NotificationError(f"HTTP {response.status_code}: the webhook was deleted or the URL is wrong")
        raise NotificationError(f"HTTP {response.status_code}")

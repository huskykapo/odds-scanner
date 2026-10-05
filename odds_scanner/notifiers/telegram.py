"""Telegram bot notifier (alerts only). Token and chat id come from environment variables."""

from __future__ import annotations

import html
import logging
import os
from datetime import timedelta, timezone
from typing import Sequence

import requests

from odds_scanner.errors import ConfigError, NotificationError
from odds_scanner.models import Arbitrage
from odds_scanner.notifiers.base import Notifier
from odds_scanner.notifiers.console import leg_label, market_label, money, money2
from odds_scanner.notifiers.dedupe import DedupeCache

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"


CURRENCY_SYMBOLS = {"EUR": "\u20ac", "USD": "$", "GBP": "\u00a3"}


def _money(value: float, currency: str) -> str:
    symbol = CURRENCY_SYMBOLS.get(currency.upper()) if currency else ""
    if symbol:
        return f"{symbol}{value:,.2f}"
    return f"{value:,.2f}" + (f" {currency}" if currency else "")


def _age_text(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    return f"{seconds:.0f} sec" if seconds < 90 else f"{seconds / 60:.0f} min"


def format_message(arb: Arbitrage, currency: str = "", *, dashboard_url: str | None = None) -> str:
    """HTML alert for one arbitrage (all third-party text is escaped)."""
    starts = arb.commence_time.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ret = arb.total_stake + arb.guaranteed_profit
    lines = [
        "\U0001F6A8 <b>ARBITRAGE FOUND</b>",
        "",
        "Event:",
        html.escape(arb.event_name),
        f"<i>{html.escape(arb.sport_key)} - starts {starts}</i>",
        "",
        "Market:",
        html.escape(market_label(arb)),
        "",
        "ROI:",
        f"<b>+{arb.realized_profit_percent:.2f}%</b>",
        "",
        "Bankroll:",
        _money(arb.bankroll, currency),
    ]
    if arb.verify_manually:
        lines += ["", "<b>VERIFY MANUALLY</b> - unusually high profit, likely a pricing error or stale odds"]
    for leg in arb.legs:
        book = html.escape(leg.bookmaker_title)
        if leg.url and leg.url.startswith(("https://", "http://")):
            book = f'<a href="{html.escape(leg.url, quote=True)}">{book}</a>'
        lines += ["", f"{book}:", f"{html.escape(leg_label(arb, leg))} @ <b>{leg.odds:.2f}</b>", f"Stake: {_money(leg.stake, currency)}"]
    lines += [
        "",
        "Return:",
        _money(ret, currency),
        "",
        "Profit:",
        f"<b>{_money(arb.guaranteed_profit, currency)}</b>",
        "",
        "Odds age:",
        _age_text(arb.odds_age_seconds),
    ]
    if arb.confidence is not None:
        lines += ["", "Confidence:", f"{arb.confidence_label} ({arb.confidence}/100)"]
    if dashboard_url:
        lines += ["", f'<a href="{html.escape(dashboard_url, quote=True)}">OPEN DASHBOARD</a>']
    lines += ["", "<i>Odds move fast - verify every price on the bookmaker's site before acting.</i>"]
    return "\n".join(lines)


class TelegramNotifier(Notifier):
    """Sends alerts to one chat. Under the live engine it sends exactly what the engine says is due;
    ``dedupe`` is only for the legacy single-provider mode that has no opportunity registry."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        dedupe: DedupeCache | None = None,
        currency: str = "",
        max_per_cycle: int = 10,
        min_profit_percent: float | None = None,
        dashboard_url: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._dedupe = dedupe
        self._currency = currency
        self._max_per_cycle = max_per_cycle
        self._min_profit = min_profit_percent
        self._dashboard_url = dashboard_url
        self._session = session or requests.Session()
        self._timeout = timeout

    @classmethod
    def from_env(
        cls, token_env: str, chat_id_env: str, *, dedupe: DedupeCache | None = None, **kwargs
    ) -> "TelegramNotifier":
        token, chat_id = os.environ.get(token_env), os.environ.get(chat_id_env)
        missing = [n for n, v in ((token_env, token), (chat_id_env, chat_id)) if not v]
        if missing:
            raise ConfigError(
                f"Telegram notifications are enabled but environment variable(s) not set: {', '.join(missing)}"
            )
        return cls(token, chat_id, dedupe=dedupe, **kwargs)  # type: ignore[arg-type]

    def notify(self, arbs: Sequence[Arbitrage]) -> list[Arbitrage]:
        handled: list[Arbitrage] = []
        sent = 0
        for arb in arbs:
            if self._min_profit is not None and arb.realized_profit_percent < self._min_profit:
                handled.append(arb)  # below this notifier's threshold: deliberately skipped, not retried
                continue
            if self._dedupe is not None and self._dedupe.is_duplicate(arb.dedupe_key):
                handled.append(arb)
                continue
            if sent >= self._max_per_cycle:
                log.info("Telegram: per-cycle cap of %d reached; remaining alerts wait for the next poll", self._max_per_cycle)
                break
            try:
                self._send(format_message(arb, self._currency, dashboard_url=self._dashboard_url))
            except NotificationError as exc:
                # Not handled, so the engine offers it again next cycle. Stop now rather than hammer a failing API.
                log.warning("Telegram alert failed: %s", exc)
                break
            if self._dedupe is not None:
                self._dedupe.remember(arb.dedupe_key)
            sent += 1
            handled.append(arb)
        return handled

    def _send(self, text: str) -> None:
        url = f"{API_URL}/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            response = self._session.post(url, json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            # Not str(exc): it can contain the URL, which contains the bot token.
            raise NotificationError(f"network error: {type(exc).__name__}") from None
        if response.status_code != 200:
            detail = ""
            try:
                detail = str(response.json().get("description", ""))
            except (ValueError, AttributeError):
                pass
            raise NotificationError(f"HTTP {response.status_code} {detail}".strip())

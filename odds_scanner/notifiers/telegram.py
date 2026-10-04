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


def format_message(arb: Arbitrage, currency: str = "") -> str:
    """HTML-formatted alert text for one arbitrage (all API-supplied text is escaped)."""
    unit = f" {currency}" if currency else ""
    starts = arb.commence_time.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"<b>Arb {arb.realized_profit_percent:.2f}%</b> - {html.escape(arb.event_name)}",
        f"{html.escape(arb.sport_key)} | {html.escape(market_label(arb))} | starts {starts}",
    ]
    if arb.verify_manually:
        lines.append("<b>VERIFY MANUALLY</b> - unusually high profit, likely a pricing error or stale odds")
    for leg in arb.legs:
        book = html.escape(leg.bookmaker_title)
        if leg.url and leg.url.startswith(("https://", "http://")):
            book = f'<a href="{html.escape(leg.url, quote=True)}">{book}</a>'
        lines.append(
            f"- {html.escape(leg_label(arb, leg))} @ <b>{leg.odds:.2f}</b> on {book}: "
            f"stake {money(leg.stake)}{unit} (pays {money2(leg.payout)}{unit})"
        )
    lines.append(
        f"Total stake {money2(arb.total_stake)}{unit}, guaranteed profit {money2(arb.guaranteed_profit)}{unit}"
    )
    lines.append("<i>Odds move fast - verify every price before acting.</i>")
    return "\n".join(lines)


class TelegramNotifier(Notifier):
    handles_dedupe = True  # give it every current arb; it sends each one once
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        dedupe: DedupeCache,
        currency: str = "",
        max_per_cycle: int = 10,
        min_profit_percent: float | None = None,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._dedupe = dedupe
        self._currency = currency
        self._max_per_cycle = max_per_cycle
        self._min_profit = min_profit_percent
        self._session = session or requests.Session()
        self._timeout = timeout

    @classmethod
    def from_env(
        cls, token_env: str, chat_id_env: str, *, dedupe: DedupeCache, **kwargs
    ) -> "TelegramNotifier":
        token, chat_id = os.environ.get(token_env), os.environ.get(chat_id_env)
        missing = [n for n, v in ((token_env, token), (chat_id_env, chat_id)) if not v]
        if missing:
            raise ConfigError(
                f"Telegram notifications are enabled but environment variable(s) not set: {', '.join(missing)}"
            )
        return cls(token, chat_id, dedupe=dedupe, **kwargs)  # type: ignore[arg-type]

    def notify(self, arbs: Sequence[Arbitrage]) -> None:
        sent = 0
        for arb in arbs:
            if self._min_profit is not None and arb.realized_profit_percent < self._min_profit:
                continue
            if self._dedupe.is_duplicate(arb.dedupe_key):
                continue
            if sent >= self._max_per_cycle:
                log.info("Telegram: per-cycle cap of %d reached; remaining alerts wait for the next poll", self._max_per_cycle)
                break
            try:
                self._send(format_message(arb, self._currency))
            except NotificationError as exc:
                # Not remembered, so it is retried next cycle. Stop now rather than hammer a failing API.
                log.warning("Telegram alert failed: %s", exc)
                break
            self._dedupe.remember(arb.dedupe_key)
            sent += 1

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

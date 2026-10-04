"""``telegram-test``: guides Telegram alert setup and sends a test message.

Run it repeatedly; each run tells you the next missing step:
no bot token -> how to create a bot; token but no chat id -> lists the chats that wrote to the
bot so you can pick yours; both set -> sends a test message.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping

import requests

from odds_scanner.config import Config
from odds_scanner.notifiers.telegram import API_URL

TIMEOUT = 10.0


class _TelegramFailure(Exception):
    """A Telegram API call failed; the message never contains the bot token."""


def _api(session: Any, token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    url = f"{API_URL}/bot{token}/{method}"
    try:
        response = session.post(url, json=payload or {}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        # Not str(exc): it can contain the URL, which contains the token.
        raise _TelegramFailure(f"network error ({type(exc).__name__}) - check your internet connection") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not isinstance(body, dict) or not body.get("ok"):
        description = body.get("description", "") if isinstance(body, dict) else ""
        if response.status_code in (401, 404):
            description = "the bot token is wrong or was revoked" + (f" ({description})" if description else "")
        raise _TelegramFailure(f"Telegram said HTTP {response.status_code}: {description}".rstrip(": "))
    return body.get("result")


def _chats_from_updates(updates: Any) -> dict[int, str]:
    """chat id -> readable name, for every chat that has contacted the bot."""
    chats: dict[int, str] = {}
    for update in updates or []:
        if not isinstance(update, dict):
            continue
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat") if isinstance(update.get(key), dict) else None
            if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                name = chat.get("title") or " ".join(
                    p for p in (chat.get("first_name"), chat.get("last_name")) if p
                ) or chat.get("username") or "?"
                chats[chat["id"]] = f"{name} ({chat.get('type', 'chat')})"
    return chats


def check_telegram(
    cfg: Config,
    *,
    session: Any | None = None,
    environ: Mapping[str, str] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Walk through Telegram setup. Returns 0 when a test message was delivered, else 1."""
    env = os.environ if environ is None else environ
    tg = cfg.notifications.telegram
    token, chat_id = env.get(tg.token_env), env.get(tg.chat_id_env)
    session = session or requests.Session()

    if not token:
        out("Step 1 of 3 - create a Telegram bot (about a minute, on your phone or Telegram Desktop):")
        out("  1. Open Telegram and chat with @BotFather.")
        out("  2. Send /newbot, choose a name and a username ending in 'bot'.")
        out("  3. BotFather replies with a token that looks like 123456789:ABC-def... - copy it.")
        out("  4. Open your new bot and press Start (or send it any message).")
        out("Then, in PowerShell, set the token and run this command again:")
        out(f'  $env:{tg.token_env} = "PASTE-THE-TOKEN-HERE"')
        out("  (that lasts for this PowerShell window only; see below to make it permanent)")
        return 1

    if not chat_id:
        out("Token found. Step 2 of 3 - find your chat id.")
        try:
            chats = _chats_from_updates(_api(session, token, "getUpdates"))
        except _TelegramFailure as exc:
            out(f"Could not ask Telegram for your chats: {exc}")
            return 1
        if not chats:
            out("No chats found yet. Open your bot in Telegram, press Start / send it any message, then run this again.")
            out("(For a group: add the bot to the group and send a message in it.)")
            return 1
        out("Chats that have written to your bot:")
        for cid, name in chats.items():
            out(f"  {cid}   {name}")
        first = next(iter(chats))
        out("Set the id of the chat that should get the alerts, then run this command again:")
        out(f'  $env:{tg.chat_id_env} = "{first}"')
        return 1

    out("Token and chat id found. Step 3 of 3 - sending a test message ...")
    try:
        _api(session, token, "sendMessage", {
            "chat_id": chat_id,
            "text": "odds-scanner test message: Telegram alerts are working. "
                    "Arbs will arrive here as they are found. Odds move fast - always verify prices before betting.",
        })
    except _TelegramFailure as exc:
        out(f"Test message failed: {exc}")
        if "chat not found" in str(exc).lower():
            out("The chat id does not match a chat this bot can write to. Press Start in the bot's chat and re-check the id.")
        return 1
    out("Test message sent - check Telegram. Setup is working.")
    if not tg.enabled:
        out("One more thing: alerts are still OFF. In config.yaml set  notifications -> telegram -> enabled: true  and restart the scanner.")
    out("To keep the token and chat id for future PowerShell windows (run once, then open a new window):")
    out(f'  setx {tg.token_env} "<your token>"')
    out(f'  setx {tg.chat_id_env} "{chat_id}"')
    return 0

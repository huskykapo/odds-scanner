"""``py start.py discord-test``: check every configured Discord channel and post one test message to it."""

from __future__ import annotations

import os

from odds_scanner.config import Config
from odds_scanner.errors import ConfigError, NotificationError
from odds_scanner.notifiers.discord import DiscordNotifier, valid_webhook

HOW_TO = """How to get a webhook URL (once per channel, 1 minute):
  Discord -> your server -> right-click the channel -> Edit Channel -> Integrations -> Webhooks
  -> New Webhook -> Copy Webhook URL. Then in PowerShell (example for a variable named DISCORD_WEBHOOK_SK):
     $env:DISCORD_WEBHOOK_SK = "PASTE-THE-URL"
     setx DISCORD_WEBHOOK_SK "PASTE-THE-URL"      (makes it permanent for new windows)
  The URL is a password: anyone who has it can post in that channel. Never share it or put it in a file."""


def check_discord(cfg: Config, *, environ=None, session=None) -> int:
    env = os.environ if environ is None else environ
    d = cfg.notifications.discord
    if not d.channels:
        print("No Discord channels in config.yaml (notifications: discord: channels:).\n")
        print(HOW_TO)
        return 1
    if not d.enabled:
        print("Note: notifications.discord.enabled is false - the scanner will not post until you set it to true.\n")
    failures = 0
    for ch in d.channels:
        books = ", ".join(ch.bookmakers) if ch.bookmakers else "any bookmaker"
        label = f"{ch.name:<20}"
        url = env.get(ch.webhook_env)
        if not url:
            print(f"{label} MISSING  environment variable {ch.webhook_env} is not set in this window")
            failures += 1
            continue
        if not valid_webhook(url):
            print(f"{label} WRONG    {ch.webhook_env} does not look like a Discord webhook URL (https://discord.com/api/webhooks/...)")
            failures += 1
            continue
        try:
            notifier = DiscordNotifier(url, name=ch.name, username=d.username, session=session)
            extra = f"\nOnly arbs that also use: {', '.join(ch.must_include)}" if ch.must_include else ""
            notifier.post({"content": f"✅ Test message from the arb scanner - channel **{ch.name}**.\n"
                                      f"Arbs posted here: every bet at {books}.{extra}"})
        except (ConfigError, NotificationError) as exc:
            print(f"{label} FAILED   {exc}")
            failures += 1
            continue
        print(f"{label} OK       test message sent ({books})")
    if failures:
        print()
        print(HOW_TO)
    return 1 if failures else 0

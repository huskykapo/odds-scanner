"""Discord webhook alerts: per-channel bookmaker filters, message content, failure handling, setup check."""

from datetime import timedelta

import pytest

from odds_scanner import cli
from odds_scanner.config import config_from_dict
from odds_scanner.errors import ConfigError
from odds_scanner.models import Arbitrage, ArbLeg
from odds_scanner.notifiers.discord import DiscordNotifier, allowed, build_embed, escape_md, valid_webhook
from odds_scanner.notifiers.discord_setup import check_discord
from tests.conftest import NOW, FakeResponse, FakeSession

URL = "https://discord.com/api/webhooks/123/SECRET-TOKEN"


def arb(*books, roi=2.0, event="Arsenal vs Chelsea", confidence=85, label="HIGH"):
    legs = tuple(
        ArbLeg(outcome=o, bookmaker_key=b, bookmaker_title=b.title(), odds=3.2, stake=33.5, payout=107.2,
               odds_updated=NOW - timedelta(seconds=20), url=f"https://{b}.example/match")
        for o, b in zip(("1", "X", "2"), books)
    )
    return Arbitrage(
        event_id=f"ev:{'|'.join(books)}", sport_key="football", event_name=event, commence_time=NOW + timedelta(hours=3),
        market="h2h_3_way", line=None, legs=legs, inverse_sum=0.98, profit_percent=roi, bankroll=100,
        total_stake=100.5, guaranteed_profit=1.7, realized_profit_percent=roi, found_at=NOW,
        home_team="Arsenal", away_team="Chelsea", confidence=confidence, confidence_label=label, status="VERIFIED",
    )


def notifier(*queue, **kw):
    session = FakeSession(*queue)
    return DiscordNotifier(URL, session=session, sleep=lambda s: None, **kw), session


# ------------------------------------------------------------------ routing
def test_every_leg_must_be_at_an_allowed_bookmaker():
    sk = {"nike", "tipos", "doxxbet"}
    assert allowed(arb("nike", "tipos", "doxxbet"), sk)
    assert not allowed(arb("nike", "tipos", "stake"), sk)
    assert allowed(arb("nike", "tipos", "stake"), None)  # no list = any bookmaker


def test_must_include_keeps_only_mixed_arbs():
    both = {"nike", "tipos", "stake", "roobet"}
    crypto = {"stake", "roobet"}
    assert allowed(arb("nike", "tipos", "stake"), both, crypto)
    assert not allowed(arb("nike", "tipos", "nike"), both, crypto)


def test_notify_posts_only_matching_arbs_and_reports_the_rest_as_handled():
    n, session = notifier(FakeResponse(204), name="slovakia", bookmakers=["nike", "tipos", "doxxbet"])
    sk_arb, crypto_arb = arb("nike", "tipos", "doxxbet"), arb("nike", "stake", "tipos")
    handled = n.notify([crypto_arb, sk_arb])
    assert handled == [crypto_arb, sk_arb]  # the crypto one is skipped on purpose: never offered again
    ((url, body),) = session.calls
    assert url == URL and body["allowed_mentions"] == {"parse": []}
    assert "Arsenal vs Chelsea" in body["embeds"][0]["title"]


def test_failed_post_is_not_acked_so_it_is_retried_next_cycle():
    n, _ = notifier(FakeResponse(204), FakeResponse(429))
    a1, a2, a3 = arb("nike", "tipos", "synot"), arb("nike", "tipos", "doxxbet"), arb("tipos", "nike", "synot")
    assert n.notify([a1, a2, a3]) == [a1]


def test_per_cycle_cap_and_min_profit():
    n, session = notifier(FakeResponse(204), max_per_cycle=1, min_profit_percent=1.0)
    small, big1, big2 = arb("a", "b", "c", roi=0.6), arb("a", "b", "d", roi=2.0), arb("a", "b", "e", roi=3.0)
    assert n.notify([small, big1, big2]) == [small, big1]
    assert len(session.calls) == 1


def test_network_errors_never_show_the_webhook_url(caplog):
    import requests

    n, _ = notifier(requests.ConnectionError(f"failed to reach {URL}"))
    assert n.notify([arb("a", "b", "c")]) == []
    assert "SECRET-TOKEN" not in caplog.text


# ------------------------------------------------------------------ content
def test_embed_has_stakes_links_local_time_and_escapes_names():
    a = arb("stake", "roobet", "mystake", event="*Bold* @everyone FC")
    e = build_embed(a, "EUR", channel="crypto")
    assert "\\*Bold\\*" in e["title"] and "@everyone" not in e["title"]
    start = int((NOW + timedelta(hours=3)).timestamp())
    assert f"<t:{start}:f>" in e["description"]
    assert [f["name"] for f in e["fields"][:3]] == ["Stake", "Roobet", "Mystake"]
    assert "€33.50" in e["fields"][0]["value"] and "[Open Stake](https://stake.example/match)" in e["fields"][0]["value"]
    assert e["color"] == 0x2ECC71 and "crypto" in e["footer"]["text"]
    assert "verify" in e["footer"]["text"].lower() or "check every price" in e["footer"]["text"].lower()


def test_helpers():
    assert escape_md("a_b|c") == "a\\_b\\|c"
    assert valid_webhook(URL) and valid_webhook("https://discordapp.com/api/webhooks/1/x")
    assert not valid_webhook("https://evil.example/api/webhooks/1/x") and not valid_webhook("")
    with pytest.raises(ConfigError):
        DiscordNotifier("http://discord.com/api/webhooks/1/x")


# ------------------------------------------------------------------ config + wiring
def discord_cfg(enabled=True, **extra):
    return config_from_dict({"notifications": {"console": False, "discord": {"enabled": enabled, "channels": [
        {"name": "slovakia", "webhook_env": "HOOK_SK", "bookmakers": ["nike", "tipos"]},
        {"name": "crypto", "webhook_env": "HOOK_CRYPTO", "bookmakers": ["stake", "roobet"], **extra},
    ]}}})


def test_config_validation():
    cfg = discord_cfg(must_include=["stake"])
    assert cfg.notifications.discord.channels[1].must_include == ("stake",)
    with pytest.raises(ConfigError, match="webhook_env"):
        config_from_dict({"notifications": {"discord": {"channels": [{"name": "x"}]}}})
    with pytest.raises(ConfigError, match="unique"):
        config_from_dict({"notifications": {"discord": {"channels": [{"name": "x", "webhook_env": "A"}, {"name": "x", "webhook_env": "B"}]}}})
    with pytest.raises(ConfigError, match="no channels"):
        config_from_dict({"notifications": {"discord": {"enabled": True}}})


def test_channels_without_a_webhook_are_skipped_but_one_is_needed(monkeypatch):
    monkeypatch.setenv("HOOK_SK", URL)
    monkeypatch.delenv("HOOK_CRYPTO", raising=False)
    (only,) = cli.build_notifiers(discord_cfg(), own_dedupe=False)
    assert only.name == "slovakia" and only.bookmakers == {"nike", "tipos"}
    monkeypatch.delenv("HOOK_SK")
    with pytest.raises(ConfigError, match="discord-test"):
        cli.build_notifiers(discord_cfg(), own_dedupe=False)
    assert cli.audiences(discord_cfg()) == [frozenset({"nike", "tipos"}), frozenset({"stake", "roobet"})]
    assert cli.audiences(discord_cfg(enabled=False)) == []


def test_discord_test_command(capsys):
    session = FakeSession(FakeResponse(204))
    code = check_discord(discord_cfg(), environ={"HOOK_SK": URL, "HOOK_CRYPTO": "https://example.com/x"}, session=session)
    out = capsys.readouterr().out
    assert code == 1
    assert "slovakia" in out and "OK" in out and "WRONG" in out and "SECRET-TOKEN" not in out
    assert "Test message" in session.calls[0][1]["content"]

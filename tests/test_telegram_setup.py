import requests

from odds_scanner import cli
from odds_scanner.config import config_from_dict
from odds_scanner.notifiers.telegram_setup import check_telegram
from tests.conftest import FakeResponse, FakeSession

TOKEN = "123456:SECRET-TOKEN"


def cfg(enabled=False):
    return config_from_dict({"notifications": {"telegram": {"enabled": enabled}}})


def run(session=None, env=None, enabled=False):
    lines = []
    code = check_telegram(cfg(enabled), session=session or FakeSession(), environ=env or {}, out=lines.append)
    return code, "\n".join(lines)


def updates(*chats):
    return {"ok": True, "result": [{"update_id": i, "message": {"chat": c}} for i, c in enumerate(chats)]}


def test_no_token_explains_how_to_create_a_bot_without_any_network_call():
    session = FakeSession()
    code, text = run(session)
    assert code == 1 and session.calls == []
    assert "@BotFather" in text and "/newbot" in text and "$env:TELEGRAM_BOT_TOKEN" in text


def test_token_without_chat_id_lists_chats_and_suggests_the_command():
    session = FakeSession(FakeResponse(200, updates(
        {"id": 42, "type": "private", "first_name": "Matúš", "last_name": "Kačáni"},
        {"id": -100, "type": "group", "title": "Arb alerts"},
        {"id": 42, "type": "private", "first_name": "Matúš", "last_name": "Kačáni"})))  # duplicate collapses
    code, text = run(session, {"TELEGRAM_BOT_TOKEN": TOKEN})
    assert code == 1
    assert "42   Matúš Kačáni (private)" in text and "-100   Arb alerts (group)" in text
    assert text.count("Matúš Kačáni") == 1
    assert '$env:TELEGRAM_CHAT_ID = "42"' in text
    assert session.calls[0][0].endswith("/getUpdates")
    assert TOKEN not in text


def test_token_without_any_messages_asks_to_press_start():
    code, text = run(FakeSession(FakeResponse(200, {"ok": True, "result": []})), {"TELEGRAM_BOT_TOKEN": TOKEN})
    assert code == 1 and "press Start" in text


def test_chat_ids_are_found_in_other_update_kinds():
    body = {"ok": True, "result": [{"my_chat_member": {"chat": {"id": 7, "type": "channel", "title": "News"}}},
                                   {"channel_post": {"chat": {"id": 8, "type": "channel", "username": "chan"}}}, "junk"]}
    _, text = run(FakeSession(FakeResponse(200, body)), {"TELEGRAM_BOT_TOKEN": TOKEN})
    assert "7   News (channel)" in text and "8   chan (channel)" in text


def test_wrong_token_gives_a_plain_explanation_and_never_prints_the_token():
    session = FakeSession(FakeResponse(401, {"ok": False, "description": "Unauthorized"}))
    code, text = run(session, {"TELEGRAM_BOT_TOKEN": TOKEN})
    assert code == 1 and "token is wrong" in text and TOKEN not in text


def test_network_error_does_not_leak_the_token():
    leaky = requests.ConnectionError(f"failed: https://api.telegram.org/bot{TOKEN}/getUpdates")
    code, text = run(FakeSession(leaky), {"TELEGRAM_BOT_TOKEN": TOKEN})
    assert code == 1 and "ConnectionError" in text and "SECRET" not in text


def test_both_set_sends_a_test_message_and_reminds_to_enable_alerts():
    session = FakeSession(FakeResponse(200, {"ok": True, "result": {}}))
    code, text = run(session, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}, enabled=False)
    assert code == 0 and "Setup is working" in text
    url, payload = session.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage" and payload["chat_id"] == "42"
    assert "enabled: true" in text  # alerts are still off in the config
    assert 'setx TELEGRAM_CHAT_ID "42"' in text and TOKEN not in text


def test_no_enable_reminder_when_already_enabled():
    session = FakeSession(FakeResponse(200, {"ok": True, "result": {}}))
    code, text = run(session, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}, enabled=True)
    assert code == 0 and "enabled: true" not in text


def test_chat_not_found_is_explained():
    session = FakeSession(FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"}))
    code, text = run(session, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "999"})
    assert code == 1 and "chat not found" in text and "Press Start" in text


def test_cli_command_runs_offline_when_nothing_is_set(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    conf = tmp_path / "c.yaml"
    conf.write_text("storage: {backend: none}\n")
    assert cli.main(["-c", str(conf), "telegram-test"]) == 1
    assert "@BotFather" in capsys.readouterr().out

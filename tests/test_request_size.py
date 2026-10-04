"""Bigger requests (all matches instead of the site page's top 50 / top matches) with safe fallback."""

import copy

import pytest

from odds_scanner.config import load_config
from odds_scanner.discovery import probe_doxxbet
from odds_scanner.errors import BlockedError, ProviderError
from odds_scanner.providers.sk import DoxxbetProvider, SynotProvider, TiposProvider
from odds_scanner.providers.sk.http import HostThrottle, SiteClient
from tests.conftest import NOW, FakeResponse, load_sk

FULL_TIPOS = load_sk("synot_football_standard_events.json")  # 4 events
EMPTY_TIPOS = {"Result": 1, "ReturnValue": ""}  # accepted, but no matches


def doxx_payload(n_events):
    """DOXXbet sample trimmed to n events (the sample has 2)."""
    p = load_sk("doxxbet_football.json")
    p["EventChanceTypes"] = p["EventChanceTypes"][:n_events]
    return p


class Site:
    def __init__(self, answer):
        self.answer = answer
        self.bodies = []

    def post(self, url, json=None, timeout=None):
        self.bodies.append(copy.deepcopy(json))
        body = self.answer(json)
        return body if isinstance(body, FakeResponse) else FakeResponse(json_body=body)

    def close(self):
        pass


def client(site):
    return SiteClient("T", session=site, sleep=lambda s: None, throttle=HostThrottle(), max_retries=0)


# ------------------------------------------------------------------ Tipos / Synot page size
def test_tipos_uses_the_biggest_page_size_the_site_accepts():
    def answer(body):
        return {500: {"Result": 0, "ReturnValue": None}, 200: FULL_TIPOS, 50: EMPTY_TIPOS}[body["Top"]]

    site = Site(answer)
    p = TiposProvider(client=client(site), clock=lambda: NOW)
    assert len(p.fetch_odds("football").events) == 4
    assert [b["Top"] for b in site.bodies] == [500, 200, 50]  # first poll: each size tried once
    assert p.request_choice == {"football": 200}  # 500 refused, 200 gave the most matches
    site.bodies.clear()
    p.fetch_odds("football")
    assert [b["Top"] for b in site.bodies] == [200]  # later polls: one request


def test_tipos_falls_back_to_50_when_bigger_pages_fail():
    def answer(body):
        return FULL_TIPOS if body["Top"] == 50 else FakeResponse(status=400)

    p = SynotProvider(client=client(Site(answer)), clock=lambda: NOW)
    assert len(p.fetch_odds("football").events) == 4
    assert p.request_choice == {"football": 50}


def test_tipos_keeps_500_when_it_works():
    site = Site(lambda body: FULL_TIPOS)
    p = TiposProvider(client=client(site), clock=lambda: NOW)
    p.fetch_odds("football")
    assert p.request_choice == {"football": 500}  # ties: the first (biggest) wins


def test_tipos_fixed_top_option_makes_a_single_request():
    site = Site(lambda body: FULL_TIPOS)
    TiposProvider(client=client(site), options={"top": 50}, clock=lambda: NOW).fetch_odds("football")
    assert [b["Top"] for b in site.bodies] == [50]


def test_all_page_sizes_failing_is_an_error_and_403_still_means_blocked():
    with pytest.raises(ProviderError):
        TiposProvider(client=client(Site(lambda body: FakeResponse(status=400))), clock=lambda: NOW).fetch_odds("football")
    with pytest.raises(BlockedError):
        TiposProvider(client=client(Site(lambda body: FakeResponse(status=403))), clock=lambda: NOW).fetch_odds("football")


def test_choice_is_made_again_after_the_chosen_size_fails():
    calls = {"n": 0}

    def answer(body):
        calls["n"] += 1
        if 4 <= calls["n"] <= 4:  # the second poll's single request fails
            return FakeResponse(status=500)
        return FULL_TIPOS

    site = Site(answer)
    p = TiposProvider(client=client(site), clock=lambda: NOW)
    p.fetch_odds("football")  # 3 requests, picks 500
    with pytest.raises(ProviderError):
        p.fetch_odds("football")
    assert p.request_choice == {}
    site.bodies.clear()
    p.fetch_odds("football")
    assert [b["Top"] for b in site.bodies] == [500, 200, 50]


# ------------------------------------------------------------------ DOXXbet "top"
def test_doxxbet_prefers_all_matches_when_the_site_accepts_top_minus_one():
    site = Site(lambda body: doxx_payload(2 if body["top"] == -1 else 1))
    p = DoxxbetProvider(client=client(site), clock=lambda: NOW)
    assert len(p.fetch_odds("football").events) == 2
    assert p.request_choice == {"football": -1}
    assert [(b["top"], b["date"]) for b in site.bodies] == [(-1, "TD"), (-1, "TM"), (1, "TD"), (1, "TM")]
    site.bodies.clear()
    p.fetch_odds("football")
    assert [b["top"] for b in site.bodies] == [-1, -1]


@pytest.mark.parametrize("minus_one_answer", [FakeResponse(status=400), {"EventChanceTypes": [], "Odds": {}}])
def test_doxxbet_falls_back_to_top_1_when_minus_one_is_refused_or_empty(minus_one_answer):
    site = Site(lambda body: minus_one_answer if body["top"] == -1 else doxx_payload(2))
    p = DoxxbetProvider(client=client(site), clock=lambda: NOW)
    assert len(p.fetch_odds("football").events) == 2
    assert p.request_choice == {"football": 1}


def test_doxxbet_explicit_top_in_body_is_respected():
    site = Site(lambda body: doxx_payload(2))
    DoxxbetProvider(client=client(site), options={"body": {"top": 1}}, clock=lambda: NOW).fetch_odds("football")
    assert {b["top"] for b in site.bodies} == {1}


def test_sport_id_lookup_uses_the_top_value_polling_settled_on():
    site = Site(lambda body: doxx_payload(2))
    p = DoxxbetProvider(client=client(site), clock=lambda: NOW)
    p.fetch_odds("football")  # settles on -1
    site.bodies.clear()
    probe_doxxbet(p, 67)
    assert site.bodies[0]["top"] == -1 and site.bodies[0]["sport"] == 67


def test_shipped_config_asks_for_bigger_requests():
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    assert cfg.providers["tipos"].options["top_values"] == [500, 200, 50]
    assert cfg.providers["synot"].options["top_values"] == [500, 200, 50]
    assert cfg.providers["doxxbet"].options["top_values"] == [-1, 1]
    assert "top" not in cfg.providers["tipos"].options

"""Synot tip: same betting platform and API as Tipos, on ``https://sport.synottip.sk``."""

from __future__ import annotations

from odds_scanner.providers.sk.tipos import TiposProvider


class SynotProvider(TiposProvider):
    name = "synot"
    title = "Synot tip"
    homepage = "https://sport.synottip.sk"
    base_url = "https://sport.synottip.sk"

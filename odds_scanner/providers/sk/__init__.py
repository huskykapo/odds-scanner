"""Providers for Slovak bookmakers' public website endpoints (no API key, no paid service)."""

from odds_scanner.providers.sk.common import SlovakProvider
from odds_scanner.providers.sk.doxxbet import DoxxbetProvider
from odds_scanner.providers.sk.monacobet import MonacobetProvider
from odds_scanner.providers.sk.nike import NikeProvider
from odds_scanner.providers.sk.synot import SynotProvider
from odds_scanner.providers.sk.tipos import TiposProvider

SK_PROVIDERS: dict[str, type[SlovakProvider]] = {
    cls.name: cls for cls in (MonacobetProvider, DoxxbetProvider, NikeProvider, TiposProvider, SynotProvider)
}

__all__ = [
    "DoxxbetProvider",
    "MonacobetProvider",
    "NikeProvider",
    "SK_PROVIDERS",
    "SlovakProvider",
    "SynotProvider",
    "TiposProvider",
]

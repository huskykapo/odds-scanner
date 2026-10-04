from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from odds_scanner.models import Arbitrage


class ArbLog(ABC):
    """Append-only record of every arbitrage found."""

    @abstractmethod
    def append(self, arbs: Sequence[Arbitrage]) -> None: ...

    def close(self) -> None:  # pragma: no cover - optional hook
        pass

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from odds_scanner.models import Arbitrage


class Notifier(ABC):
    """Receives the arbitrages found in one polling cycle (possibly none)."""

    @abstractmethod
    def notify(self, arbs: Sequence[Arbitrage]) -> None: ...

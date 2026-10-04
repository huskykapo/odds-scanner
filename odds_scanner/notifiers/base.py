from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from odds_scanner.models import Arbitrage


class Notifier(ABC):
    """Receives the arbitrages found in one polling cycle (possibly none)."""

    # False: the live engine only passes arbs that are new. True: it passes every current arb
    # and the notifier de-duplicates itself (so a failed send is retried on the next cycle).
    handles_dedupe = False

    @abstractmethod
    def notify(self, arbs: Sequence[Arbitrage]) -> None: ...

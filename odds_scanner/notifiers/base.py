from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from odds_scanner.models import Arbitrage


class Notifier(ABC):
    """Receives the arbitrages it should announce this cycle (possibly none).

    The live engine decides *what* is due (new, returned, ROI moved, ...). A notifier returns the
    arbs it dealt with - delivered, or deliberately skipped (e.g. below its own threshold) - or
    ``None`` meaning all of them. Arbs it could not deliver are left out of the result, so the engine
    offers them again next cycle instead of losing the alert.
    """

    @abstractmethod
    def notify(self, arbs: Sequence[Arbitrage]) -> Sequence[Arbitrage] | None: ...

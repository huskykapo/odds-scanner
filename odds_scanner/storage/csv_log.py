from __future__ import annotations

import csv
from pathlib import Path
from typing import Sequence

from odds_scanner.models import Arbitrage
from odds_scanner.storage.base import ArbLog

MAX_LEGS = 3  # three-way markets are the widest we support

FIELDS = [
    "found_at", "sport", "event_id", "event", "commence_time", "market", "line",
    "profit_percent", "realized_profit_percent", "bankroll", "total_stake", "guaranteed_profit",
] + [f"{col}_{i}" for i in range(1, MAX_LEGS + 1) for col in ("outcome", "bookmaker", "odds", "stake", "payout")]


def _text(value: str) -> str:
    """Neutralise spreadsheet formula injection in third-party text (team names etc.)."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


class CsvArbLog(ArbLog):
    """Appends one row per arbitrage; the header is written when the file is new or empty."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def append(self, arbs: Sequence[Arbitrage]) -> None:
        if not arbs:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self._path.exists() or self._path.stat().st_size == 0
        with self._path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            if write_header:
                writer.writeheader()
            for arb in arbs:
                writer.writerow(self._row(arb))

    @staticmethod
    def _row(arb: Arbitrage) -> dict[str, object]:
        row: dict[str, object] = {
            "found_at": arb.found_at.isoformat(),
            "sport": arb.sport_key,
            "event_id": arb.event_id,
            "event": _text(arb.event_name),
            "commence_time": arb.commence_time.isoformat(),
            "market": arb.market,
            "line": "" if arb.line is None else arb.line,
            "profit_percent": round(arb.profit_percent, 4),
            "realized_profit_percent": round(arb.realized_profit_percent, 4),
            "bankroll": arb.bankroll,
            "total_stake": arb.total_stake,
            "guaranteed_profit": arb.guaranteed_profit,
        }
        for i, leg in enumerate(arb.legs[:MAX_LEGS], start=1):
            row.update(
                {
                    f"outcome_{i}": _text(leg.outcome),
                    f"bookmaker_{i}": _text(leg.bookmaker_title),
                    f"odds_{i}": leg.odds,
                    f"stake_{i}": leg.stake,
                    f"payout_{i}": leg.payout,
                }
            )
        return row

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from odds_scanner.models import Arbitrage
from odds_scanner.storage.base import ArbLog

SCHEMA = """
CREATE TABLE IF NOT EXISTS arbs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    sport TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event TEXT NOT NULL,
    commence_time TEXT NOT NULL,
    market TEXT NOT NULL,
    line REAL,
    profit_percent REAL NOT NULL,
    realized_profit_percent REAL NOT NULL,
    bankroll REAL NOT NULL,
    total_stake REAL NOT NULL,
    guaranteed_profit REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS arb_legs (
    arb_id INTEGER NOT NULL REFERENCES arbs(id),
    position INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    bookmaker_key TEXT NOT NULL,
    bookmaker TEXT NOT NULL,
    odds REAL NOT NULL,
    stake REAL NOT NULL,
    payout REAL NOT NULL,
    PRIMARY KEY (arb_id, position)
);
CREATE INDEX IF NOT EXISTS idx_arbs_found_at ON arbs(found_at);
"""


class SqliteArbLog(ArbLog):
    """Stores arbs in ``arbs`` with their bets in ``arb_legs`` (one row per bet)."""

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(SCHEMA)

    def append(self, arbs: Sequence[Arbitrage]) -> None:
        if not arbs:
            return
        with self._conn:  # one transaction per batch
            for arb in arbs:
                cur = self._conn.execute(
                    "INSERT INTO arbs (found_at, sport, event_id, event, commence_time, market, line,"
                    " profit_percent, realized_profit_percent, bankroll, total_stake, guaranteed_profit)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        arb.found_at.isoformat(), arb.sport_key, arb.event_id, arb.event_name,
                        arb.commence_time.isoformat(), arb.market, arb.line, arb.profit_percent,
                        arb.realized_profit_percent, arb.bankroll, arb.total_stake, arb.guaranteed_profit,
                    ),
                )
                self._conn.executemany(
                    "INSERT INTO arb_legs (arb_id, position, outcome, bookmaker_key, bookmaker, odds, stake, payout)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (cur.lastrowid, i, l.outcome, l.bookmaker_key, l.bookmaker_title, l.odds, l.stake, l.payout)
                        for i, l in enumerate(arb.legs, start=1)
                    ],
                )

    def close(self) -> None:
        self._conn.close()

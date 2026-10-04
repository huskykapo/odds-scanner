"""Read the arb log back for the dashboard's history view.

Every new arb (or a known arb whose odds changed) is appended to the log, so one opportunity can
have several rows. Here rows are grouped per opportunity (match + market + line): first and last
time it was logged, how many times, and the most profitable version with its bets.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any, Callable

from odds_scanner.markets import market_title, outcome_title

MAX_ROWS = 5000  # newest log rows read


def _read_sqlite(path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        arbs = conn.execute(
            "SELECT id, found_at, sport, event_id, event, commence_time, market, line, profit_percent,"
            " realized_profit_percent FROM arbs ORDER BY id DESC LIMIT ?", (MAX_ROWS,)
        ).fetchall()
        if not arbs:
            return []
        legs: dict[int, list[dict[str, Any]]] = {}
        low = arbs[-1][0]
        for arb_id, _pos, outcome, key, title, odds in conn.execute(
            "SELECT arb_id, position, outcome, bookmaker_key, bookmaker, odds FROM arb_legs WHERE arb_id >= ? ORDER BY arb_id, position",
            (low,),
        ):
            legs.setdefault(arb_id, []).append({"outcome": outcome, "bookmaker_key": key, "bookmaker": title, "odds": odds})
    finally:
        conn.close()
    cols = ("id", "found_at", "sport", "event_id", "event", "commence_time", "market", "line", "profit", "realized")
    return [{**dict(zip(cols, row)), "legs": legs.get(row[0], [])} for row in arbs]


def _read_csv(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(newline="", encoding="utf-8") as fh:
        for i, r in enumerate(csv.DictReader(fh)):
            legs = []
            for n in (1, 2, 3):
                if r.get(f"bookmaker_{n}"):
                    legs.append({"outcome": r.get(f"outcome_{n}", ""), "bookmaker_key": "", "bookmaker": r[f"bookmaker_{n}"],
                                 "odds": float(r.get(f"odds_{n}") or 0)})
            rows.append({
                "id": i, "found_at": r.get("found_at"), "sport": r.get("sport"), "event_id": r.get("event_id"),
                "event": r.get("event", ""), "commence_time": r.get("commence_time"), "market": r.get("market", ""),
                "line": float(r["line"]) if r.get("line") else None, "profit": float(r.get("profit_percent") or 0),
                "realized": float(r.get("realized_profit_percent") or 0), "legs": legs,
            })
    rows.reverse()  # newest first, like the SQLite query
    return rows[:MAX_ROWS]


def read_history(
    sqlite_path: str | Path | None,
    csv_path: str | Path | None,
    *,
    leg_extras: Callable[[str, str, float], dict[str, Any]] | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """Grouped history, newest opportunity first. ``leg_extras(key, title, odds)`` adds per-leg fields."""
    rows: list[dict[str, Any]] = []
    try:
        if sqlite_path and Path(sqlite_path).exists():
            rows = _read_sqlite(Path(sqlite_path))
        if not rows and csv_path and Path(csv_path).exists():
            rows = _read_csv(Path(csv_path))
    except (OSError, sqlite3.Error, csv.Error, ValueError):
        return []

    groups: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for r in rows:  # newest first
        key = (r["event_id"], r["market"], r["line"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"last_found": r["found_at"], "first_found": r["found_at"], "times": 0, "best": r}
            order.append(key)
        g["first_found"] = r["found_at"]
        g["times"] += 1
        if r["profit"] > g["best"]["profit"]:
            g["best"] = r

    out = []
    for key in order[:limit]:
        g = groups[key]
        best = g["best"]
        home, _, away = str(best["event"]).partition(" vs ")
        legs = []
        for leg in best["legs"]:
            item = {
                **leg,
                "outcome_label": outcome_title(leg["outcome"], home or None, away or None, best["line"], best["market"]),
                "effective_odds": leg["odds"],
            }
            if leg_extras:
                item.update(leg_extras(leg["bookmaker_key"], leg["bookmaker"], leg["odds"]))
            legs.append(item)
        out.append({
            "event": best["event"],
            "sport": best["sport"],
            "start": best["commence_time"],
            "market": best["market"],
            "market_label": market_title(best["market"], best["line"]),
            "line": best["line"],
            "profit": round(best["profit"], 4),
            "first_found": g["first_found"],
            "last_found": g["last_found"],
            "times": g["times"],
            "legs": legs,
        })
    return out

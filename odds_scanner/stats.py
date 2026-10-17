"""``py start.py stats``: what the scanner's own log says about WHEN and WHERE arbs appear.

Reads the arb log (SQLite if present, else CSV): one row per announced episode. Shows how many were
found how long before kick-off (with average ROI) and which bookmaker pairs produced them, so
decisions such as polling intervals can rest on your own data. Read-only.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from odds_scanner.config import Config

BUCKETS = (  # (label, upper bound in hours before kick-off)
    ("under 1 h", 1), ("1-3 h", 3), ("3-12 h", 12), ("12-24 h", 24), ("1-3 days", 72), ("over 3 days", float("inf")),
)


@dataclass(frozen=True)
class Row:
    found: datetime
    start: datetime
    roi: float
    books: tuple[str, ...]

    @property
    def hours_before(self) -> float:
        return (self.start - self.found).total_seconds() / 3600.0


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def load_sqlite(path: str | Path) -> list[Row]:
    con = sqlite3.connect(str(path))
    try:
        legs: dict[int, list[str]] = defaultdict(list)
        for arb_id, name in con.execute("SELECT arb_id, bookmaker FROM arb_legs ORDER BY arb_id, position"):
            legs[arb_id].append(name)
        rows = []
        for arb_id, found, start, roi in con.execute("SELECT id, found_at, commence_time, realized_profit_percent FROM arbs"):
            rows.append(Row(_dt(found), _dt(start), float(roi), tuple(legs.get(arb_id, ()))))
        return rows
    finally:
        con.close()


def load_csv(path: str | Path) -> list[Row]:
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            books = tuple(rec[f"bookmaker_{i}"] for i in (1, 2, 3) if rec.get(f"bookmaker_{i}"))
            rows.append(Row(_dt(rec["found_at"]), _dt(rec["commence_time"]), float(rec["realized_profit_percent"]), books))
    return rows


def load_rows(cfg: Config) -> tuple[list[Row], str]:
    sqlite_path, csv_path = Path(cfg.storage.sqlite_path), Path(cfg.storage.csv_path)
    if sqlite_path.exists():
        rows = load_sqlite(sqlite_path)
        if rows:
            return rows, str(sqlite_path)
    if csv_path.exists():
        return load_csv(csv_path), str(csv_path)
    return [], ""


def summarise(rows: Iterable[Row]) -> str:
    rows = list(rows)
    if not rows:
        return "No arbitrages logged yet. Let the scanner run (and find some), then run this again."
    by_bucket: dict[str, list[float]] = {label: [] for label, _ in BUCKETS}
    for r in rows:
        h = r.hours_before
        label = next((lab for lab, hi in BUCKETS if h < hi), BUCKETS[-1][0]) if h >= 0 else "after kick-off"
        by_bucket.setdefault(label, []).append(r.roi)
    lines = [f"{len(rows)} logged arbitrage episode(s), {min(r.found for r in rows):%Y-%m-%d} to {max(r.found for r in rows):%Y-%m-%d}", "",
             "How long before kick-off they were found:", f"  {'':12}{'count':>6}{'share':>8}{'avg ROI':>10}"]
    for label, rois in by_bucket.items():
        if rois:
            lines.append(f"  {label:<12}{len(rois):>6}{len(rois) / len(rows):>8.0%}{sum(rois) / len(rois):>9.2f}%")
    pairs = Counter(" + ".join(sorted(set(r.books))) for r in rows)
    lines += ["", "Bookmaker combinations:"]
    lines += [f"  {n:>4}  {name}" for name, n in pairs.most_common(8)]
    if len(rows) < 30:
        lines += ["", f"Only {len(rows)} episodes so far: treat the shares above as a hint, not a conclusion."]
    return "\n".join(lines)


def run(cfg: Config, out: Callable[[str], None] = print) -> int:
    rows, source = load_rows(cfg)
    if source:
        out(f"source: {source}\n")
    out(summarise(rows))
    return 0

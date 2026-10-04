"""Plain-text rendering of arbitrages, used for the console table (and reusable elsewhere)."""

from __future__ import annotations

import sys
from datetime import timezone
from typing import Sequence, TextIO

from odds_scanner.markets import market_title, outcome_title, split_period
from odds_scanner.models import Arbitrage, ArbLeg
from odds_scanner.notifiers.base import Notifier

_LEGACY_LABELS = frozenset({"h2h", "totals", "spreads", "btts", "draw_no_bet"})

HEADERS = ("Profit", "Event", "Starts (UTC)", "Market", "Bet", "Odds", "Bookmaker", "Stake", "Payout")
RIGHT_ALIGNED = {"Profit", "Odds", "Stake", "Payout"}


def money(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def money2(value: float) -> str:
    """Always two decimals (payouts, totals, profit), so columns line up."""
    return f"{value:,.2f}"


def market_label(arb: Arbitrage) -> str:
    base, period = split_period(arb.market)
    if period is None and base in _LEGACY_LABELS:
        if arb.line is None:
            label = arb.market
        elif arb.market == "spreads":
            label = f"spreads (home {arb.line:+g})"
        else:
            label = f"{arb.market} {arb.line:g}"
    else:
        label = market_title(arb.market, arb.line)
    return label + (" [push risk]" if arb.push_possible else "")


def leg_label(arb: Arbitrage, leg: ArbLeg) -> str:
    """Outcome as a reader understands it: ``1 (Košice)`` rather than ``1``."""
    return outcome_title(leg.outcome, arb.home_team, arb.away_team, arb.line, arb.market)


def _rows(arb: Arbitrage) -> list[list[str]]:
    starts = arb.commence_time.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    rows = []
    for i, leg in enumerate(arb.legs):
        first = i == 0
        rows.append(
            [
                (f"{arb.realized_profit_percent:.2f}%" + ("!" if arb.verify_manually else "")) if first else "",
                arb.event_name if first else "",
                starts if first else "",
                market_label(arb) if first else "",
                leg_label(arb, leg),
                f"{leg.odds:.2f}",
                leg.bookmaker_title,
                money(leg.stake),
                money2(leg.payout),
            ]
        )
    return rows


def format_arb_table(arbs: Sequence[Arbitrage], currency: str = "") -> str:
    """Render arbs (one block per arb, one line per bet) as an aligned text table.

    The Profit column is the guaranteed profit % *after stake rounding*, i.e. what the
    shown stakes actually return in the worst case.
    """
    if not arbs:
        return "No arbitrage opportunities found."
    blocks = [_rows(a) for a in arbs]
    widths = [len(h) for h in HEADERS]
    for block in blocks:
        for row in block:
            widths = [max(w, len(c)) for w, c in zip(widths, row)]

    def fmt(cells: Sequence[str]) -> str:
        parts = [c.rjust(w) if h in RIGHT_ALIGNED else c.ljust(w) for h, c, w in zip(HEADERS, cells, widths)]
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * w for w in widths)
    lines = [fmt(HEADERS), rule]
    for arb, block in zip(arbs, blocks):
        lines.extend(fmt(r) for r in block)
        unit = f" {currency}" if currency else ""
        lines.append(f"  -> total stake {money2(arb.total_stake)}{unit}, guaranteed profit {money2(arb.guaranteed_profit)}{unit}")
        if arb.verify_manually:
            lines.append("  !! unusually high profit - verify manually (likely a pricing error or stale odds)")
        lines.append(rule)
    return "\n".join(lines)


class ConsoleNotifier(Notifier):
    def __init__(self, currency: str = "", stream: TextIO | None = None) -> None:
        self._currency = currency
        self._stream = stream

    def notify(self, arbs: Sequence[Arbitrage]) -> None:
        stream = self._stream or sys.stdout
        print(format_arb_table(arbs, self._currency), file=stream, flush=True)

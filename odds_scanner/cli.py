"""Command-line entry point: ``odds-scanner`` / ``python -m odds_scanner``."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import timedelta
from typing import Sequence

from odds_scanner import __version__
from odds_scanner.config import Config, load_config
from odds_scanner.errors import OddsScannerError
from odds_scanner.notifiers import ConsoleNotifier, DedupeCache, Notifier, TelegramNotifier
from odds_scanner.providers import OddsProvider, ReplayProvider, TheOddsApiProvider
from odds_scanner.scanner import Scanner
from odds_scanner.storage import ArbLog, CsvArbLog, SqliteArbLog

log = logging.getLogger("odds_scanner")

EXIT_CONFIG = 2


def setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # urllib3 logs full request URLs at DEBUG, and The Odds API takes its key as a query
    # parameter - never let that reach the console, whatever --log-level says.
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_provider(cfg: Config) -> tuple[OddsProvider, Config]:
    """Create the provider. In replay mode the sports scanned are the ones present in the file."""
    p = cfg.provider
    if p.name == "replay":
        provider = ReplayProvider(p.replay_file, rebase_timestamps=p.replay_rebase_timestamps)  # type: ignore[arg-type]
        return provider, replace(cfg, sports=tuple(provider.sport_keys()))
    return (
        TheOddsApiProvider(api_key_env=p.api_key_env, timeout=p.timeout_seconds, max_retries=p.max_retries),
        cfg,
    )


def build_notifiers(cfg: Config) -> list[Notifier]:
    n = cfg.notifications
    notifiers: list[Notifier] = []
    if n.console:
        notifiers.append(ConsoleNotifier(cfg.currency))
    if n.telegram.enabled:
        notifiers.append(
            TelegramNotifier.from_env(
                n.telegram.token_env,
                n.telegram.chat_id_env,
                dedupe=DedupeCache(timedelta(minutes=n.dedupe_ttl_minutes)),
                currency=cfg.currency,
                max_per_cycle=n.max_per_cycle,
            )
        )
    return notifiers


def build_logs(cfg: Config) -> list[ArbLog]:
    s = cfg.storage
    logs: list[ArbLog] = []
    if s.backend in ("csv", "both"):
        logs.append(CsvArbLog(s.csv_path))
    if s.backend in ("sqlite", "both"):
        logs.append(SqliteArbLog(s.sqlite_path))
    return logs


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="odds-scanner",
        description="Scan bookmaker odds for arbitrage (surebet) opportunities. Alerts only - never places bets.",
    )
    ap.add_argument("-c", "--config", default="config.yaml", help="path to config file (default: %(default)s)")
    ap.add_argument("--once", action="store_true", help="scan once and exit instead of polling forever")
    ap.add_argument("--replay", metavar="FILE", help="read odds from a JSON file instead of the live API (no key needed)")
    ap.add_argument("--bankroll", type=float, help="override bankroll from config")
    ap.add_argument("--min-profit", type=float, metavar="PCT", help="override min profit %% threshold from config")
    ap.add_argument("--log-level", help="override log level (DEBUG, INFO, WARNING, ...)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap.parse_args(argv)


def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    changes: dict[str, object] = {}
    if args.bankroll is not None:
        changes["bankroll"] = args.bankroll
    if args.min_profit is not None:
        changes["min_profit_percent"] = args.min_profit
    if args.log_level:
        changes["log_level"] = args.log_level
    if args.replay:
        changes["provider"] = replace(cfg.provider, name="replay", replay_file=args.replay)
    # Rebuild via the constructor so overrides are validated like file values.
    return replace(cfg, **changes) if changes else cfg


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = apply_overrides(load_config(args.config), args)
        setup_logging(cfg.log_level)
        provider, cfg = build_provider(cfg)
        scanner = Scanner(cfg, provider, notifiers=build_notifiers(cfg), logs=build_logs(cfg))
    except OddsScannerError as exc:
        # Logging may not be configured yet if the config itself was bad.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    log.info(
        "starting (%s provider): %d sport(s), regions=%s, markets=%s, min profit %.2f%%, bankroll %s %s",
        provider.name, len(cfg.sports), ",".join(cfg.regions), ",".join(cfg.markets),
        cfg.min_profit_percent, cfg.bankroll, cfg.currency,
    )
    return scanner.run(once=args.once)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

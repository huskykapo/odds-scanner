"""Command-line entry point: ``odds-scanner`` / ``python -m odds_scanner``."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from dataclasses import replace
from datetime import timedelta
from typing import Sequence

from odds_scanner import __version__
from odds_scanner.arbitrage import BookRule, FinderSettings
from odds_scanner.config import Config, load_config
from odds_scanner.discovery import DiscoveryCache
from odds_scanner.engine import LiveEngine, Source
from odds_scanner.errors import ConfigError, OddsScannerError
from odds_scanner.matching import MatchSettings
from odds_scanner.notifiers import ConsoleNotifier, DedupeCache, Notifier, TelegramNotifier, format_arb_table
from odds_scanner.providers import OddsProvider, ReplayProvider, TheOddsApiProvider
from odds_scanner.providers.sk import SK_PROVIDERS
from odds_scanner.scanner import Scanner
from odds_scanner.storage import ArbLog, CsvArbLog, SqliteArbLog

log = logging.getLogger("odds_scanner")

EXIT_CONFIG = 2


def setup_logging(level: str) -> None:
    # Team and bookmaker names are Slovak ("Niké", "Košice"): never crash on a console or a
    # redirected file whose encoding cannot show them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
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
                min_profit_percent=n.telegram.min_profit_percent,
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


def build_sources(cfg: Config) -> list[Source]:
    """One :class:`Source` per enabled entry under ``providers:``."""
    sources: list[Source] = []
    for name, sc in cfg.providers.items():
        if not sc.enabled:
            continue
        sports = list(sc.sports or cfg.sports)
        if name == "the_odds_api":
            provider: OddsProvider = TheOddsApiProvider(
                api_key_env=str(sc.options.get("api_key_env", cfg.provider.api_key_env)),
                timeout=sc.timeout_seconds,
                max_retries=sc.max_retries,
            )
            sources.append(Source(
                name, "The Odds API", provider, sports, sc.poll_interval_seconds,
                fetch_kwargs={"regions": cfg.regions, "markets": cfg.markets, "bookmakers": cfg.bookmakers},
                quota_floor=cfg.quota.stop_below,
            ))
            continue
        cls = SK_PROVIDERS[name]
        options = dict(sc.options)
        found = DiscoveryCache().found(name)  # sport ids looked up automatically on an earlier run
        if found and not options.get("sample_files"):
            options[cls.SPORT_OPTION] = {**found, **(options.get(cls.SPORT_OPTION) or {})}  # config wins
        sk = cls(
            options=options, timeout=sc.timeout_seconds, max_retries=sc.max_retries,
            retry_backoff=sc.retry_backoff_seconds, min_interval=sc.min_request_interval_seconds,
        )
        supported = [s for s in sports if sk.supports(s)]
        skipped = [s for s in sports if not sk.supports(s)]
        if skipped:
            log.info("%s: no site id known yet for %s", cls.title, ", ".join(skipped))
        sources.append(Source(name, cls.title, sk, supported, sc.poll_interval_seconds, homepage=cls.homepage, skipped_sports=skipped))
    return sources


def finder_settings(cfg: Config) -> FinderSettings:
    return FinderSettings(
        bankroll=cfg.bankroll,
        min_profit_percent=cfg.min_profit_percent,
        max_profit_percent=cfg.max_profit_percent,
        stale_after=timedelta(seconds=cfg.stale_after_seconds),
        stake_rounding=cfg.stake_rounding,
        verify_above_percent=cfg.verify_above_percent,
        book_rules={
            k: BookRule(stake_step=b.stake_step, min_stake=b.min_stake, stake_fee=b.stake_fee, win_tax=b.win_tax)
            for k, b in cfg.bookmaker_settings.items()
        },
    )


def build_engine(cfg: Config) -> LiveEngine:
    sources = build_sources(cfg)
    if not sources:
        raise ConfigError("no provider is enabled (see 'providers:' in config.yaml)")
    m = cfg.matching
    return LiveEngine(
        sources,
        finder_settings(cfg),
        match_settings=MatchSettings.from_raw_aliases(
            m.aliases, time_tolerance=timedelta(minutes=m.time_tolerance_minutes), threshold=m.name_threshold
        ),
        notifiers=build_notifiers(cfg),
        logs=build_logs(cfg),
        dedupe_ttl=timedelta(minutes=cfg.notifications.dedupe_ttl_minutes),
        currency=cfg.currency,
        dashboard_options={
            "refresh_seconds": cfg.dashboard.refresh_seconds,
            "highlight_seconds": cfg.dashboard.highlight_seconds,
            "sports": sorted({s for src in sources for s in src.sports if "_" not in s}),
        },
    )


def run_live(cfg: Config, engine: LiveEngine, *, once: bool, dashboard: bool) -> int:
    from odds_scanner.dashboard import Dashboard, lan_ip

    if once:
        try:
            arbs = engine.run_once()
        finally:
            engine.stop()
        if not cfg.notifications.console:  # the console notifier already printed new arbs
            print(format_arb_table(arbs, cfg.currency))
        elif not arbs:
            print(format_arb_table([], cfg.currency))
        for st in engine.states.values():
            log.info("%s: %s, %d event(s) %s", st.title, st.status, st.events, st.message)
        return 0

    dash = None
    if dashboard:
        try:
            dash = Dashboard(engine.snapshot, cfg.dashboard.host, cfg.dashboard.port)
        except OSError as exc:
            raise ConfigError(f"cannot open the dashboard on port {cfg.dashboard.port}: {exc}") from exc
        dash.start()
        ip = lan_ip()
        print(f"Dashboard: http://localhost:{dash.port}" + (f"   (phone on the same Wi-Fi: http://{ip}:{dash.port})" if ip else ""), flush=True)
        if cfg.dashboard.open_browser:
            import webbrowser

            try:
                webbrowser.open(f"http://localhost:{dash.port}")
            except Exception:  # noqa: BLE001 - no browser available is fine
                pass
    engine.start(analyze_every=min(cfg.dashboard.refresh_seconds, 5.0))
    engine.start_discovery(DiscoveryCache())
    log.info("polling %s - press Ctrl+C to stop", ", ".join(f"{s.title} ({','.join(s.sports) or '-'})" for s in engine.sources))
    try:
        threading.Event().wait()  # until Ctrl+C
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        engine.stop()
        if dash:
            dash.stop()
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="odds-scanner",
        description="Scan bookmaker odds for arbitrage (surebet) opportunities. Alerts only - never places bets.",
        epilog="commands: run (default) - scan and serve the dashboard; probe - check each bookmaker endpoint once; "
        "probe-fortuna - check whether Fortuna's football page contains match data.",
    )
    ap.add_argument("command", nargs="?", default="run", choices=("run", "probe", "probe-fortuna"))
    ap.add_argument("-c", "--config", default="config.yaml", help="path to config file (default: %(default)s)")
    ap.add_argument("--once", action="store_true", help="scan once and exit instead of polling forever")
    ap.add_argument("--no-dashboard", action="store_true", help="do not start the web dashboard")
    ap.add_argument("--port", type=int, help="dashboard port (default from config: 8765)")
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
    if getattr(args, "port", None):
        changes["dashboard"] = replace(cfg.dashboard, port=args.port)
    if args.replay:
        changes["provider"] = replace(cfg.provider, name="replay", replay_file=args.replay)
    # Rebuild via the constructor so overrides are validated like file values.
    return replace(cfg, **changes) if changes else cfg


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "probe-fortuna":
        from odds_scanner.probe import probe_fortuna

        setup_logging(args.log_level or "WARNING")
        return probe_fortuna()
    try:
        cfg = apply_overrides(load_config(args.config), args)
        setup_logging(cfg.log_level)
        if args.command == "probe":
            from odds_scanner.probe import probe

            return probe(cfg)
        if not args.replay:
            return run_live(cfg, build_engine(cfg), once=args.once, dashboard=not args.no_dashboard)
        # Legacy single-provider mode: replay a file in The Odds API format.
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

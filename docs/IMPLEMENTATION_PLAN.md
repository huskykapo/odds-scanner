# Implementation plan: Tipsport SK, Chance SK, Roobet, Stake

Written after inspecting the repository (baseline: 287 tests pass, 5 Slovak providers working).
Read-only odds collection only. Nothing here places, prepares or submits a bet.

## What exists (Phase 1 findings)

| Concern | Where | Notes |
|---|---|---|
| Adapter interface | `providers/base.py` `OddsProvider.fetch_odds(sport, *, regions, markets, bookmakers) -> FetchResult` | Slovak sites subclass `providers/sk/common.py` `SlovakProvider`: implement `_fetch_payloads()` + classmethod `parse()` (pure, unit-testable). |
| Registration | `providers/sk/__init__.py` `SK_PROVIDERS`, `config.py` `default_sources()`, `cli.py` `build_sources()` | A new Slovak provider = new class + one registry line + one config default. |
| HTTP, retries, limits | `providers/sk/http.py` `SiteClient` | 1 request/s per host floor, exponential backoff on network/5xx/429, **401/403 or captcha page raises `BlockedError`** -> provider marked `blocked` and never polled again. No evasion. |
| Data model | `models.py` `Event`, `BookmakerOdds`, `MarketOdds`, `Outcome`, `Arbitrage`, `ArbLeg`, `NearMiss` | `Event.betradar_id`, per-bookmaker `event_name`, `url`, `last_update` already preserved. |
| Normalisation | `markets.py`, `providers/sk/common.py` `build_markets` | Outcome codes 1/X/2, periods (`@reg`), totals lines, double chance. |
| Event matching | `matching.py` | Betradar id first; else same sport + kick-off tolerance + fuzzy names of BOTH teams; swapped home/away rejected; reserve/youth/women markers never fuzzy-matched; alias table in config. Already conservative. |
| Arbitrage engine | `arbitrage/calculator.py`, `arbitrage/finder.py` | 2-/3-way, stake split, rounding per bookmaker step, min stake, fees/tax, stale/started filters. Do not duplicate. |
| Scheduler | `engine.py` `LiveEngine` | One thread per provider (own interval, exponential backoff on failure, other providers unaffected) + one analyzer thread. |
| Dedupe / notifications | `engine.py` + `notifiers/` | Telegram sends each *new* `dedupe_key`; **the key includes the exact odds**, so any odds move re-notifies. |
| Storage | `storage/` | CSV + SQLite log of new arbs; `storage/history.py` reads it for the dashboard. |
| Dashboard | `dashboard.py` + `dashboard.html`, `near_misses.html` | Shows legs, stakes, payout, profit %, price age, links. **No RECHECK, no confidence, no verified/live status.** |

## Gaps against the requested behaviour

1. No Tipsport/Chance/Roobet/Stake adapters.
2. No validation pipeline (candidate -> verify -> recalc -> confirm -> notify) and no
   first_seen/last_seen/verified_at record per opportunity.
3. Notification dedupe is by exact odds (spams when odds drift); needs opportunity-level
   fingerprint + "ROI changed significantly" rule.
4. No adaptive polling (fixed interval per provider regardless of kick-off proximity).
5. No RECHECK endpoint/button, no confidence score, no max-stake support.

## Order of work

1. **Diagnostics first** (this round): `odds_scanner/diagnostics.py` - read-only, polite, stops at the
   first block, never prints secrets. Needed because the sandbox this was built in cannot reach the
   bookmakers; results must come from the machine that will run the scanner.
2. **Tipsport SK, then Chance SK adapters** - only if the diagnostic shows a normal public JSON
   endpoint. Earlier manual check (see `sample_data/sk/README.md`) recorded a 403 bot-check page for
   both; if the diagnostic confirms it, the adapter is NOT built and the blocker is documented.
3. **Roobet and Stake** via a legitimate data source after its real coverage/pricing is tested with a key.
4. Validation pipeline + dedupe fingerprints + storage of first/last seen.
5. RECHECK (backend endpoint re-fetching only the bookmakers of that opportunity, then the dashboard button).
6. Telegram message upgrade, adaptive polling.
7. `docs/ODDS_SOURCES.md` kept current; regression tests for the 5 existing providers stay green throughout.

## Guard rails (apply to every adapter)

- Never bypass captcha/Cloudflare/WAF, geo-blocks, rate limits or authentication; never reuse tokens
  or cookies; never log in. A 401/403/challenge stops that approach and is documented.
- Identify honestly where we control the request (diagnostics use an explicit User-Agent).
- Secrets only from environment variables; never printed, logged or committed.
- Bookmaker buttons only link to a bookmaker page. No bet submission code exists or will be added.

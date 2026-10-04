# odds-scanner

A sports-betting arbitrage ("surebet") **scanner**. It polls an odds aggregator, finds markets where
the best odds from *different* bookmakers add up to a guaranteed profit, and tells you about them.

**It only alerts. It never places bets and never touches a bookmaker account.**

> ## Disclaimer - read this first
>
> * **Odds move fast.** An arbitrage that is visible in the data may already be gone when you look at
>   it, and one leg of a bet can be accepted while the other is rejected or re-priced. A "guaranteed"
>   profit is only guaranteed if *every* leg is placed at the quoted price. Always check every price
>   on the bookmaker's own site before staking anything.
> * **Bookmakers limit and close accounts** they identify as arbing. Expect stake limits, voided bets
>   and restrictions. Check each bookmaker's terms; some prohibit this explicitly.
> * **Results are not guaranteed.** Data can be wrong or stale, markets can be mis-matched (different
>   rules, void/push conditions, extra-time handling), and fees, commission, currency conversion and
>   deposit/withdrawal costs are **not** modelled. Profit % in this tool is before all of those.
> * This is software, not financial advice. You are responsible for what you do with it and for
>   complying with the laws and bookmaker terms that apply to you. Only gamble what you can afford to
>   lose, and not at all if you are underage or gambling is restricted where you live.

## How it works

For every event and market the scanner takes the **best (highest) decimal odds per outcome across all
bookmakers**, remembering which bookmaker offers each one. With odds `o_i`:

```
sum_of_inverses = Σ 1 / o_i
arbitrage exists  <=>  sum_of_inverses < 1          (2 outcomes, or 3 for e.g. football 1X2)
profit %          =  (1 / sum_of_inverses − 1) × 100
stake_i           =  bankroll × (1 / o_i) / sum_of_inverses
```

Example: tennis, 2.10 at AlphaBet on player A and 2.05 at BetaPlay on player B:
`1/2.10 + 1/2.05 = 0.964` → profit `3.73 %`. With a 1000 bankroll you stake 494 / 506 and, whoever wins,
get back about 1037.

Stakes are rounded to a currency unit (`stake_rounding`), and **payouts, total stake and profit are
recalculated from the rounded stakes**, so the table shows what you would actually place.

Things the scanner deliberately refuses to report (false positives are the norm in this game):

| Situation | Handling |
|---|---|
| Best odds all from the same bookmaker | ignored |
| Stale prices (`last_update` older than `stale_after_seconds`, or missing) | ignored |
| Event already started | ignored |
| Odds ≤ 1, NaN or infinite | discarded as bad data |
| A bookmaker that omits an outcome (e.g. suspended Draw) | that bookmaker is ignored for the market, so a 1X2 never degrades into a fake two-way |
| Totals / spreads on different lines (Over 224.5 vs Under 220.5) | grouped by line, never mixed |
| Profit above `max_profit_percent` (default 25 %) | treated as a data error |
| Profit that rounding destroys (tiny bankroll) | dropped |

Whole-number totals/spread lines (e.g. `220.0`) can end in a *push* (stake refunded), which voids the
profit guarantee; such arbs are flagged `[push risk]`.

Supported markets: `h2h` (2- or 3-way, Draw is detected automatically), `h2h_3_way`, `totals`,
`spreads`, `btts`, `draw_no_bet`. Lay markets and other non-exhaustive markets (e.g. double chance,
player props) are not supported on purpose - their outcomes do not sum to certainty.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Get an API key from [The Odds API](https://the-odds-api.com) and export it - **never put it in a file
that gets committed**:

```bash
export ODDS_API_KEY=your-key-here
```

## Run

```bash
# Try it offline first: no key, no network. Replays sample_data/sample_odds.json
odds-scanner --replay sample_data/sample_odds.json --once

# Live, one scan
odds-scanner --once

# Live, polling forever (Ctrl-C to stop)
odds-scanner

# Other options
odds-scanner -c my-config.yaml --bankroll 500 --min-profit 2 --log-level DEBUG
python -m odds_scanner --help
```

Exit codes: `0` ok / interrupted, `2` config or environment error, `3` stopped to protect the API quota
(or it ran out), `4` the API rejected the key.

### Output

* **Console**: a table of current arbs, best profit % first, one block per arb with the bet, odds,
  bookmaker, stake and payout for each leg.
* **Log**: every arb found in every poll is appended to `data/arbs.csv` and/or `data/arbs.db`
  (SQLite: tables `arbs` and `arb_legs`) with timestamp, event, bookmakers, odds, stakes and profit %.
  An arb that persists across polls is logged each time it is seen - that is the history of how long it lived.
* **Telegram** (optional): one message per arb, de-duplicated (see below).

### Telegram alerts

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Send your bot a message, then open `https://api.telegram.org/bot<token>/getUpdates` and read `chat.id`.
3. Export both and enable it in `config.yaml` (`notifications.telegram.enabled: true`):

```bash
export TELEGRAM_BOT_TOKEN=123456:ABC...
export TELEGRAM_CHAT_ID=123456789
```

The same arb (same event, market, line, bookmakers **and odds**) is not sent again within
`dedupe_ttl_minutes`; if the odds move it counts as a new alert. The cache lives in memory, so a restart
forgets it. A failed send is logged (never crashes the scanner, never includes the token) and retried
next poll.

## Configuration (`config.yaml`)

All keys are optional; unknown keys are rejected so typos do not pass silently.

| Key | Default | Meaning |
|---|---|---|
| `sports` | `[soccer_epl]` | Odds API sport keys to scan |
| `regions` | `[eu, uk]` | Bookmaker regions (`us`, `us2`, `uk`, `au`, `eu`) |
| `markets` | `[h2h]` | See supported markets above |
| `bookmakers` | `[]` | Whitelist of bookmaker keys; empty = all |
| `min_profit_percent` | `1.0` | Report arbs with at least this guaranteed profit |
| `max_profit_percent` | `25.0` | Discard bigger "arbs" as bad data (`null` disables) |
| `bankroll` | `1000` | Total to split across the legs |
| `currency` | `EUR` | Display only |
| `stake_rounding` | `1.0` | Round stakes to a multiple of this |
| `poll_interval_seconds` | `300` | Seconds between polls |
| `stale_after_seconds` | `300` | Ignore prices older than this |
| `quota.backoff_below` / `backoff_multiplier` | `100` / `4` | Slow polling when this few requests remain |
| `quota.stop_below` | `10` | Stop rather than dip below this many requests |
| `notifications.console` | `true` | Print the table |
| `notifications.telegram.*` | disabled | `enabled`, `token_env`, `chat_id_env` (names of env vars) |
| `notifications.dedupe_ttl_minutes` | `60` | De-dup window |
| `notifications.max_per_cycle` | `10` | Max Telegram messages per poll |
| `storage.backend` | `csv` | `csv`, `sqlite`, `both` or `none` (+ `csv_path`, `sqlite_path`) |
| `provider.name` | `the_odds_api` | `the_odds_api` or `replay` (+ `api_key_env`, `replay_file`, ...) |
| `log_level` | `INFO` | Python logging level |

The shipped `config.yaml` is the same with comments and slightly more conservative polling (600 s).

### API quota

The Odds API charges **markets × regions credits per sport per request**. The scanner reads the
`x-requests-remaining / used / last` headers on every response and logs them, polls
`backoff_multiplier` times slower when `remaining ≤ quota.backoff_below`, and stops (exit code 3) rather
than let a poll take the quota under `quota.stop_below`. It also backs off exponentially when polls fail
and honours `Retry-After` on HTTP 429.

Budget accordingly: 2 sports × 1 region × 2 markets is 4 credits per poll, so the 500 credits/month of a
free plan last only ~125 polls (check their current pricing). Arbitrage windows are short, so useful
continuous scanning realistically needs a paid plan - the free tier is best for trying things out.

## Project layout

```
odds_scanner/
  providers/    OddsProvider interface, TheOddsApiProvider (live), ReplayProvider (offline JSON)
  arbitrage/    calculator.py (pure math + stake splitting), finder.py (event/market scanning)
  notifiers/    console table, Telegram, de-dup cache
  storage/      CSV and SQLite arb logs
  scanner.py    polling loop, quota handling, error isolation
  cli.py        entry point
  config.py     validated YAML config
sample_data/    sample_odds.json in The Odds API format (real arbs plus deliberate traps)
tests/          pytest suite, fully offline
```

### Adding another data source

Subclass `odds_scanner.providers.OddsProvider`, implement
`fetch_odds(sport, *, regions, markets, bookmakers) -> FetchResult`, and return `Event` objects (see
`odds_scanner/models.py`; `providers/parsing.py` shows the mapping from The Odds API's JSON). Raise
`ProviderError` (or `RateLimitError`, `AuthenticationError`, `QuotaExhaustedError`) on failure. Then
wire it up in `cli.build_provider`. Nothing else needs to change.

### Replay files

`ReplayProvider` reads either a JSON list of events or `{"sport_key": [events...]}` in The Odds API v4
response format. With `replay_rebase_timestamps: true` (default) timestamps are shifted so the newest
`last_update` equals "now", keeping the sample data fresh for the staleness filter. In replay mode the
scanned sports are those in the file, and every market in the file is served regardless of the
`markets` setting.

## Tests

```bash
pytest
```

The suite needs no network or API key. It covers the arbitrage math and stake calculator (equal odds,
odds ≤ 1, missing outcomes, three-way markets, rounding), the finder's false-positive guards, the
providers (HTTP behaviour via fakes and a local server), notifiers, storage, the scanner's quota and
failure handling, and the CLI end to end on the replay provider.

## Known limitations

* No commission/fee modelling (betting exchanges charge commission on winnings).
* Each poll is a snapshot; there is no streaming, and no check that a price is still available.
* Bookmaker market definitions differ in edge cases (overtime, retirement, void rules). The scanner
  matches outcomes by name and line only.
* The Telegram de-dup cache is in memory only.

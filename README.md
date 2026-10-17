# odds-scanner

An arbitrage ("surebet") **scanner for Slovak bookmakers** that runs locally on your PC, fully
automatically. It reads the public odds feeds of **MONACObet, DOXXbet, Niké, Tipos and Synot tip**,
recognises the same match across bookmakers, finds combinations of best prices that guarantee a
profit, works out the stakes and shows them on a local web dashboard (phone-friendly), in the
terminal and optionally on Telegram.

* **No AI and no paid API at runtime.** No API key is needed. The Odds API provider is still in
  the code but is **off** by default.
* **It only alerts.** It never logs in, never places bets and never touches a bookmaker account.
* Tipsport, Chance and Fortuna are **not** scraped. If any site answers with a 403, captcha or
  bot-check page, that provider is shown as **blocked** and is no longer polled. The scanner never
  tries to get around bot protection or access control.

> ## Disclaimer - read this first
>
> * **Odds move fast.** An arb visible in the data may be gone by the time you look at it, and one leg
>   can be accepted while another is rejected or re-priced. The profit is only guaranteed if *every*
>   leg is placed at the shown price. **Always check every price on the bookmaker's own site before
>   betting.** The dashboard shows each price's age in seconds.
> * **Bookmakers limit and close accounts** that bet arbs: stake limits, voided bets and restrictions
>   are normal. Read each bookmaker's terms.
> * Data can be wrong, stale or mis-matched (different rules for overtime, retirements, voids). Arbs
>   above 10 % are flagged **VERIFY MANUALLY** - they are usually pricing errors.
> * Deposit/withdrawal costs and currency conversion are not modelled. Stake fees and win taxes are,
>   if you set them (see `bookmaker_settings`).
> * This is software, not financial advice. You are responsible for complying with the law and the
>   bookmakers' terms. Only gamble what you can afford to lose, and never if you are under 18.

## Setup on Windows

1. Install **Python 3.11 or newer** from <https://www.python.org/downloads/> and tick
   **"Add python.exe to PATH"** in the installer.
2. Download this repository (green "Code" button -> *Download ZIP*, then unzip) or `git clone` it.
3. Double-click **`start.py`**. The first run downloads the few pure-Python libraries it needs
   (`requests`, `PyYAML`, ...) into the `lib` folder; later runs start immediately.
   *Windows Smart App Control blocks downloaded `.bat` files, which is why `start.py` is the
   recommended launcher: it runs through Python itself (signed, allowed) and installs no compiled
   add-ons. Do not turn Smart App Control off - it cannot be turned back on without reinstalling
   Windows.* If double-clicking opens an editor, type `cmd` in the folder's address bar and run
   `py start.py`. (`start.bat`, which uses a virtual environment, still works where `.bat` files are
   allowed.)
4. The dashboard opens in your browser by itself (**<http://localhost:8765>**). Keep the black
   window open while you use it; close it (or press Ctrl+C) to stop the scanner.

To first check that the bookmaker sites answer from your PC, open a terminal (`cmd`) in the folder and
run:

```bat
py start.py probe
```

When Windows asks whether Python may use the network, allow **private networks** - that is what lets
your phone open the dashboard.

Manual setup (any OS):

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (Linux/macOS: source .venv/bin/activate)
pip install -e ".[dev]"           # [dev] adds pytest
python -m odds_scanner            # or ./start.sh on Linux/macOS
```

### Using it from your phone

The dashboard listens on all network interfaces (`dashboard.host: 0.0.0.0`). At start-up the
terminal prints the address to use, e.g.

```
Dashboard: http://localhost:8765   (phone on the same Wi-Fi: http://192.168.1.23:8765)
```

Open that second address on a phone connected to the same Wi-Fi. If it does not load, allow Python
through the Windows firewall for private networks (Windows Security -> Firewall -> *Allow an app
through firewall*). Set `dashboard.host: 127.0.0.1` if you want the dashboard reachable only from
the PC itself. The dashboard is read-only and has no login, so only use it on a network you trust.

## Commands

```bash
python -m odds_scanner                 # scan continuously + dashboard on http://localhost:8765
python -m odds_scanner --once          # poll every provider once, print the arbs, exit
python -m odds_scanner probe           # check each live endpoint once: OK / BLOCKED / ERROR + event counts
python -m odds_scanner probe-fortuna   # one plain request to ifortuna.sk: does the page contain match data?
python -m odds_scanner telegram-test   # guided Telegram setup: bot token -> chat id -> test message
python -m odds_scanner --help          # options: -c CONFIG, --bankroll, --min-profit, --port,
                                       #          --no-dashboard, --log-level DEBUG, ...
```

`start.py`, `start.bat` and `start.sh` pass their arguments through (`py start.py probe`).

Example `probe` output:

```
monacobet  OK       812 football event(s) with usable odds (3.4s)
doxxbet    OK       96 football event(s) with usable odds (0.6s)
nike       OK       57 football event(s) with usable odds (0.9s)
tipos      OK       50 football event(s) with usable odds (0.5s)
synot      BLOCKED  Synot tip: HTTP 403 - access refused (bot protection?)
```

`probe-fortuna` only reports whether the page contains match data (there is no Fortuna parser).

## What you see

**Dashboard** (refreshes every 3 s): one card per arb, best profit first; arbs first seen in the last
2 minutes are highlighted and the newest is tagged **NEWEST**. Each card shows the match, kick-off,
market and profit %, and per leg: bookmaker (a link to the match if the data has one, otherwise to
the site), the bookmaker's own spelling of the match, outcome, odds, **stake** with a **copy**
button, payout and the **age of the price in seconds**. Change the **bankroll** at the top and all
stakes are recalculated in the browser with the same rounding rules (remembered per browser). Filter
by sport. Below: **provider status** (ok / error / blocked, events, last update, notes) and
**matching quality** (how many matches are quoted by 2+ bookmakers, joined by Betradar id or by name,
unmatched events per bookmaker).

**Near misses** (`http://localhost:8765/near-misses`, linked from the dashboard header): the closest
combinations of the best prices from different bookmakers that are **not** arbitrages yet - either
real arbs below `min_profit_percent`, or small guaranteed losses down to `near_miss_percent` (default
3 %). Each shows the gap (e.g. `-1.84 %` = backing everything would lose 1.84 % of the stake;
`0 %` = break-even), the bets, odds, bookmakers and the age of each price. They obey the same data
rules as arbs (fresh prices, complete markets, two bookmakers, not started), so it is a live view of
how close the market is. They are **not** bets to place. Set `near_miss_percent: 0` to switch it off.

**Terminal**: every *new* arb is printed as a table. **Telegram** (optional): one message per new
arb above the threshold, with stakes. **Log**: every new arb is appended to `data/arbs.csv` and
`data/arbs.db` (SQLite tables `arbs` and `arb_legs`). "New" means not seen with the same bookmakers
and odds in the last `notifications.dedupe_ttl_minutes` (60); if a price changes it is a new arb.

## How it works

```
5 provider threads ──> latest events per bookmaker ──> event matching ──> arbitrage finder ──> dashboard
 (own poll interval,                                    (Betradar id,       (best odds per        console
  1 req/s per site,                                      fuzzy names)        outcome, stakes)      Telegram, CSV/SQLite
  retries, block detection)
```

### Providers (`odds_scanner/providers/sk/`)

| Bookmaker | Endpoint | Betradar id | Notes |
|---|---|---|---|
| MONACObet | `GET ibet-monaco.dualsoft.bet/restapi/offer/sk/sport/{S,H,B,T}/mob` | `brMatchId` | whole sport per request (~5 MB for football) → polled every 120 s; `options.league_ids` limits it to some leagues |
| DOXXbet | `POST www.doxxbet.sk/offer/GetOfferList` | `BetradarStatisticsUrn` | `EventDate` is Bratislava local time (converted to UTC, DST-aware) |
| Niké | `GET www.nike.sk/api-gw/nikeone/v1/boxes/search/portal?...menu=/futbal` | – | `hasMoreBets` paging; matched by names + time |
| Tipos | `POST tipkurz.etipos.sk/.../GetWebStandardEvents` | yes | base64 protobuf, read by a built-in schema-less decoder |
| Synot tip | same as Tipos on `sport.synottip.sk` | yes | same parser |

Endpoint details and real captured responses are in `sample_data/sk/`. Every provider: browser
User-Agent, never more than one request per second per site, timeouts, retries with exponential
backoff on network errors / HTTP 5xx / 429, and HTTP 401/403 or a captcha page => **blocked**.

**Sports.** football, hockey, basketball and tennis are configured for MONACObet (sport codes
S/H/B/T) and Niké (menus `/futbal`, `/hokej`, `/basketbal`, `/tenis`). DOXXbet, Tipos and Synot
identify sports by numbers of their own, and only football's was known (`sport: 54`,
`CategoryID: "28"`). **The scanner looks the others up by itself** on the first start: in the
background it tries the candidate numbers on each of these sites (at the normal pace of at most one
request per second) and recognises the sport from the category name in the reply (Tipos/Synot) or
by comparing the matches in the reply with MONACObet's hockey/basketball/tennis matches (Betradar
ids). This takes a few minutes; the dashboard shows "looking up the site's ids ..." meanwhile, and
each sport starts being polled as soon as it is found. Results are saved in
`data/discovered_sports.json` and reused on every start (sports not found are retried the next
day; delete the file to force a new lookup). You can always set an id yourself, which takes
precedence:

```yaml
providers:
  doxxbet: {options: {sport_ids: {football: 54, hockey: <id>}}}
  tipos:   {options: {category_ids: {football: "28", hockey: "<id>"}}}
```

A few request details could not be verified offline and are configurable: Niké's paging parameter
(`options.page_param`, default `page`; paging stops as soon as a page adds nothing new), DOXXbet's
offer days (`options.dates`, default `[TD, TM]`; `TM` = tomorrow is verified, a failing day is
skipped) and MONACObet's tip types beyond 1/2/3/227/228 (`options.tip_types`).

### Markets and normalisation

Every bookmaker's odds are mapped to the same keys before comparing:

| Market | Outcomes | Comes from |
|---|---|---|
| 1X2 (`h2h_3_way`) | 1 / X / 2 | "Zápas" / "Výsledok", MONACObet tips 1/2/3 |
| double chance pairs | 1X vs 2, X2 vs 1, 12 vs X | 1X/12/X2 odds paired with the opposite 1X2 outcome, across bookmakers |
| two-way winner (`h2h`) | 1 / 2 | tennis |
| totals | Over / Under per line | MONACObet 227/228 (`total=2.5`) |

* **Hockey and basketball "Zápas" 1X2 is regulation time** (a draw is possible). Those markets carry
  the label *(regulation time)* (`h2h_3_way@reg`) and are only ever compared with other
  regulation-time markets, never with a full-time / including-overtime market. The same applies to
  the double-chance pairs and totals of those sports.
* Suspended, locked or disabled odds and odds ≤ 1.01 are skipped. A 1X2 missing one outcome is
  dropped entirely, so a suspended draw can never turn into a fake two-way arb.

### Match pages: every bet type that can form a safe arb (football)

The bookmakers' lists only carry the main result. For football matches offered by **2+ bookmakers
that start within 24 hours**, the app also opens the match page on DOXXbet, Tipos and Synot
(`details:` in `config.yaml`: one extra request per match, refreshed every 4 minutes, 3 s apart per
site). MONACObet sends these bets in its list already. Compared, for the whole match and, where
offered, the 1st / 2nd half separately:

| Bet | DOXXbet (Betradar id) | Tipos / Synot (code) | MONACObet (tip type) |
|---|---|---|---|
| 1X2, double chance | uf:1, uf:60, uf:83 | 19, 20, 64 | 1-3, 4-6 |
| Draw no bet | uf:11, uf:64, uf:86 | 21 | – |
| Over/under goals | uf:18, uf:68, uf:90 | 25, 69, 89 | 227/228, 229/230 |
| Home / away team goals over/under | uf:19/20, 69/70, 91/92 | 27/28, 70/71, 90/91 | 355-358, 371-374 |
| Both teams to score | uf:29, uf:75, uf:95 | 36, 74, 94 | 272/273 |
| Odd/even goals | uf:26, uf:74, uf:94 | 33 | – |
| Handicap (two-way, ±0.5, ±1, ±1.5 ...) | uf:16, uf:66, uf:88 | 24, 68 | – |
| 3-way handicap (0:1, 1:0 ...) | uf:14, uf:65, uf:87 | 22, 169 | – |
| First goal (1 / no goal / 2) | uf:8 | 79 | – |
| Most corners | uf:162 | 209 | – |

Every mapping was checked against real captured odds of the same matches at different bookmakers
(a swapped over/under or handicap sign would show up as a large fake arb). Not compared on
purpose: goalscorer / player bets (no bookmaker offers the opposite side), combined bets
("1x2 a počet gólov", multigóly, ...: outcomes overlap or are split differently), card markets
(bookmakers count cards differently) and quarter lines (x.25 / x.75, which split the stake).
Whole-number lines (handicap -1, over 2.0) can refund ("push") and are flagged *push risk*.

`py start.py capture` saves the raw lists plus five match pages per site into a ZIP - send it to a
developer when a site changes its format.

### Event matching (`odds_scanner/matching.py`)

1. Same **Betradar match id** on both sides (MONACObet, DOXXbet, Tipos, Synot) => same match.
2. Otherwise: same sport, kick-off within **15 min**, and **both** team names similar: accents
   stripped, lower-case, club prefixes dropped (FC, FK, MFK, ŠK, MŠK, HC, HK, AS, KFC, ...), then
   token overlap + difflib ≥ **0.8**. "ŠK Slovan Bratislava" = "Slovan Bratislava",
   "AS Trenčín" = "Trenčín", but "Slovan Bratislava" ≠ "Inter Bratislava" ≠ "Slovan Liberec".
   Reserve / youth / women's teams (B, U21, Ž, ...) never match the first team. A pair that only
   matches with home and away **swapped is rejected** (the 1 and 2 bets would be the wrong way round).
3. **Manual aliases** for names that differ too much:

```yaml
matching:
  aliases:
    "Austria": "Rakúsko"
    "Slovan": "ŠK Slovan Bratislava"
```

Unmatched events are logged at DEBUG level (`--log-level DEBUG`); the dashboard shows the counts.

### Arbitrage and stakes

For every match, market and line the best odds per outcome across bookmakers are taken (at least 2
different bookmakers must be involved):

```
inverse sum  = Σ 1 / odds_i              arbitrage  <=>  inverse sum < 1
profit %     = 1 / inverse sum − 1
stake_i      = bankroll × (1 / odds_i) / inverse sum
```

Then each stake is **rounded to that bookmaker's stake step** (default 0.50 EUR) and raised to its
**minimum stake**, and payouts, total stake and the guaranteed profit are **recomputed from the
rounded stakes**. If rounding kills the profit the arb is dropped. Per-bookmaker **stake fee** and
**win tax** (percent) are applied to the odds *before* comparing:
`effective odds = 1 + (odds × (1 − fee) − 1) × (1 − tax)`.

Defaults: minimum profit **0.5 %**, arbs above **10 %** are flagged *verify manually*, above 25 % they
are treated as bad data and dropped. Prices older than 300 s and matches that already started are
ignored.

Example (from the tests): MONACObet FC Košice 1.69, Niké draw 4.60, Niké Komárno 5.50 ->
inverse sum 0.9909, profit 0.92 %. Bankroll 100 -> stakes 59.50 / 22.00 / 18.50, every outcome pays
at least 100.56, guaranteed profit 0.56 EUR after rounding.

## Configuration (`config.yaml`)

The shipped `config.yaml` is commented. Unknown keys are rejected, so typos do not pass silently.
The most useful settings:

| Key | Default | Meaning |
|---|---|---|
| `sports` | `[football, hockey, basketball, tennis]` | Sports polled at every provider |
| `min_profit_percent` | `0.5` | Show arbs with at least this profit |
| `verify_above_percent` | `10` | Flag arbs above this as "verify manually" |
| `max_profit_percent` | `25` | Drop bigger "arbs" as bad data (`null` disables) |
| `bankroll` | `100` | Total split across the legs (the dashboard can override it) |
| `stake_rounding` | `0.5` | Default stake step |
| `stale_after_seconds` | `300` | Ignore older prices |
| `providers.<name>.enabled` | `true` (Odds API: `false`) | Turn a source on/off |
| `providers.<name>.poll_interval_seconds` | `60` (MONACObet `120`) | Seconds between polls of that site |
| `providers.<name>.timeout_seconds` / `max_retries` / `retry_backoff_seconds` | `20` / `2` / `2` | HTTP behaviour |
| `providers.<name>.min_request_interval_seconds` | `1` | Spacing between requests to one site (≥ 1) |
| `providers.<name>.sports` | top-level `sports` | Per-provider sport list |
| `providers.<name>.options` | see `config.yaml` | Site ids per sport, league filter, paging, ... |
| `bookmaker_settings.<key>` | step 0.5, min 0, fee 0, tax 0 | `stake_step`, `min_stake`, `stake_fee`, `win_tax` |
| `matching.time_tolerance_minutes` / `name_threshold` / `aliases` | `15` / `0.8` / `{}` | Event matching |
| `dashboard.host` / `port` / `refresh_seconds` | `0.0.0.0` / `8765` / `3` | Web dashboard |
| `notifications.console` | `true` | Print new arbs |
| `near_miss_percent` | `3` | `/near-misses` list: show non-arbs down to this % loss (`0` = off) |
| `notifications.telegram.*` | disabled | `enabled`, `token_env`, `chat_id_env`, `min_profit_percent` |
| `notifications.dedupe_ttl_minutes` | `60` | Don't re-alert / re-log the same arb within this window |
| `storage.backend` | `both` in `config.yaml` | `csv`, `sqlite`, `both`, `none` |

**Offline demo / your own captures:** `providers.<name>.options.sample_files: {football: path.json}`
makes a provider read a saved response instead of calling the site (then only sports with a sample
are polled).

### Validation, confidence, RECHECK and alerts (how an arb becomes an alert)

A raw arbitrage is only a *candidate*. Before it is announced it becomes an **opportunity**:

1. **Validation** (all must pass): event identity (two different bookmakers, match not started),
   market identity (one market/line, distinct outcomes), **odds freshness** (every price at most
   `validation.max_odds_age_seconds` old), a complete arbitrage (all outcomes covered, inverse odds < 1),
   **minimum stakes** (from `bookmaker_settings`; maximum stakes are not published by these sources, so
   check each bookmaker), and a **recalculation** from the leg odds that must match the reported stakes/profit.
2. **Confirmation:** a fresh poll of *every* bookmaker involved must still show it
   (`validation.confirm_polls`, default 1; 0 = alert at once). This delays an alert by up to the slowest
   involved bookmaker's poll interval, and it removes most false alarms. `--once` scans skip this step.
3. **Notify once:** one opportunity = event + market + line + which bookmaker is used for which outcome.
   `+2.84 % -> +2.90 % -> +2.75 %` is one opportunity and one alert. You are alerted again only if it
   **disappeared and came back** (unseen for `validation.gone_after_seconds`), its **ROI moved** by at least
   `validation.renotify_roi_delta` points (and `renotify_min_interval_seconds` passed), or the **combination
   changed** (another bookmaker for an outcome is a different opportunity). A failed or partial Telegram send is
   not forgotten: it is offered again next cycle.

**Confidence** (shown as HIGH / MEDIUM / LOW with the reasons on hover) is a deterministic score, not AI:
start at 100; -20 if the match was joined by team names + kick-off (not an exact Betradar id; Niké has none);
-5/-15/-30 for prices older than 30/60/120 s; -25 if not yet confirmed; -15 for ROI above 5 % and -40 above
`verify_above_percent`; -15 for a whole-number line (push); capped at 20 if any validation check failed.
80+ is HIGH, 55+ MEDIUM.

**RECHECK ODDS** (button on every card, `POST /api/recheck`): re-fetches *only* the bookmakers (and sport) of that
opportunity, re-runs the whole pipeline and shows **🟢 ARBITRAGE STILL AVAILABLE** (with any changed odds),
**🔴 ARBITRAGE NO LONGER AVAILABLE**, or **⚪ COULD NOT CONFIRM** if any bookmaker could not be re-fetched (it
never claims an arb is valid from stale data). At most one recheck per opportunity every 5 seconds. The
**OPEN <BOOKMAKER>** buttons only open the bookmaker's page in a new tab. Nothing in this program places or
prepares a bet.

Telegram alerts carry the event, market, ROI, bankroll, each bookmaker with the selection, odds and stake,
the guaranteed return and profit, odds age, confidence and (if `notifications.dashboard_url` is set) an
**OPEN DASHBOARD** link.

### Polling speed (adaptive polling) and `stats`

Each bookmaker is polled in its own thread. With `adaptive_polling` on (default) the wait after a
successful poll depends on that bookmaker's *next kick-off*: a match within `soon_hours` (3) -> poll
twice as often (`soon_factor` 0.5, never faster than `min_interval_seconds`, 30 s); nothing within
`far_hours` (24) -> half as often (`far_factor` 2.0, at most `max_interval_seconds`, 300 s); otherwise the
configured `poll_interval_seconds`. A faster tier is never slower than your configured interval and
a slower tier never faster. Failing polls keep their exponential backoff, and the HTTP client never
sends more than one request per second to a site, whatever you configure.

**Want more updates overall?** Lower `poll_interval_seconds` for a bookmaker under `providers:` (e.g. 30), or
shrink `adaptive_polling.min_interval_seconds`. Watch the *Providers* table: if a site starts answering
errors or 403, the scanner backs off / stops polling it, and going faster only makes that more likely.
MONACObet's whole-sport football feed is ~5 MB per poll: restrict it with `options.league_ids` before
polling it faster.

**Daytime only (`active_hours`).** Under `providers:` any source can have `active_hours: "08:00-23:00"` (local time
of the machine running the scanner; windows that cross midnight such as `"22:00-06:00"` work too). Outside the window
that source is not polled at all (the Providers table shows *sleeping* and when it resumes), which saves metered API
requests overnight. Prices from a sleeping source go stale within `stale_after_seconds` and are ignored, so
the scanner never alerts on them.

`py start.py stats` reads your own arb log and shows how long before kick-off arbs were found (with
average ROI) and which bookmaker pairs produced them: use it to decide where extra polling pays off.

### Telegram alerts

The easy way: run `py start.py telegram-test` (or `python -m odds_scanner telegram-test`) and follow
what it prints - it tells you the next missing step each time you run it:

1. **No token yet** - it explains how to create a bot with [@BotFather](https://t.me/BotFather).
   Set the token in PowerShell: `$env:TELEGRAM_BOT_TOKEN = "123456:ABC..."` and run it again.
2. **Token, no chat id** - it lists the chats that have written to your bot (press *Start* in your
   bot first) and prints the command to set `TELEGRAM_CHAT_ID`.
3. **Both set** - it sends a test message to your chat. Then set
   `notifications.telegram.enabled: true` in `config.yaml` and restart the scanner.

`$env:...` only lasts for that PowerShell window. To keep the values for future windows run `setx`
once and open a new window (never put the token in a file that is committed):

```powershell
setx TELEGRAM_BOT_TOKEN "123456:ABC..."
setx TELEGRAM_CHAT_ID "123456789"
```

On Linux/macOS use `export`. Each arb is sent once (same match, market, bookmakers and odds) within
`dedupe_ttl_minutes`; failed sends are retried, never crash the scanner and never log the token.

### The Odds API (optional, off by default)

Set `providers.the_odds_api.enabled: true`, list Odds API sport keys in `providers.the_odds_api.sports`
and set `ODDS_API_KEY`. Its events are matched with the Slovak ones (aliases help with English
names). Its `h2h` market includes overtime, so it is never combined with the Slovak regulation-time
hockey/basketball 1X2. The legacy single-source mode still works offline:
`python -m odds_scanner --replay sample_data/sample_odds.json --once`.

## Project layout

```
odds_scanner/
  providers/sk/   monacobet.py, doxxbet.py, nike.py, tipos.py, synot.py, protobuf.py (decoder),
                  http.py (throttle, retries, block detection), common.py (base class, normalisation)
  providers/      OddsProvider interface, TheOddsApiProvider, ReplayProvider
  matching.py     cross-bookmaker event matching
  markets.py      market keys, regulation-time periods, labels, fee maths
  arbitrage/      calculator.py (stake splitting and rounding), finder.py (arb search)
  engine.py       provider threads + analyzer
  dashboard.py/.html   local web dashboard
  probe.py        probe / probe-fortuna
  notifiers/      console table, Telegram, de-dup cache
  storage/        CSV and SQLite logs
  scanner.py      legacy single-provider loop (--replay)
sample_data/sk/   real captured bookmaker responses + endpoint notes
tests/            pytest suite, fully offline
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

No network needed. Covers each provider's parser against its captured sample, the HTTP client
(throttling, retries, 403/captcha => blocked), the protobuf decoder, matching (Betradar id, Slovak
spellings, near misses, swapped teams), regulation-time vs full-time separation, double chance,
fees and per-bookmaker rounding, the engine, the dashboard server, the CLI end to end on saved
responses, and the original Odds API / replay code.

## Known limitations

* Only the market types above are compared (no handicaps, BTTS, player props, ...).
* Each poll is a snapshot; a price can change between polls (watch the age column).
* Several Slovak bookmakers appear to share an odds supplier, so arbs between them are rare and small.
* Endpoints are unofficial website APIs and can change without notice - run `probe` when something
  stops working.

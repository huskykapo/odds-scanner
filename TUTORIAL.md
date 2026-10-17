# Arb scanner - simple tutorial

A step-by-step guide for Windows (PowerShell). No programming needed.

> **What this is:** a tool that watches the odds of 5 Slovak bookmakers (MONACObet, DOXXbet, Niké,
> Tipos, Synot tip) and shows you when betting on *every* outcome of a match at *different*
> bookmakers would guarantee a profit (an "arbitrage" or "surebet").
> **It only shows you opportunities. It never logs in anywhere and never places bets.**

> **Read this once - it matters.** Odds move fast, so an arb you see may be gone in seconds, and if
> only one of your bets gets accepted you can lose money. Bookmakers limit or close accounts that bet
> arbs. Data can be wrong or out of date. Nothing here is guaranteed or financial advice. **Always
> open each bookmaker's own site and check the price before you bet.** Only bet what you can afford
> to lose, and only if you are 18+ and it is legal where you live.

---

## 1. First-time setup (once)

1. **Install Python 3.11 or newer** from <https://www.python.org/downloads/>. In the installer tick
   **"Add python.exe to PATH"**. (Check it worked: open PowerShell and run `py --version`.)
2. **Get the program.** Download `odds-scanner-update.zip`, then unzip it, for example into your
   Downloads folder. In PowerShell:

   ```powershell
   cd $HOME\Downloads
   Expand-Archive -LiteralPath ".\odds-scanner-update.zip" -DestinationPath . -Force
   cd .\odds-scanner
   ```

   (If the zip has a different name, e.g. `odds-scanner-dashboard.zip`, use that name.)
3. **Check that the bookmaker sites answer from your connection:**

   ```powershell
   py start.py probe
   ```

   The first run downloads a few small libraries (needs internet). You should see five lines with `OK`
   and a number of events. A line saying `BLOCKED` or `ERROR` means that site can't be reached from
   your connection; the scanner will skip it.

## 2. Start it (every time)

```powershell
cd $HOME\Downloads\odds-scanner
py start.py
```

Your browser opens **http://localhost:8765** (type it in yourself if it doesn't).
**Keep the PowerShell window open** while you use it. Press **Ctrl+C** in it to stop.

## 3. What the dashboard shows

| Part | What it means |
|---|---|
| **Arb cards** (top) | One card per opportunity, best profit first. New ones (last 2 minutes) are highlighted. "No arbitrage opportunities right now" is normal - real arbs are rare and short-lived. |
| **Each card** | The match, kick-off time, the market, the profit %, and for every bet: which bookmaker, which outcome, the odds, **how much to stake** (with a *copy* button), the payout, and **how old the price is in seconds**. |
| **Total stake / profit** | Plain summary of what you put in and the minimum you get back. |
| **Bankroll box** (top bar) | Type your total amount (e.g. 200). All stakes are recalculated instantly. |
| **Sport filter** | Show only football, hockey, basketball or tennis. |
| **VERIFY MANUALLY** tag | Profit above 10%. Almost always a pricing error or stale/mismatched data, not a real arb. Be very careful. |
| **History** | Arbs found earlier, grouped per match. |
| **Calculator** | Type odds in by hand to get stakes for any combination you spot yourself. |
| **Providers** | Each bookmaker's status (ok / error / blocked), number of events and how fresh the data is. Hockey, basketball and tennis for Tipos/Synot are looked up once on the first run (a few minutes). |
| **Matching quality** | How many matches are quoted by 2+ bookmakers (only those can contain an arb). |

### Near misses page

Click **Near misses** in the top bar (or open **http://localhost:8765/near-misses**).

It shows the *closest* combinations that are **not** profitable arbs yet, with a gap number:

- `-1.84 %` = betting on everything at these prices would **lose** 1.84% of your stake.
- `0 %` = break-even. `+0.3 %` = a real but tiny arb, below your minimum threshold.
- The closer to 0, the closer the market is to an arb. It is a "radar", **not** a list of bets to place.

Very useful while arbs are rare: it proves the scanner is alive and shows where prices are tight.

## 4. How to act on an arb (carefully)

0. Press **RECHECK ODDS** on the card first. It re-fetches the current odds of just those bookmakers and
   tells you 🟢 still available / 🔴 no longer available / ⚪ could not confirm. Only continue on 🟢.
   (The **OPEN <BOOKMAKER>** buttons just open the bookmaker's site; nothing is ever bet for you.)
1. Look at the **price age** on each leg. Old (red) prices are risky.
2. Open **every** bookmaker in the card (the names are links) and **find the match**.
3. Check that the **odds are still the same** (or better) on **every** bookmaker. If any one changed, skip it.
4. Make sure it is the **same market** (e.g. full-time result vs. including overtime; same line for
   totals/handicaps). A tag "push risk" means a whole-number line that can be refunded - the
   guarantee does not hold then.
5. Only if everything matches, place the bets. Place the **riskiest/fastest-moving leg first**.
6. Never bet more than you can lose. Expect small profits and occasional account limits.

## 5. Telegram alerts (get a message on your phone)

Set up once. On your phone, in Telegram:

1. Search for **@BotFather**, open it, send `/newbot`, choose a name and a username ending in `bot`.
2. BotFather sends a **token** like `123456789:ABC-def...`. Copy it.
3. Open your new bot and press **Start** (or send it any message).

Then in PowerShell, in the scanner folder:

```powershell
$env:TELEGRAM_BOT_TOKEN = "PASTE-YOUR-TOKEN"
py start.py telegram-test
```

It lists the chats that wrote to your bot and prints a line like `$env:TELEGRAM_CHAT_ID = "123456789"`.
Run that line, then run `py start.py telegram-test` once more: you should get a **test message** in
Telegram.

Now make it permanent and turn it on:

```powershell
setx TELEGRAM_BOT_TOKEN "PASTE-YOUR-TOKEN"
setx TELEGRAM_CHAT_ID "123456789"
notepad config.yaml
```

In `config.yaml` find the `telegram:` part and change `enabled: false` to `enabled: true`, save,
**close PowerShell, open a new one**, and start the scanner again. Every new arb is sent once with
the stakes. Your token is a password: never share it or put it in a file you share.

## 6. Settings you may want to change

Open the settings file with `notepad config.yaml`, edit, save, and restart the scanner.

| Setting | Default | What it does |
|---|---|---|
| `bankroll` | 100 | Starting total to split across the bets (you can also change it live on the dashboard). |
| `min_profit_percent` | 0.5 | Ignore arbs below this profit %. |
| `near_miss_percent` | 3 | How far from an arb the Near misses page goes. `0` switches it off. |
| `verify_above_percent` | 10 | Arbs above this get the "VERIFY MANUALLY" tag. |
| `stale_after_seconds` | 300 | Ignore prices older than this. |
| `bookmaker_settings` | step 0.5, min 1 | Per bookmaker: smallest stake step, minimum stake, stake fee %, tax on winnings %. Set these to match your real accounts so stakes are realistic. |
| `dashboard: host` | `0.0.0.0` | Change to `127.0.0.1` so only your own computer can open the dashboard (default also lets your phone on the same Wi-Fi open it). |
| `storage: backend` | both | Every found arb is saved to `data\arbs.csv` (open in Excel) and `data\arbs.db`. |

## 7. Updating when new features arrive

Your friend and I both improve the same program on GitHub. A new version arrives as a new zip.
Unzip it **over** your `odds-scanner` folder (your `lib` and `data` folders are kept), then start as
usual. If you use git: `git pull`.

## 8. If something goes wrong

| Problem | Fix |
|---|---|
| `py` is not recognised | Reinstall Python and tick "Add python.exe to PATH", then open a new PowerShell. |
| `Expand-Archive ... does not exist` | The zip is not in that folder. Run `Get-ChildItem $HOME\Downloads *odds-scanner*` to see the exact name. |
| Port 8765 already in use | `py start.py --port 8780` and open http://localhost:8780. |
| A bookmaker shows `blocked` or `error` | That site can't be reached or refused automated requests. It is skipped and never retried automatically. Try `py start.py probe` again later. |
| Dashboard shows 0 arbs for hours | Normal. Check the Near misses page; leave it running during busy match days, or set up Telegram so you don't have to watch. |
| Windows blocks the script | Use `py start.py` (not `.bat`). Don't turn off Smart App Control. |
| No Telegram test message | Press **Start** in your bot's chat, re-check the token, and re-run `py start.py telegram-test` - it tells you what is missing. |

## 9. Command cheat sheet

```powershell
py start.py                 # run the scanner + dashboard
py start.py probe           # check every bookmaker site once
py start.py telegram-test   # set up / test Telegram alerts
py start.py --once          # scan once, print results, exit
py start.py --port 8780     # use another port
py start.py --help          # all options
```

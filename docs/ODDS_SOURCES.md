# Odds sources

Read-only odds collection. The scanner never logs in, never places or prepares a bet, and never
bypasses CAPTCHA, Cloudflare/WAF, rate limits, geo-restrictions or authentication. A 401/403/challenge
stops that approach for that bookmaker; it is documented here, not worked around.

**How to read the "tested" column.** *Tested* = observed by us with a real request. *Advertised* =
a vendor's own marketing or a search summary; **not verified**. This document was written in a build
environment whose network proxy refuses connections to bookmaker and odds-API hosts, so live
verification has to be done on the machine that runs the scanner:

```
py start.py diagnostics all          # or:  diagnostics tipsport | chance | roobet | stake | nike | ...
```

Last documentation update: 2026-10-05.

---

## Summary

| Bookmaker | Status | Source | Public? | Free |
|---|---|---|---|---|
| MONACObet, DOXXbet, Niké, Tipos, Synot tip | **WORKING** (tested by the project owner on 2026-10-04/05) | the site's own public JSON endpoints | yes, anonymous | yes |
| Tipsport SK | **BLOCKED** (tested 2026-10-05) | public `/rest/offer/...` (research lead) | no: the home page itself returns a 403 bot-check | n/a |
| Chance SK | **BLOCKED** (tested 2026-10-05) | same platform as Tipsport (lead) | no: the home page itself returns a 403 bot-check | n/a |
| Roobet | **NO SOURCE SELECTED** | third-party aggregator (advertised) | no official API found | free tiers unlikely to include it |
| Stake | **NO SOURCE SELECTED** | third-party aggregator (advertised) | no public odds API | free tiers unlikely to include it |

---

## The five working Slovak bookmakers

| | MONACObet | DOXXbet | Niké | Tipos | Synot tip |
|---|---|---|---|---|---|
| Endpoint | `ibet-monaco.dualsoft.bet/restapi/offer/sk/sport/<S/H/B/T>/mob` | `www.doxxbet.sk/offer/GetOfferList` (POST) | `www.nike.sk/api-gw/nikeone/v1/boxes/search/portal` | `tipkurz.etipos.sk/.../GetWebStandardEvents` (POST, protobuf in JSON) | same platform as Tipos on `sport.synottip.sk` |
| Auth | none | none | none | none (any 32-hex token) | same as Tipos |
| Pre-match / live | pre-match | pre-match | pre-match (live filtered out) | pre-match | pre-match |
| Markets | 1X2 / winner, double chance, totals, extras from match pages | same | same | 1X2, double chance | same |
| Poll interval (default) | 120 s (whole-sport feed ~5 MB) | 60 s | 60 s | 60 s | 60 s |
| Limits | our client: max 1 request/s per host, backoff on 429/5xx | same | same | same | same |
| Betradar match id | yes | yes | no | yes | yes |
| Risk | undocumented endpoints can change or start blocking at any time; site terms may restrict automated access | | | | |
| Fallback | The Odds API provider (in the code, off by default) or an aggregator | | | | |

Source of the endpoint details and sample responses: `sample_data/sk/README.md`.

---

## Tipsport SK  (priority 1)

| | |
|---|---|
| Source | Public site `https://www.tipsport.sk`, REST paths `/rest/offer/v4/sports`, `/rest/offer/v2/offer`, `/rest/offer/v2/search`, `/rest/offer/v3/sports/COMPETITION/{id}/matches`, `/rest/offer/v3/matches/{id}/communityStats` (**leads from an older open-source client; not assumed to still work**) |
| Public vs authenticated | unknown. Earlier manual check on 2026-10-04 (`sample_data/sk/README.md`): `POST /rest/offer/v2/offer?limit=75` -> **403 bot-check page**, tested from inside a browser with cookies omitted |
| Pre-match / live | unknown (not testable until the endpoint is reachable) |
| Tested by diagnostics | **2026-10-05, from the project owner's own PC and connection: `BLOCKED`.** 1 request (`GET https://www.tipsport.sk/`, honest User-Agent): HTTP 403, `text/html`, 74029 bytes, a captcha/bot-check page; the site set 1 cookie. The probe stopped at once: no endpoint was tried, nothing retried, no workaround attempted. Matches the earlier manual check of 2026-10-04 |
| Polling plan if it works | pre-match 60-120 s, 1 request/s ceiling |
| Known restrictions | **The site refuses automated clients at the home page.** Per project rules this approach ends here: no cookie reuse, header or User-Agent tricks, headless-browser evasion, proxies or retries. No adapter will be built for this source |
| Fallback | an odds aggregator that licenses Tipsport data (not yet researched), or leave Tipsport out |

**What the older open-source client documents** (`stepankarlovec/tipsport`, tipsport.cz, PHP; read 2026-10-05):
GET the home page and read the site's own `JSESSIONID` cookie, then call `GET /rest/offer/v4/sports` (tree in
`data.children`), `GET /rest/offer/v1/competitions/top`, `POST /rest/offer/v2/offer` (JSON keys `results,
highlightAnyTime, limit, type, id, fulltexts, matchIds, matchViewFilters`), `GET /rest/offer/v2/search?searchText=
&includePrematch=true&includeResults=false` (key `results`), `GET /rest/offer/v3/sports/COMPETITION/{id}/matches?
fromResults=false`, `GET /rest/offer/v3/matches/{id}/communityStats?...`. It sets no User-Agent and no login. It does
**not** show what a match or an odds entry looks like, so a parser cannot be written from it.

**Why no adapter yet:** writing a parser against an unseen structure risks showing wrong prices as arbitrage.
`py start.py diagnostics tipsport --save tipsport_samples` stores the public JSON bodies of the working probes
(never cookies/headers); the adapter is then written and tested against that real data.

**Outcome:** BLOCKED on 2026-10-05, so no adapter is built. Re-run `py start.py diagnostics tipsport` occasionally; only a
normal, anonymous 200 response would reopen this.

## Chance SK  (priority 2)

| | |
|---|---|
| Source | Public site `https://www.chance.sk` (group sibling of Tipsport; same REST paths were the lead) |
| Tested by diagnostics | **2026-10-05, from the project owner's own PC: `BLOCKED`.** 1 request (`GET https://www.chance.sk/`, honest User-Agent): HTTP 403, `text/html`, 3754 bytes, a captcha/bot-check page; the site set 1 cookie. The probe stopped at once: nothing retried, no workaround attempted. Matches the earlier manual check of 2026-10-04 |
| Public vs authenticated | not observed (blocked before any endpoint was reached) |
| Pre-match / live | unknown |
| Known restrictions | The site refuses automated clients at the home page. Per project rules this approach ends here (no cookie reuse, header or User-Agent tricks, headless-browser evasion, proxies or retries). No adapter will be built for this source |
| Fallback | an odds aggregator that licenses Chance data (not yet researched), or leave Chance out |

Re-run `py start.py diagnostics chance` occasionally; only a normal anonymous 200 response would reopen this.

## Roobet  (priority 3)

| | |
|---|---|
| Official API | none found. No developer portal or documented odds API turned up in research on 2026-10-05 |
| Candidate sources | third-party odds aggregators, e.g. OddsPapi, Odds-API.io, others |
| What is **advertised** (not verified) | OddsPapi's own site/blog lists Roobet among "crypto and offshore books" under "350+ bookmakers". A second search summary of the same vendor said Roobet coverage was *not confirmed*. **Conflicting; unresolved** |
| Free tiers (advertised) | OddsPapi: 250 requests/month (~8/day), paid from ~$49/month, WebSocket on paid tiers. Odds-API.io: free plan = 2 "recreational" bookmakers, 100 requests/hour; sharp/exchange books need a paid plan; WebSocket costs extra; new free keys reported as paused |
| Is Roobet on the free plan? | **unknown; probably not** (free plans restrict which bookmakers you get) |
| Suitability for arbitrage | 250 requests/month cannot support continuous scanning. 100 requests/hour is ~1 request per 36 s, workable for a few sports *if* Roobet is included |
| Decision | **No provider is hard-coded.** Before choosing: sign up for a free key, list the bookmakers it actually exposes, confirm Roobet, sports/markets, update frequency and limits, and record the result here |
| Key handling | environment variable only (e.g. `ROOBET_ODDS_API_KEY`); never committed, never printed (diagnostics only reports "set"/"not set") |
| Fallback | none; Roobet stays out |

## Stake  (priority 4)

| | |
|---|---|
| Official API | **no documented public odds API** found. Search results describe unofficial wrappers/GraphQL clients that can also place bets and manage accounts: **not used**, they are authenticated/account automation and against the project's rules |
| Candidate sources | aggregators advertising Stake odds: OddsPapi, SharpAPI, OpticOdds, Betstamp (advertised) |
| Sports / markets (advertised) | match winner, over/under, Asian handicap, BTTS, player props, live markets |
| Free tier | same caveat as Roobet: unknown whether Stake is included on any free plan |
| Decision | **No adapter** until a legitimate source's real Stake coverage and limits have been tested |
| Fallback | none |

---

---

## Research log: legitimate routes to Tipsport SK, Chance SK, Roobet, Stake (2026-10-05)

Everything below comes from search summaries or vendor pages that could not be opened from the build
environment. It is **advertised, not verified**. No key was available, so nothing was tested live.

| Candidate | Claims (advertised) | Price / limits (advertised) | Caveats |
|---|---|---|---|
| **SportMonks** Premium Odds Feed (built with TXODDS) | bookmaker list includes **Tipsport-SK** and IFortuna; "Slovak and German variants are covered, Czech variants (Tipsport CZ, Fortuna CZ, Chance, SynotTip) are not" | Premium Odds Feed ~EUR 129/month on top of a base plan (from EUR 29/month); 14-day trial on paid plans; free plan limited to two leagues | pre-match odds; football-centred; whether **Chance SK** is carried is unclear; update delay unknown |
| **PulseScore** | Tipsport pre-match + live, 18 sports | not checked | describes **Tipsport (Czech)**, not tipsport.sk |
| **OddsPapi** | 350+ bookmakers; its own pages list Stake and Roobet | free 250 requests/month; paid from ~USD 49/month; WebSocket on paid tiers | one search summary said Roobet coverage was not confirmed; Tipsport/Chance coverage not found |
| **Odds-API.io** | 265-365+ bookmakers, REST + WebSocket | free: 2 "recreational" bookmakers, 100 requests/hour (new free keys reported paused); paid ~USD 65+/month; WebSocket costs extra | sharp/exchange books need a paid plan; Roobet/Stake/Tipsport coverage not confirmed |
| **SharpAPI, OpticOdds, Betstamp** | advertise Stake odds | not checked | Roobet/Tipsport/Chance not found |
| **Apify "Tipsport Betting Odds Scraper"** | a "Tipsport XML" scraper | USD 5.99/month + usage; marked DEPRECATED | how it gets the data is not stated; third-party scrapers often rely on proxy/anti-bot techniques that this project will not use |

**Why a bot can "have" Tipsport and Chance when the sites refuse our client.** Plausible explanations,
none verified: (1) it buys a licensed aggregator feed; (2) it runs scraping behind residential
proxies / browser-automation that evades bot protection (not acceptable here); (3) the bot's data is
Czech Tipsport, not tipsport.sk. Identify the bot before assuming which.

**Consequences of an aggregator feed.** Prices are delayed relative to the bookmaker's own site, so
arbs from it are likelier to be stale; a RECHECK can only re-query the same aggregator, not the bookmaker,
and every bet must be verified by hand on the bookmaker's site.

**Test plan (cheapest first, each recorded here with a date and the real numbers):**
1. OddsPapi free key: list the bookmakers the key returns; look for Roobet, Stake, Tipsport, Chance.
2. SportMonks 14-day trial: look for Tipsport-SK / Chance in the bookmaker list and check update delay.
3. Odds-API.io free key: check its bookmaker list.
Only a source that actually returns the bookmaker gets an adapter.

### OddsPapi coverage - TESTED 2026-10-05 (project owner's PC, free key)

`py start.py diagnostics oddspapi` -> HTTP 200, **720 bookmakers visible** to a free key (1 request).

| Looked for | Result |
|---|---|
| Roobet | FOUND (`Roobet`, `roobet`) |
| Stake | FOUND (`Stake.com`, `stake`, `Stake BR`, `stake.bet.br`; also `MyStake` / `mystake`, a different brand) |
| MyStake | FOUND (`MyStake`, `mystake`) |
| Tipsport, Chance, Synot | **not in the list** |
| Fortuna | only Fortuna PL / RO (not SK) |
| Pinnacle | FOUND (several variants, incl. `pinnacle+5` / `pinnacle+30` = delayed feeds) |

Listed does not prove the free plan returns their odds. Next test: `py start.py diagnostics oddspapi --save oddspapi_samples`
(2 requests of the 250/month): fetches one football fixture with Roobet/Stake/MyStake/Pinnacle odds and saves the
responses so the adapter can be written from the real format.

## Cost outlook

| Goal | Realistic option |
|---|---|
| Keep running for free | the 5 working Slovak sources |
| Add Roobet/Stake | an aggregator plan that actually includes them; likely a paid tier (tens of USD per month), to be confirmed |
| Continuous scanning of an aggregator | a plan with at least ~1 request per 30-60 s per sport, or WebSocket streaming |

## Changelog
- 2026-10-05: Tipsport SK and Chance SK tested from the owner's PC: both BLOCKED (403 bot-check at the home page). Initial version; diagnostics added; no live verification of Tipsport/Chance/Roobet/Stake possible from the build environment.

# Multi-country Discord service - research and plan (2026-10-17)

Status: **research and planning only. Nothing is built.** Facts below come from web search results (vendor blogs, policy pages
read through a search tool; the sandbox cannot open most of these sites), so every item marked *verify* must be checked on the live page
or with the vendor before money or customers are involved. Not legal advice.

## 1. The idea

One scanner, many audiences. A member picks a country in Discord and gets a role. Channels are visible per role, and only arbs whose
**every leg** is bookable by that audience are posted there. Combinations: `GB`, `SK`, `GB + international` (Stake, Roobet, MyStake...).
Subscription tiers decide which roles/channels a member can see.

## 2. Free API for testing - recommendation

None of the free tiers can *run* a multi-country service (they are 250-500 calls a month); they can only prove the routing logic.

| Provider | Free tier (as advertised) | Fit for this plan |
|---|---|---|
| **The Odds API** | 500 credits/month; a call costs markets x regions credits; about 40 soft bookmakers grouped by region (UK, EU, US, AU); no sharp books | **Best test source**: region model matches our audience model (`uk`, `eu`, `us`, `au`). 500 credits is about 80 multi-market calls, enough to verify routing, not to scan |
| OddsPapi | 250 requests/month, each returns the full fixture board, 350+ books incl. Pinnacle and crypto | Good for a one-off broad snapshot (all regions in one call); login was flaky for you earlier |
| Odds-API.io | You saw no free plan; a vendor blog claims 100 req/h with 2 books (conflicting, *verify*) | Paid only in practice |
| SharpAPI | 12 req/min, 2 books, US-focused, reported 60 s delay | Not useful for multi-country |
| SportsGameOdds | sources disagree (9 books, delayed) | Skip |

**Plan for testing: no API cost at all for most of it.** Build and test the region routing on recorded fixtures (we already have the replay
provider and sample files). Use The Odds API free key only for 2-3 live sanity calls (one per region).

## 3. Blocking questions (decide before building the paid part)

1. **Data licence.** Every aggregator bans reselling or redistributing its data (OddsPapi: "may not resell, repackage, or redistribute our
   data as a standalone product"; Odds-API.io section 9: no resale/redistribution/sublicensing without written consent; The Odds API allows
   display in apps/sites but not a standalone raw-data product). Posting arbs (derived data) to paying members is probably "display", but it
   is a grey zone. **Ask each vendor in writing** before selling anything. OddsPapi says its enterprise tier covers redistribution (*verify*).
2. **Our direct Slovak feeds are scraped from bookmaker sites.** Fine for personal use; selling alerts built on scraping bookmaker sites is a
   different ToS/legal position. Needs a decision (licensed feed instead, or accept the risk).
3. **Discord rules.** Discord's Monetization Policy lists "facilitation of gambling or gambling-adjacent activities", with examples
   including gambling advice, as content that may not be monetized. So **do not use Discord's built-in server subscriptions**; paid
   betting servers use external payment tools, but the server can still be actioned (*verify live policy*). Mitigation: keep the engine
   channel-agnostic (notifiers already are) so Telegram or a website can replace Discord without a rewrite.
4. **Payments.** Stripe's restricted-business rules treat gambling as restricted/prohibited and sources disagree on tips/handicapping
   services (*verify with Stripe directly*). Alternatives (Whop, LaunchPass, other processors) have their own rules; none checked.
5. **Per-country law and advertising.** UK: operators advertised to British consumers must hold a Gambling Commission licence and ads follow
   the CAP Code (ASA). The `GB + international` tier points UK members at unlicensed crypto casinos; I believe Stake.com and Roobet are not
   UKGC-licensed (*verify*), which is the riskiest part of the plan. Other countries differ. Get a gambling lawyer's view for each launch country.
   Also: 18+ gate, responsible-gambling notice, no "guaranteed profit" claims (bookmakers limit arbers; settlement and stale-price risk).

## 4. Technical design

**Region model** (new `regions.yaml`): each bookmaker has `available_in: [GB, IE, ...]` or `international: true`. An *audience* is a named
set of bookmakers: `GB` = GB books, `GB+INTL` = GB books + international books, `SK`, etc. The bookmaker-to-country map comes from the
aggregator bookmaker lists (which carry region tags) plus hand-curation, and needs periodic review.

**Analysis**: fetch once, analyse per audience. Running `find_arbitrages` per audience on only that audience's books is better than
filtering global results: a global best combo may use a book the audience cannot use, while a different valid combo exists inside their
set. Cost is small (same events). The opportunity registry keys fingerprints per audience, so dedupe, RECHECK and renotify work unchanged.

**Delivery**: one `DiscordNotifier` per audience posting to that audience's channel via a **channel webhook** (no gateway connection needed
for posting; simplest and robust). Reuse `format_message` layout with an embed. Channel layout, e.g. category `GB`: `#gb-arbs`,
`#gb-near-misses`; category `GB+INTL`: `#gb-intl-arbs`; same for `SK`. Category permissions hide each category from members without the role.

**Roles and tiers**: country picker = a persistent button/select view in a `#pick-your-country` channel (discord.py persistent views:
`timeout=None`, fixed `custom_id`s, re-registered at startup). Discord's own Onboarding/"Channels & Roles" can do plain role picking with
zero code; a custom bot is only needed once paid-tier gating is added. Tier = extra role set by the payment webhook (member pays -> webhook
-> bot grants role; cancel/expiry -> bot removes it). Suggested tiers: **Free** (one country, delayed e.g. 5 min, ROI capped, no RECHECK),
**Basic** (own country, real time), **Pro** (country + international, near-misses, RECHECK links). Delay and ROI cap are per-audience
settings, so tiers are config, not code.

**Cost scaling**: aggregator cost depends on books/regions covered, not on customer count (one fetch serves everyone), so margins improve
with members. Reference prices (advertised): Odds-API.io GBP 49 (2 books) to 229 (15 books) per month; The Odds API about USD 30 for 20k
credits; Betburger prematch EUR 79.99 (see ODDS_SOURCES.md).

## Status (2026-10-24)

Done, for free testing: Discord webhook alerts with per-channel bookmaker sets (`notifications.discord`),
per-channel arb search in the engine (phase 1 without a separate regions file: the channel lists are the
audiences), `discord-test`, and the OddsPapi source for Stake/Roobet/MyStake (free plan, 240-request
monthly budget). Not done: role-picker bot, payments/tiers, other countries' bookmakers.

## 5. Phases

0. Decisions (section 3): written answers from the data vendor(s), Discord/payment rules, lawyer for launch countries.
1. Region model + per-audience analysis, tested on fixtures (no API cost). Includes config validation and tests.
2. `DiscordNotifier` (webhook) + channel layout script/doc; dry-run mode that prints instead of posting.
3. Role-picker bot (country selection) in a private test server.
4. Live test with The Odds API free key (one call per region); compare against real bookmaker sites by hand.
5. Payment webhook -> tier roles; tier settings (delay, ROI cap, features).
6. Hardening: rate limits, secrets via env vars only, audit log, kill switch, status channel, disclaimers.

Standing project rules still apply: alerts only, never place bets, no bypassing access controls, secrets only in env vars.

# Slovak bookmaker samples

Real responses captured on 2026-10-04 from each bookmaker's public website,
trimmed to a few matches. Used to build and test parsers offline.

| Bookmaker | Endpoint | Works without browser session? | Format | Betradar match id |
|---|---|---|---|---|
| Niké | `GET https://www.nike.sk/api-gw/nikeone/v1/boxes/search/portal?betNumbers&date&live=false&menu=/futbal&minutes&order&prematch=true&results=false` | yes | JSON | no (own ids) |
| DOXXbet | `POST https://www.doxxbet.sk/offer/GetOfferList` body `{"sportEvent":-1,"sport":54,"region":-1,"leaugeCup":-1,"live":-1,"date":"TM","top":1,"streamOnly":-1}` | yes | JSON | yes, `BetradarStatisticsUrn` |
| Tipos | `POST https://tipkurz.etipos.sk/WebServices/Api/SportsBettingService.svc/GetWebStandardEvents` body `{"LanguageID":17,"Token":"<any 32 hex chars>","CategoryID":"28","Top":50,"IncludeLiveCategories":false}` | yes | JSON wrapping base64 protobuf | yes |
| Synot | same as Tipos on `https://sport.synottip.sk` (same platform) | yes | JSON wrapping base64 protobuf | yes |
| Tipsport | `POST https://www.tipsport.sk/rest/offer/v2/offer?limit=75` | **no** - 403 bot-check page | JSON | no |
| Chance | same platform as Tipsport (`www.chance.sk`) | **no** - 403 bot-check page | JSON | no |
| Fortuna | odds are rendered server-side into the page; scripted requests got an empty page | **no** | HTML | ? |

"Works without browser session" was tested from inside a browser with cookies
omitted. A Python client may still be treated differently (TLS fingerprint,
rate limits) - verify on the machine that will run the scanner.

Market notes:
- Hockey and basketball "1X2"/"Zápas" at these bookmakers is **regulation time**
  (draw possible). Never compare it with The Odds API `h2h`, which includes overtime.
- Team names are in Slovak ("Rakúsko", "Nemecko").
- DOXXbet `EventDate` has no timezone; it is Europe/Bratislava local time.
- Niké, DOXXbet and Tipos/Synot all also list 1X / 12 / X2 double-chance odds.

"""``capture``: save the raw responses the app receives from each bookmaker into one ZIP file.

Used to add new bet types (over/under, both teams to score, handicaps, ...): the saved responses
show where each site puts them. Only public odds data is saved - nothing about you or your
accounts.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from odds_scanner.engine import Source
from odds_scanner.errors import BlockedError, ProviderError
from odds_scanner.providers.sk.common import SlovakProvider


DETAILS_PER_SITE = 5  # match pages saved per site (one extra request each)


def _save_details(z: zipfile.ZipFile, src: Source, payloads: list, sport: str, now: datetime, out: Callable[[str], None]) -> int:
    """Save the full bet list of the next few matches, for sites that have a match-detail request."""
    provider = src.provider
    fetch = getattr(provider, "fetch_detail_raw", None)
    if fetch is None:
        return 0
    events = []
    for payload in payloads:
        try:
            events.extend(provider._parse(payload, sport, now))  # noqa: SLF001
        except Exception:  # noqa: BLE001 - capture what we can
            continue
    unique = {e.id: e for e in events if e.commence_time > now}  # today's and tomorrow's lists overlap
    events = sorted(unique.values(), key=lambda e: e.commence_time)[:DETAILS_PER_SITE]
    saved = 0
    for i, ev in enumerate(events):
        native = ev.id.split(":", 1)[1]
        variants = [("", {})]
        if i == 0 and src.key in ("tipos", "synot"):
            variants.append(("_longpolling", {"long_polling": True}))  # the site's own page sends True
        for suffix, kwargs in variants:
            try:
                raw = fetch(native, **kwargs)
            except BlockedError:
                raise
            except ProviderError as exc:
                out(f"{src.title:10}  match {native}{suffix}: ERROR {exc}")
                continue
            z.writestr(f"{src.key}/detail_{native}{suffix}.json", json.dumps({"_event": ev.name, "_start": ev.commence_time.isoformat(), "response": raw}, ensure_ascii=False))
            saved += 1
    if saved:
        out(f"{src.title:10}  {sport}: saved {saved} match detail page(s)")
    return saved


def capture(
    sources: Sequence[Source],
    out: Callable[[str], None] = print,
    dest_dir: Path | str = ".",
    now: datetime | None = None,
) -> Path | None:
    """Fetch every configured sport of each Slovak source once and zip the raw answers."""
    now = now or datetime.now(timezone.utc)
    path = Path(dest_dir) / f"capture-{now:%Y%m%d-%H%M}.zip"
    saved = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for src in sources:
            provider = src.provider
            if not isinstance(provider, SlovakProvider) or provider.samples:
                continue
            for sport in src.sports:
                try:
                    payloads = provider._fetch_payloads(sport, provider.sport_params[sport])  # noqa: SLF001
                except BlockedError as exc:
                    out(f"{src.title:10}  BLOCKED  {exc}")
                    break
                except ProviderError as exc:
                    out(f"{src.title:10}  {sport}: ERROR {exc}")
                    continue
                for i, payload in enumerate(payloads, start=1):
                    z.writestr(f"{src.key}/{sport}_{i}.json", json.dumps(payload, ensure_ascii=False))
                saved += len(payloads)
                out(f"{src.title:10}  {sport}: saved {len(payloads)} response(s)")
                if sport == "football":
                    try:
                        saved += _save_details(z, src, payloads, sport, now, out)
                    except BlockedError as exc:
                        out(f"{src.title:10}  BLOCKED  {exc}")
                        break
        z.writestr("INFO.txt", f"Captured {now.isoformat()} by odds-scanner `capture`.\n"
                               f"Request settings used: { {s.key: getattr(s.provider, 'request_choice', {}) for s in sources} }\n")
    if not saved:
        path.unlink(missing_ok=True)
        out("Nothing could be saved.")
        return None
    out("")
    out(f"Done. Send this file:  {path.resolve()}")
    return path

"""CGC tracking API as a *historical* backfill source.

OGN's live APRS-IS feed has no history. The Cambridge Gliding Centre tracking
page, however, retains the last few days of fixes (its own OGN receiver feeding
a database). We pull a day from its cursor API and write fixes into the same
store the OGN collector uses, so all downstream tooling is source-agnostic.

Endpoint: GET /api/Tracking/{Y|M|D|0|0|0}  -> JSON string -> base64 -> gzip ->
array of per-aircraft fix blocks. Each block: points {L,N,H,T}, the last row of
each block adds G (callsign), S (speed), Z ("typeIdx|model registration").
Times are UK local; we convert to UTC. H is ft ASL.
"""
import base64
import gzip
import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone

from .store import Store

logger = logging.getLogger(__name__)

BASE = "https://members.camgliding.uk/api/Tracking/"
HEADERS = {
    "User-Agent": "ognflights/0.1",
    "Referer": "https://members.camgliding.uk/tracking/default.aspx",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
# CGC times are UK local (BST in summer). Adjust if you backfill winter days.
LOCAL_OFFSET = timedelta(hours=1)

# CGC aircraft-type index (Z field, first token) -> our vocabulary.
CGC_TYPE = {"1": "glider", "2": "tow", "8": "motorglider"}


def _fetch(pointer: str, cookie: str) -> list:
    req = urllib.request.Request(BASE + pointer, headers={**HEADERS, "Cookie": f"cgc-ops-dev-rosters-sql={cookie}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)              # API returns a JSON string
    if not isinstance(body, str) or "authorised" in body.lower():
        raise RuntimeError(f"CGC API error / bad cookie: {body!r}")
    return json.loads(gzip.decompress(base64.b64decode(body)).decode("utf-8", "replace"))


def fetch_day(day: datetime, cookie: str) -> list:
    """Return all raw fix records for `day` by following the cursor."""
    pointer = f"{day.year}|{day.month}|{day.day}|0|0|0"
    pts, seen = [], set()
    while True:
        batch = _fetch(pointer, cookie)
        if len(batch) > 1:
            pts.extend(batch[:-1])
        nxt = batch[-1].get("P")
        if not nxt or nxt == pointer or nxt in seen:
            break
        seen.add(pointer); pointer = nxt
    return pts


class _Rec:
    """Minimal Beacon-like object so we can reuse Store.add_fix / upsert_device."""
    __slots__ = ("address", "ts", "lat", "lon", "altitude_ft", "speed_kt",
                 "climb_fpm", "receiver", "address_type", "aircraft_type")


class _Dev:
    __slots__ = ("registration", "cn", "model", "aircraft_type", "tracked", "identified")


def backfill(store: Store, day: datetime, cookie: str) -> int:
    """Fetch a CGC day and insert its fixes into the store. Returns fix count."""
    pts = fetch_day(day, cookie)
    # split into per-aircraft blocks (terminated by a row carrying 'G')
    blocks, cur = [], []
    for p in pts:
        cur.append(p)
        if "G" in p:
            blocks.append(cur); cur = []

    n = 0
    base = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    for blk in blocks:
        term = blk[-1]
        callsign = term.get("G", "?")
        z = term.get("Z", "").split("|")
        ac_type = CGC_TYPE.get(z[0], "powered") if z else "powered"
        model = z[1] if len(z) > 1 else ""
        registration = model.split()[-1] if model and model.split()[-1].startswith(("G-", "ZS", "D-")) else None

        dev = _Dev()
        dev.registration, dev.cn, dev.model = registration, callsign, model
        dev.aircraft_type, dev.tracked, dev.identified = ac_type, True, True

        for p in blk:
            h, m, s = (int(x) for x in p["T"].split(":"))
            ts = base + timedelta(hours=h, minutes=m, seconds=s) - LOCAL_OFFSET
            rec = _Rec()
            rec.address, rec.ts = callsign, ts
            rec.lat, rec.lon, rec.altitude_ft = p["L"], p["N"], float(p["H"])
            rec.speed_kt = p.get("S")
            rec.climb_fpm = None
            rec.receiver = "CGC"
            rec.address_type, rec.aircraft_type = "CGC", ac_type
            store.add_fix(rec)
            store.upsert_device(rec, dev)
            n += 1
    store.commit()
    logger.info("backfilled %d fixes across %d aircraft for %s", n, len(blocks), day.date())
    return n

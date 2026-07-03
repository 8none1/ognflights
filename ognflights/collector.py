"""Collector: stream OGN beacons into the store, resolving registrations as we go.

Runs forever by default (intended as a long-lived daemon / systemd service);
pass max_seconds to cap a run for testing.

`collect()` is the simple legacy mode (store everything in an area). `watch()` is
the buddy-follow daemon: detect launches from the field and follow each launched
aircraft anywhere until it lands, storing only those flights, into year-partitioned
SQLite files.
"""
import logging
import math
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from .aprs import OgnClient, stream
from . import config
from .config import FILTER_RADIUS_KM, GRANSDEN, Site
from .ddb import DDB
from .store import Store, store_for_day

logger = logging.getLogger(__name__)


def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def collect(store: Store, ddb: DDB, site: Site = GRANSDEN,
            radius_km: int = FILTER_RADIUS_KM, max_seconds: int | None = None,
            commit_every: int = 200) -> int:
    start = time.time()
    n = 0
    for b in stream(site, radius_km=radius_km, reconnect=max_seconds is None):
        if b.no_track:
            continue                       # respect opt-out
        store.add_fix(b)
        store.upsert_device(b, ddb.lookup(b.address))
        n += 1
        if n % commit_every == 0:
            store.commit()
            logger.info("stored %d fixes", n)
        if max_seconds is not None and time.time() - start >= max_seconds:
            break
    store.commit()
    return n


def watch(ddb: DDB, max_seconds: int | None = None, commit_every: int = 100,
          status: dict | None = None) -> int:
    """Buddy-follow daemon.

    Subscribe to a catch circle around the field. When an aircraft is seen low
    inside the launch geofence ("armed") and then climbs away, treat it as a
    launch from the field, start following it anywhere via a live buddy filter,
    and store its whole flight (incl. the buffered launch roll) until it lands.
    Only followed aircraft are stored, into year-partitioned SQLite files.
    """
    ceiling = GRANSDEN.elevation_ft + config.LAUNCH_MAX_AGL_FT
    base_filter = f"r/{config.LAUNCH_LAT}/{config.LAUNCH_LON}/{config.CATCH_RADIUS_KM}"
    owned: set[str] = set()          # source callsigns we're following
    armed: set[str] = set()          # seen low at the field, awaiting a climb-out
    last_seen: dict[str, float] = {}
    buffers: dict[str, deque] = defaultdict(deque)

    def build_filter() -> str:
        return base_filter + (" b/" + "/".join(sorted(owned)) if owned else "")

    client = OgnClient(build_filter(), reconnect=max_seconds is None)
    year = datetime.now(timezone.utc).year
    store = store_for_day(datetime.now(timezone.utc))
    start = time.time()
    n = 0
    last_trim = start
    if status is not None:
        status.update(started=start, connected=False, following=0, stored=0, last_beacon=None)
    try:
        for b in client.beacons():
            now = time.time()
            if status is not None:
                status["connected"] = True
                status["last_beacon"] = now
                status["following"] = len(owned)
                status["stored"] = n
            y = datetime.now(timezone.utc).year
            if y != year:
                store.commit(); store.close()
                store = store_for_day(datetime.now(timezone.utc)); year = y
                logger.info("rolled over to year %d", year)

            if b.no_track:
                continue
            last_seen[b.source] = now

            if b.source in owned:
                store.add_fix(b); store.upsert_device(b, ddb.lookup(b.address))
                n += 1
            else:
                buf = buffers[b.source]; buf.append(b)
                cutoff = b.ts.timestamp() - config.LAUNCH_BUFFER_S
                while buf and buf[0].ts.timestamp() < cutoff:
                    buf.popleft()
                low = (b.altitude_ft <= ceiling and
                       _haversine_m(b.lat, b.lon, config.LAUNCH_LAT, config.LAUNCH_LON) <= config.LAUNCH_RADIUS_M)
                if low:
                    armed.add(b.source)
                elif b.source in armed and b.altitude_ft > ceiling:
                    # armed at the field then climbed away -> a launch from Gransden
                    owned.add(b.source); armed.discard(b.source)
                    for pb in buf:
                        store.add_fix(pb); store.upsert_device(pb, ddb.lookup(pb.address))
                    n += len(buf); buffers.pop(b.source, None)
                    client.set_filter(build_filter())
                    logger.info("launch: following %s (%d aircraft)", b.source, len(owned))

            if n and n % commit_every == 0:
                store.commit()

            if now - last_trim > 30:
                last_trim = now
                gone = [s for s in owned if now - last_seen.get(s, 0) > config.FOLLOW_IDLE_TIMEOUT_S]
                if gone:
                    owned.difference_update(gone)
                    client.set_filter(build_filter())
                    logger.info("landed/lost %d, following %d", len(gone), len(owned))
                stale = [s for s in list(buffers) if now - last_seen.get(s, 0) > config.LAUNCH_BUFFER_S * 2]
                for s in stale:
                    buffers.pop(s, None); armed.discard(s)
                store.commit()

            if max_seconds is not None and now - start >= max_seconds:
                break
    finally:
        store.commit(); store.close()
    return n

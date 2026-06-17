"""Collector: stream OGN beacons into the store, resolving registrations as we go.

Runs forever by default (intended as a long-lived daemon / systemd service);
pass max_seconds to cap a run for testing.
"""
import logging
import time

from .aprs import stream
from .config import FILTER_RADIUS_KM, GRANSDEN, Site
from .ddb import DDB
from .store import Store

logger = logging.getLogger(__name__)


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

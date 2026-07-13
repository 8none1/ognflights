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


def _label_for(dev) -> str:
    """Human label from a DDB device (registration [CN]), or empty if unknown."""
    if dev and dev.registration:
        return dev.registration + (f" [{dev.cn}]" if dev.cn else "")
    return ""


# Map an OGN address_type to the APRS source-callsign prefix the parser produces.
# aprs.ADDRESS_TYPES = {0: RANDOM, 1: ICAO, 2: FLARM, 3: OGN}; the callsign is the
# prefix + the device hex (e.g. FLARM DDB11F -> FLRDDB11F, ICAO 405542 -> ICA405542).
# RANDOM ids are anonymous and cannot be reconstructed reliably, so we skip them.
_TYPE_PREFIX = {"FLARM": "FLR", "ICAO": "ICA", "OGN": "OGN"}


def _source_for(address: str, address_type: str) -> str | None:
    """Reconstruct the APRS source callsign for a device, or None if not possible."""
    prefix = _TYPE_PREFIX.get((address_type or "").upper())
    if not prefix:
        return None
    return prefix + address.upper()


def watch(ddb: DDB, max_seconds: int | None = None, commit_every: int = 100,
          status: dict | None = None, hub=None) -> int:
    """Buddy-follow daemon.

    Subscribe to a catch circle around the field. When an aircraft is seen low
    inside the launch geofence ("armed") and then climbs away, treat it as a
    launch from the field, start following it anywhere via a live buddy filter,
    and store its whole flight (incl. the buffered launch roll) until it lands.
    Only followed aircraft are stored, into year-partitioned SQLite files.

    If `hub` is given, publish a lightweight event per stored fix for the live
    web view (non-blocking; a slow browser can never stall this loop).
    """
    # Live-event helpers live in webapp; import lazily so a headless run needn't touch it.
    if hub is not None:
        from .webapp import live_color, live_height_m, _live_model
    else:
        live_color = live_height_m = _live_model = None

    def _short_cs(label: str) -> str:
        """Short callsign = the bit in [brackets] (mirrors the live view's shortCallsign)."""
        if "[" in label and "]" in label:
            return label[label.index("[") + 1:label.index("]")].strip()
        return label

    def _publish(b, dev):
        if hub is None:
            return
        label = _label_for(dev) or b.address
        model_str = dev.model if dev else ""
        ac_type = (dev.aircraft_type if dev else None) or b.aircraft_type or ""
        hub.publish({
            "addr": b.address,
            "name": label,
            "label": label,
            "lon": round(b.lon, 6),
            "lat": round(b.lat, 6),
            "height_m": live_height_m(b.altitude_ft),
            "ts": int(b.ts.timestamp()),
            "model": _live_model(model_str, ac_type),
            "color": live_color(b.address),
        })

    # Parked/ground overlay: aircraft sitting at the field with their FLARM on are already
    # in the catch-circle beacon stream. Surface them as a LIVE-ONLY, ephemeral overlay -
    # never stored, never segmented. Throttled per address so the stream stays light.
    ground_last_pub: dict[str, float] = {}      # hex -> wall-clock of last ground publish

    def _publish_ground(b, dev, now):
        if hub is None:
            return
        prev = ground_last_pub.get(b.address)
        if prev is not None and now - prev < config.GROUND_PUBLISH_INTERVAL_S:
            return                              # throttle: at most one ground event / interval
        ground_last_pub[b.address] = now
        label = _label_for(dev) or b.address
        model_str = dev.model if dev else ""
        ac_type = (dev.aircraft_type if dev else None) or b.aircraft_type or ""
        hub.publish({
            "g": 1,                             # ground/parked flag (vs a normal airborne event)
            "addr": b.address,
            "name": label,
            "cs": _short_cs(label),
            "lon": round(b.lon, 6),
            "lat": round(b.lat, 6),
            "ts": int(b.ts.timestamp()),
            "model": _live_model(model_str, ac_type),
            "color": live_color(b.address),
        })

    ceiling = GRANSDEN.elevation_ft + config.LAUNCH_MAX_AGL_FT
    base_filter = f"r/{config.LAUNCH_LAT}/{config.LAUNCH_LON}/{config.CATCH_RADIUS_KM}"
    # All follow state is keyed by the aircraft HEX ADDRESS, not the APRS source
    # callsign. One physical aircraft that transmits over two links (e.g. FLARM and
    # ADS-B) reaches us under two source callsigns (FLR<hex> and ICA<hex>) but the
    # same hex, so hex-keying tracks, stores and publishes it exactly once.
    owned: set[str] = set()          # hex addresses we're following
    armed: set[str] = set()          # hex seen low at the field, awaiting a climb-out
    last_seen: dict[str, float] = {}                     # hex -> wall-clock last heard
    buffers: dict[str, deque] = defaultdict(deque)       # hex -> pre-ownership fixes
    # Source callsigns seen per owned/armed hex. We subscribe to every source variant
    # of a followed aircraft (so we hear it via either transmitter) while still
    # tracking it as a single hex-keyed entry.
    sources: dict[str, set[str]] = defaultdict(set)
    # In-memory fast path: highest fix ts stored per hex address. The same beacon
    # reaches us once per ground receiver that heard it, so most beacons are
    # duplicates; this lets us drop them without touching SQLite at all. Keyed by
    # hex (same lifecycle as `owned`), pruned when we stop following an aircraft so
    # it cannot grow unbounded. The DB UNIQUE(address, ts) index + INSERT OR IGNORE
    # remains the correctness backstop for the narrow restart/out-of-order case.
    max_ts: dict[str, int] = {}
    # Last accepted position per aircraft (ts, lat, lon), to reject spatially-impossible
    # position jumps: garbage fixes that arrive in good time order but teleport the aircraft
    # (so the dedup/ts guard cannot see them). Same lifecycle as max_ts.
    last_pos: dict[str, tuple] = {}
    glitch_max_ms = config.GLITCH_MAX_SPEED_KT * 0.514444  # kt -> m/s

    def store_fix(b, dev) -> bool:
        """Store a fix, using the in-memory max-ts pre-filter as the fast path.

        Keyed by b.address (hex), so a fix already stored via one transmitter is
        dropped when the same fix arrives via the aircraft's other transmitter.
        Returns True only if the fix was genuinely new (passed the pre-filter and
        was actually inserted), so the caller knows whether to publish it live.
        """
        ts = int(b.ts.timestamp())
        prev = max_ts.get(b.address)
        if prev is not None and ts <= prev:
            return False                       # duplicate/out-of-order: skip SQLite entirely
        lp = last_pos.get(b.address)           # reject spatially-impossible jumps (garbage fixes)
        if lp is not None:
            dt = ts - lp[0]
            if dt > 0 and _haversine_m(lp[1], lp[2], b.lat, b.lon) / dt > glitch_max_ms:
                return False                   # implausible speed: drop, stay anchored to last good
        stored = store.add_fix(b)              # unique index is the backstop
        store.upsert_device(b, dev)
        if stored:
            max_ts[b.address] = ts
            last_pos[b.address] = (ts, b.lat, b.lon)
        return stored

    def build_filter() -> str:
        srcs = sorted({s for hexid in owned for s in sources.get(hexid, ())})
        return base_filter + (" b/" + "/".join(srcs) if srcs else "")

    year = datetime.now(timezone.utc).year
    store = store_for_day(datetime.now(timezone.utc))

    # Restart recovery: re-acquire aircraft still airborne and recently heard, so a
    # container restart does not lose a flight in progress. Seed them into `owned`
    # (with reconstructed source callsigns) BEFORE connecting, so the very first
    # buddy filter already includes them; ones no longer transmitting simply idle out.
    now0 = time.time()
    now_ts0 = int(now0)
    try:
        recovered = store.recent_airborne(
            now_ts0, config.RECOVERY_WINDOW_S,
            GRANSDEN.elevation_ft + config.RECOVERY_MIN_AGL_FT)
    except Exception as e:                      # never let recovery block startup
        recovered = []
        logger.warning("recovery query failed: %s", e)
    for addr, addr_type, last_ts, last_alt in recovered:
        src = _source_for(addr, addr_type)
        if src is None:
            logger.info("recovery: skip %s (type %r, cannot reconstruct source)",
                        addr, addr_type)
            continue
        owned.add(addr)
        sources[addr].add(src)
        last_seen[addr] = float(last_ts)       # so the idle timeout still applies
        max_ts[addr] = int(last_ts)            # do not re-store fixes we already have
        logger.info("recovery: re-acquiring %s via %s (last %ds ago, %.0f ft)",
                    addr, src, now_ts0 - last_ts, last_alt)
    if owned:
        logger.info("recovery: re-acquired %d aircraft: %s",
                    len(owned), ", ".join(sorted(owned)))

    client = OgnClient(build_filter(), reconnect=max_seconds is None)
    start = time.time()
    n = 0
    last_trim = start
    if status is not None:
        status.update(started=start, connected=False, following=len(owned), stored=0,
                      last_beacon=None, last_line=None)
    try:
        for b in client.beacons():
            now = time.time()
            if status is not None:
                # Any line from the server (incl. None keepalive ticks) proves the
                # backend link is alive; only real beacons count as aircraft traffic.
                status["connected"] = True
                status["last_line"] = now
                status["following"] = len(owned)
            if b is None:
                continue                       # keepalive tick: link up, no aircraft data
            if status is not None:
                status["last_beacon"] = now
                status["stored"] = n
            y = datetime.now(timezone.utc).year
            if y != year:
                store.commit(); store.close()
                store = store_for_day(datetime.now(timezone.utc)); year = y
                logger.info("rolled over to year %d", year)

            if b.no_track:
                continue
            last_seen[b.address] = now

            if b.address in owned:
                # keep every source variant so we still hear this aircraft if it
                # later transmits via a second link; refresh the buddy filter if new.
                if b.source not in sources[b.address]:
                    sources[b.address].add(b.source)
                    client.set_filter(build_filter())
                dev = ddb.lookup(b.address)
                if store_fix(b, dev):
                    n += 1
                    _publish(b, dev)   # only new fixes reach the live view
            else:
                buf = buffers[b.address]; buf.append(b)
                sources[b.address].add(b.source)
                cutoff = b.ts.timestamp() - config.LAUNCH_BUFFER_S
                while buf and buf[0].ts.timestamp() < cutoff:
                    buf.popleft()
                low = (b.altitude_ft <= ceiling and
                       _haversine_m(b.lat, b.lon, config.LAUNCH_LAT, config.LAUNCH_LON) <= config.LAUNCH_RADIUS_M)
                if low:
                    armed.add(b.address)
                    # parked at the field and not (yet) being followed: surface it on the
                    # live overlay (ephemeral, never stored). Once it launches it enters
                    # `owned` and this branch stops running for it.
                    _publish_ground(b, ddb.lookup(b.address), now)
                elif b.address in armed and b.altitude_ft > ceiling:
                    # armed at the field then climbed away -> a launch from Gransden
                    owned.add(b.address); armed.discard(b.address)
                    for pb in buf:
                        pdev = ddb.lookup(pb.address)
                        if store_fix(pb, pdev):
                            n += 1
                            _publish(pb, pdev)   # only new fixes reach the live view
                    buffers.pop(b.address, None)
                    client.set_filter(build_filter())
                    logger.info("launch: following %s (%d aircraft)", b.address, len(owned))

            if n and n % commit_every == 0:
                store.commit()

            if now - last_trim > 30:
                last_trim = now
                gone = [h for h in owned if now - last_seen.get(h, 0) > config.FOLLOW_IDLE_TIMEOUT_S]
                if gone:
                    owned.difference_update(gone)
                    for h in gone:
                        max_ts.pop(h, None)     # stop tracking ts once we drop the aircraft
                        last_pos.pop(h, None)   # and its last-good position
                        sources.pop(h, None)    # and forget its source callsigns
                    client.set_filter(build_filter())
                    logger.info("landed/lost %d, following %d", len(gone), len(owned))
                stale = [h for h in list(buffers) if now - last_seen.get(h, 0) > config.LAUNCH_BUFFER_S * 2]
                for h in stale:
                    buffers.pop(h, None); armed.discard(h)
                    if h not in owned:
                        sources.pop(h, None)
                # prune ground-publish throttle state for aircraft we haven't heard in a while,
                # so the dict cannot grow unbounded (mirrors the parked prune grace on the client).
                gground = [h for h in ground_last_pub
                           if now - last_seen.get(h, 0) > config.GROUND_STATE_TTL_S]
                for h in gground:
                    ground_last_pub.pop(h, None)
                store.commit()

            if max_seconds is not None and now - start >= max_seconds:
                break
    finally:
        store.commit(); store.close()
    return n

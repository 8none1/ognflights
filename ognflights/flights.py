"""Flight segmentation: turn a stream of fixes into individual flights.

A flight begins on a ground->airborne transition (sudden climb above the
airfield) and ends on airborne->ground, or on a long gap in fixes. Short hops
and ground noise are filtered out.
"""
import math
from dataclasses import dataclass, field

from .config import (BRIDGE_MAX_GAP_SECONDS, GROUND_AGL_FT, LANDED_MAX_AGL_FT,
                     LANDED_SPEED_KT, LANDED_STATIONARY_SECONDS, LAUNCH_FAILURE_AGL_FT,
                     MAX_FIX_GAP_SECONDS, MIN_FLIGHT_PEAK_AGL_FT, MIN_FLIGHT_SECONDS, Site)
from .store import Fix


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (minimal local copy of collector._haversine_m)."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def _ground_speed_kt(prev: Fix, cur: Fix) -> float | None:
    """Ground speed (kt) between two consecutive fixes from position/time.

    Fallback for when Fix.speed_kt is not populated (e.g. CGC backfill data).
    Returns None if there is no usable time delta.
    """
    dt = cur.ts - prev.ts
    if dt <= 0:
        return None
    metres = _haversine_m(prev.lat, prev.lon, cur.lat, cur.lon)
    return (metres / dt) * 1.943844  # m/s -> knots


@dataclass
class Flight:
    address: str
    fixes: list[Fix] = field(default_factory=list)

    @property
    def start(self) -> int: return self.fixes[0].ts
    @property
    def end(self) -> int: return self.fixes[-1].ts
    @property
    def duration_s(self) -> int: return self.end - self.start
    def peak_alt_ft(self) -> float: return max(f.alt_ft for f in self.fixes)


def segment(address: str, fixes: list[Fix], site: Site) -> list[Flight]:
    ground = site.elevation_ft + GROUND_AGL_FT
    flights: list[Flight] = []
    cur: Flight | None = None
    prev: Fix | None = None
    # Start index (into cur.fixes) of the current run of low+stationary fixes, or None.
    run_start_i: int | None = None

    for f in fixes:
        prev_ts = prev.ts if prev is not None else None
        airborne = f.alt_ft >= ground
        gap = prev_ts is not None and (f.ts - prev_ts) > MAX_FIX_GAP_SECONDS
        # Bridge coverage dropouts: don't split if still airborne both sides and the gap
        # is not enormous (the position just interpolates across). A gap while on the
        # ground, or an enormous gap, still ends the flight (a real landing).
        bridge = airborne and prev_ts is not None and (f.ts - prev_ts) <= BRIDGE_MAX_GAP_SECONDS
        if cur is not None and gap and not bridge:
            flights.append(cur); cur = None; run_start_i = None
        if airborne:
            if cur is None:
                cur = Flight(address=address)
                run_start_i = None
            cur.fixes.append(f)

            # --- landed detection: low AND stationary for a continuous run ---
            low = (f.alt_ft - site.elevation_ft) < LANDED_MAX_AGL_FT
            # Prefer the reported ground speed; fall back to position-delta when absent.
            if f.speed_kt is not None:
                stationary = f.speed_kt <= LANDED_SPEED_KT
            elif prev is not None:
                gs = _ground_speed_kt(prev, f)
                stationary = gs is not None and gs <= LANDED_SPEED_KT
            else:
                stationary = False  # no prior fix and no reported speed: can't tell

            if low and stationary:
                if run_start_i is None:
                    run_start_i = len(cur.fixes) - 1
                run_start_ts = cur.fixes[run_start_i].ts
                if (f.ts - run_start_ts) >= LANDED_STATIONARY_SECONDS:
                    # Landed: trim to the first fix of the stationary run (touchdown/stop),
                    # discarding the stationary tail, and start a fresh flight afterwards.
                    cur.fixes = cur.fixes[:run_start_i + 1]
                    flights.append(cur); cur = None; run_start_i = None
            else:
                # Moving or not-low: the run is broken.
                run_start_i = None
        elif cur is not None:
            flights.append(cur); cur = None; run_start_i = None
        prev = f
    if cur is not None:
        flights.append(cur)

    real = []
    for fl in flights:
        peak_agl = fl.peak_alt_ft() - ground
        # A normal flight (high enough AND long enough), or a brief-but-genuine launch that
        # reached launch-failure height (an aborted launch, which never lasts long enough).
        if peak_agl >= MIN_FLIGHT_PEAK_AGL_FT and (
                fl.duration_s >= MIN_FLIGHT_SECONDS or peak_agl >= LAUNCH_FAILURE_AGL_FT):
            real.append(fl)
    return real


def classify_launch(flight: Flight, site: Site) -> str:
    """Rough launch-type guess from the initial climb rate.

    Winch launches gain height very fast (often >1000 ft in ~40 s); aerotows
    climb more gradually. Heuristic only.
    """
    fixes = flight.fixes
    if len(fixes) < 3:
        return "unknown"
    t0 = fixes[0].ts
    climb = [f for f in fixes if f.ts - t0 <= 60]
    if len(climb) < 2:
        return "unknown"
    gained = climb[-1].alt_ft - climb[0].alt_ft
    secs = max(1, climb[-1].ts - climb[0].ts)
    rate_fps = gained / secs            # ft per second
    if rate_fps > 18:                   # ~1100 ft/min+
        return "winch"
    if rate_fps > 3:
        return "aerotow"
    return "unknown"

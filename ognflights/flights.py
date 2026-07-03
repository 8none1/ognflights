"""Flight segmentation: turn a stream of fixes into individual flights.

A flight begins on a ground->airborne transition (sudden climb above the
airfield) and ends on airborne->ground, or on a long gap in fixes. Short hops
and ground noise are filtered out.
"""
from dataclasses import dataclass, field

from .config import (BRIDGE_MAX_GAP_SECONDS, GROUND_AGL_FT, MAX_FIX_GAP_SECONDS,
                     MIN_FLIGHT_PEAK_AGL_FT, MIN_FLIGHT_SECONDS, Site)
from .store import Fix


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
    prev_ts: int | None = None

    for f in fixes:
        airborne = f.alt_ft >= ground
        gap = prev_ts is not None and (f.ts - prev_ts) > MAX_FIX_GAP_SECONDS
        # Bridge coverage dropouts: don't split if still airborne both sides and the gap
        # is not enormous (the position just interpolates across). A gap while on the
        # ground, or an enormous gap, still ends the flight (a real landing).
        bridge = airborne and prev_ts is not None and (f.ts - prev_ts) <= BRIDGE_MAX_GAP_SECONDS
        if cur is not None and gap and not bridge:
            flights.append(cur); cur = None
        if airborne:
            if cur is None:
                cur = Flight(address=address)
            cur.fixes.append(f)
        elif cur is not None:
            flights.append(cur); cur = None
        prev_ts = f.ts
    if cur is not None:
        flights.append(cur)

    real = []
    for fl in flights:
        if (fl.peak_alt_ft() - ground) >= MIN_FLIGHT_PEAK_AGL_FT \
                and fl.duration_s >= MIN_FLIGHT_SECONDS:
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

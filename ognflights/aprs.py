"""OGN APRS-IS client and beacon parser (stdlib only).

Connects to the Open Glider Network APRS-IS feed with a server-side area filter
and yields parsed aircraft position beacons. No third-party dependencies.

Reference: APRS position format + OGN status comment.
  FLRDD8EE2>APRS,qAS,UKGRLLP:/165422h5210.98N/00006.71W'000/000/A=000236 !W58! id06DD8EE2 -019fpm +0.0rot ...
"""
import logging
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import (APRS_CALLSIGN, APRS_HOST, APRS_PORT, FILTER_RADIUS_KM, Site)

logger = logging.getLogger(__name__)

# OGN aircraft-type codes (from the id flags nibble)
AIRCRAFT_TYPES = {
    0: "unknown", 1: "glider", 2: "tow", 3: "helicopter", 4: "parachute",
    5: "dropplane", 6: "hangglider", 7: "paraglider", 8: "poweredaircraft",
    9: "jet", 10: "ufo", 11: "balloon", 12: "airship", 13: "uav",
    14: "reserved", 15: "static",
}
ADDRESS_TYPES = {0: "RANDOM", 1: "ICAO", 2: "FLARM", 3: "OGN"}

# Source-call prefixes that denote an aircraft (not a ground receiver).
AIRCRAFT_PREFIXES = ("FLR", "ICA", "OGN", "PAW", "FNT", "FLD", "RND", "SKY", "ADSL")

# Timestamped, uncompressed APRS position: /HHMMSSh DDMM.mmN /DDDMM.mmW <sym> [CSE/SPD] /A=AAAAAA
_POS = re.compile(
    r"^/(?P<time>\d{6})h"
    r"(?P<lat>\d{2}\d{2}\.\d+)(?P<ns>[NS])"
    r".(?P<lon>\d{3}\d{2}\.\d+)(?P<ew>[EW])"
    r"(?P<sym>.)"
    r"(?:(?P<crs>\d{3})/(?P<spd>\d{3}))?"
    r"/A=(?P<alt>-?\d{6})"
)
_DAO = re.compile(r"!W(\d)(\d)!")          # extra lat/lon precision digit
_ID = re.compile(r"id([0-9A-Fa-f]{2})([0-9A-Fa-f]{6})")
_FPM = re.compile(r"([+-]\d+)fpm")
_ROT = re.compile(r"([+-][\d.]+)rot")


@dataclass
class Beacon:
    raw: str
    source: str            # e.g. FLRDD8EE2
    address: str           # device hex id, e.g. DD8EE2
    address_type: str      # ICAO / FLARM / OGN / RANDOM
    aircraft_type: str     # glider / tow / ...
    stealth: bool
    no_track: bool
    ts: datetime           # UTC
    lat: float
    lon: float
    altitude_ft: float     # GPS altitude, ft (relative to WGS84 geoid)
    course: int | None
    speed_kt: int | None
    climb_fpm: int | None
    receiver: str | None   # ground station that heard it


def _coord(raw: str, hemi: str, dao_digit: str | None) -> float:
    """APRS DDMM.mm(+DAO) with hemisphere -> signed decimal degrees."""
    minutes = raw[-5:]          # MM.mm
    degrees = int(raw[:-5])     # leading DD or DDD
    if dao_digit:
        minutes = minutes + dao_digit
    val = degrees + float(minutes) / 60.0
    return -val if hemi in ("S", "W") else val


def parse_beacon(line: str) -> Beacon | None:
    """Parse one APRS-IS line into a Beacon, or None if not an aircraft fix."""
    if ":" not in line or ">" not in line:
        return None
    header, body = line.split(":", 1)
    source = header.split(">", 1)[0]
    if not source.startswith(AIRCRAFT_PREFIXES):
        return None
    m = _POS.match(body)
    if not m:
        return None

    dao = _DAO.search(body)
    lat = _coord(m["lat"], m["ns"], dao.group(1) if dao else None)
    lon = _coord(m["lon"], m["ew"], dao.group(2) if dao else None)

    hh, mm, ss = int(m["time"][:2]), int(m["time"][2:4]), int(m["time"][4:6])
    now = datetime.now(timezone.utc)
    ts = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    if ts - now > timedelta(minutes=1):    # packet just before "now"; handle midnight rollover
        ts -= timedelta(days=1)

    address, addr_type, ac_type, stealth, no_track = source[3:], "FLARM", "unknown", False, False
    idm = _ID.search(body)
    if idm:
        flags = int(idm.group(1), 16)
        address = idm.group(2).upper()
        addr_type = ADDRESS_TYPES.get(flags & 0x03, "RANDOM")
        ac_type = AIRCRAFT_TYPES.get((flags >> 2) & 0x0F, "unknown")
        stealth = bool(flags & 0x40)
        no_track = bool(flags & 0x80)

    fpm = _FPM.search(body)
    # receiver is the last element of the AX.25 path before the ':'
    receiver = header.split(",")[-1] if "," in header else None

    return Beacon(
        raw=line, source=source, address=address, address_type=addr_type,
        aircraft_type=ac_type, stealth=stealth, no_track=no_track, ts=ts,
        lat=lat, lon=lon, altitude_ft=float(m["alt"]),
        course=int(m["crs"]) if m["crs"] else None,
        speed_kt=int(m["spd"]) if m["spd"] else None,
        climb_fpm=int(fpm.group(1)) if fpm else None,
        receiver=receiver,
    )


def stream(site: Site, radius_km: int = FILTER_RADIUS_KM, reconnect: bool = True):
    """Yield Beacon objects from the OGN feed, filtered to `radius_km` around `site`.

    Reconnects with backoff on socket errors when `reconnect` is True.
    """
    login = (f"user {APRS_CALLSIGN} pass -1 vers ognflights 0.1 "
             f"filter r/{site.lat}/{site.lon}/{radius_km}\r\n")
    backoff = 1
    while True:
        try:
            logger.info("connecting to %s:%s", APRS_HOST, APRS_PORT)
            with socket.create_connection((APRS_HOST, APRS_PORT), timeout=20) as sock:
                sock.settimeout(60)
                f = sock.makefile("rwb")
                banner = f.readline().decode("utf-8", "replace").strip()
                logger.info("server: %s", banner)
                f.write(login.encode()); f.flush()
                backoff = 1
                last_keepalive = time.time()
                for raw in f:
                    line = raw.decode("utf-8", "replace").rstrip()
                    if not line or line.startswith("#"):
                        # server comment / keepalive; send our own periodically
                        if time.time() - last_keepalive > 240:
                            f.write(b"# keepalive\r\n"); f.flush()
                            last_keepalive = time.time()
                        continue
                    b = parse_beacon(line)
                    if b is not None:
                        yield b
        except (OSError, socket.timeout) as e:
            logger.warning("connection error: %s", e)
            if not reconnect:
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

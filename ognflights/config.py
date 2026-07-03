"""Project configuration. Defaults target Gransden Lodge (Cambridge Gliding Centre)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Site:
    name: str
    lat: float
    lon: float
    elevation_ft: float   # airfield elevation, ft AMSL


GRANSDEN = Site(name="Gransden Lodge", lat=52.18717, lon=-0.10937, elevation_ft=250.0)

# APRS-IS feed
APRS_HOST = "aprs.glidernet.org"
APRS_PORT = 14580
APRS_CALLSIGN = "OGNFLT"        # read-only login; any token works with pass -1
FILTER_RADIUS_KM = 60           # legacy area filter (used by the simple `collect`)

# Buddy-follow capture (the `watch` daemon):
# subscribe to a CATCH circle to spot launches, then follow each launched aircraft
# anywhere via a live APRS-IS buddy filter until it lands.
CATCH_RADIUS_KM = 15            # circle around the field to detect launches
# Launch geofence: an aircraft is "ours" once seen inside this circle at low altitude
# (i.e. on/just-off the field). Tight enough to exclude the neighbouring airfield.
LAUNCH_LAT = 52.18085
LAUNCH_LON = -0.11386
LAUNCH_RADIUS_M = 966.0
LAUNCH_MAX_AGL_FT = 200         # "low" = within this height above the field elevation
# Drop an aircraft from the follow list after this long with no beacons (landed/out of range).
FOLLOW_IDLE_TIMEOUT_S = 600
# Buffer this many seconds of an aircraft's pre-ownership fixes so we keep the launch roll.
LAUNCH_BUFFER_S = 60

# Storage: one SQLite file per calendar year (data/ogn-YYYY.sqlite), kept indefinitely.
DATA_DIR = "data"

# Flight detection
# An aircraft is "on the ground" below this height above the airfield.
GROUND_AGL_FT = 0
# A flight must climb at least this far above ground and last at least this long.
MIN_FLIGHT_PEAK_AGL_FT = 150
MIN_FLIGHT_SECONDS = 90
# Gap (no fixes) longer than this splits a track, even without a ground fix.
MAX_FIX_GAP_SECONDS = 120
# ...unless the aircraft is still airborne on both sides of the gap (a receiver-coverage
# dropout, not a landing): bridge those up to this long so a flight isn't falsely split.
BRIDGE_MAX_GAP_SECONDS = 1800

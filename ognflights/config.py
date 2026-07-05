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
# Reject spatially-impossible position fixes at capture: a fix implying a ground speed above
# this (from the last accepted position) is a garbage ADS-B/OGN report (they arrive in good
# time order, so the dedup/ts guard can't catch them). Nothing we track goes near this.
GLITCH_MAX_SPEED_KT = 250

# Restart recovery: on `watch` startup, re-acquire aircraft that were still airborne
# and recently heard, so a container restart no longer loses a flight in progress.
# An aircraft qualifies if its most recent stored fix is within RECOVERY_WINDOW_S and
# it was clearly airborne (alt above field elevation by more than RECOVERY_MIN_AGL_FT).
RECOVERY_WINDOW_S = 3600
RECOVERY_MIN_AGL_FT = 200

# Storage: one SQLite file per calendar year (data/ogn-YYYY.sqlite), kept indefinitely.
DATA_DIR = "data"

# Flight detection
# An aircraft is "on the ground" below this height above the airfield.
GROUND_AGL_FT = 0
# A flight must climb at least this far above ground and last at least this long.
MIN_FLIGHT_PEAK_AGL_FT = 150
MIN_FLIGHT_SECONDS = 90
# Landed end condition: end a flight once the aircraft has been both LOW and
# STATIONARY for a continuous run of at least LANDED_STATIONARY_SECONDS. This ends
# a flight cleanly at touchdown even when the glider then sits at ~field elevation
# (which reads as "airborne" while GROUND_AGL_FT is 0), and splits a land-then-relaunch
# into two flights. The flight is trimmed to the FIRST fix of the stationary run.
LANDED_MAX_AGL_FT = 100         # "low" = height above field elevation below this
LANDED_SPEED_KT = 5             # "stationary" = ground speed at/near zero (kt)
LANDED_STATIONARY_SECONDS = 60  # low+stationary must persist this long to count as landed

# Gap (no fixes) longer than this splits a track, even without a ground fix.
MAX_FIX_GAP_SECONDS = 120
# ...unless the aircraft is still airborne on both sides of the gap (a receiver-coverage
# dropout, not a landing): bridge those up to this long so a flight isn't falsely split.
BRIDGE_MAX_GAP_SECONDS = 1800

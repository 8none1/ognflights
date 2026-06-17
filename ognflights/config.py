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
FILTER_RADIUS_KM = 60           # area filter around the site

# Flight detection
# An aircraft is "on the ground" below this height above the airfield.
GROUND_AGL_FT = 250
# A flight must climb at least this far above ground and last at least this long.
MIN_FLIGHT_PEAK_AGL_FT = 150
MIN_FLIGHT_SECONDS = 90
# Gap (no fixes) longer than this splits a track, even without a ground fix.
MAX_FIX_GAP_SECONDS = 120

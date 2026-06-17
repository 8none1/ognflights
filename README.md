# ognflights

Capture [Open Glider Network](http://wiki.glidernet.org/) (OGN) traffic around a
gliding site, store every position fix, and extract **individual flights** (one
sudden climb to one descent-to-ground) for export to GPX / KML / IGC.

Born from reverse-engineering the Cambridge Gliding Centre tracking page; this
version goes straight to the underlying data source (OGN) instead of scraping.

## How it works

```
FLARM/ADS-B on aircraft  ->  OGN ground receivers (APRS-IS)  ->  ognflights
        (~1 Hz radio)            (e.g. UKGRLLP @ Gransden)        collector -> SQLite
                                                                       |
                                          DDB (device id -> registration)
                                                                       v
                                       flight segmentation -> GPX / KML / IGC
```

- **`aprs.py`** - connects to the OGN APRS-IS feed with a server-side radius
  filter and parses position beacons (lat/lon/alt/speed/climb + device id flags).
  Stdlib sockets only, no dependencies.
- **`ddb.py`** - downloads the OGN Device Database and maps a device's hex id to
  its registration, competition number, model and type. Cached 24 h.
- **`store.py`** - SQLite: `fixes` (every position) + `devices` (resolved metadata).
- **`flights.py`** - segments a day's fixes per aircraft into flights using a
  ground-height threshold and a max-gap rule; guesses winch vs aerotow from the
  initial climb rate.
- **`export.py`** - GPX, KML (plain `LineString`, works in Google Earth **Web**),
  and minimal IGC (OGN-derived, GPS altitude only - good for replay, not badge claims).

## Usage

```bash
# 1. Run the collector (foreground; runs until stopped). Reconnects automatically.
python3 cli.py collect

#    ...or cap it for a quick test:
python3 cli.py collect --minutes 5

# 2. See what was captured on a day (UTC):
python3 cli.py aircraft --day 2026-06-17

# 3. List detected flights, optionally for one aircraft:
python3 cli.py flights --day 2026-06-17 --reg G-CKFY

# 4. Export your flights (numbers come from the `flights` listing):
python3 cli.py export --day 2026-06-17 --reg G-CKFY --flights 4,5,6 --format all --outdir out/
```

The collector is meant to run continuously so it captures whole flying days. See
`ognflights.service` for a systemd unit.

## Configuration

Edit `ognflights/config.py`:

- `GRANSDEN` - site name, lat/lon, airfield elevation (ft). Change to track a
  different field.
- `FILTER_RADIUS_KM` - how wide an area to subscribe to.
- Flight-detection thresholds (ground AGL, minimum peak/duration, max gap).

`OGNFLIGHTS_DB` env var overrides the SQLite path (default `data/ogn.sqlite`).

## Notes / caveats

- OGN altitude is GPS altitude relative to the WGS84 geoid, not baro "ft ASL".
- Coverage is best-effort: depends on receivers being up. Aircraft set to
  "no-track" are honoured and dropped; "stealth" may still appear without id.
- Timestamps are UTC throughout. Convert to local for display if you prefer.

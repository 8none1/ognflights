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
  Stdlib sockets only, no dependencies. **Live only - OGN has no history**, so the
  collector must run continuously to capture flying days.
- **`cgc.py`** - **dormant, manual-only** historical backfill. The Cambridge
  Gliding Centre tracking page retains the last few days, so this can recover a
  day the collector missed. It only runs when you explicitly type `backfill` -
  nothing calls it automatically. OGN is the primary, go-forward source; reach
  for CGC only as a one-off "break glass". A whole day comes back in ~2 requests,
  but be courteous and don't loop it.
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

# 1b. (manual, rarely) recover a missed past day from CGC - see caveat above.
#     Run once, never loop. OGN is the normal source.
# CAMGLIDING_COOKIE=... python3 cli.py backfill --day 2026-06-17

# 2. See what was captured on a day (UTC):
python3 cli.py aircraft --day 2026-06-17

# 3. List detected flights, optionally for one aircraft:
python3 cli.py flights --day 2026-06-17 --reg G-CKFY

# 4. Export your flights (numbers come from the `flights` listing):
python3 cli.py export --day 2026-06-17 --reg G-CKFY --flights 4,5,6 --format all --outdir out/

# 5. Dump everything captured that day into ONE Google-Earth file
#    (colour-coded folder per aircraft). --gliders drops ADS-B airliners.
python3 cli.py earth --day 2026-06-17 --out capture.kml [--gliders]
```

The collector is meant to run continuously so it captures whole flying days. See
`ognflights.service` for a systemd unit.

## 3D replay (CesiumJS)

`replay/make_replay.py` turns a day's flights into a **self-contained, static
CesiumJS web page**: a 3D globe with time-animated aircraft you can play/scrub.
No server, no account, no build step - just an HTML file.

```bash
# one aircraft's selected flights, opening on the speed-coloured full track
python3 replay/make_replay.py --out out/gckfy.html --day 2026-07-01 \
    --title "G-CKFY (my flights)" --reg "G-CKFY:1,2,3,6" --trail full --speed-colour

# every glider/tug that flew that day
python3 replay/make_replay.py --out out/all-gliders.html --day 2026-07-01 \
    --title "All gliders" --gliders
```

Features: time slider; per-aircraft 3D models chosen from the CGC model string
(gliders vs DR-400 tugs, see the `MODELS` registry); trail modes full / active /
off; ground-speed-coloured trails (speed is derived from consecutive fixes, since
the feed rarely supplies it); night-sky and place-names toggles; a reset-view
button; `--home "lon,lat,height,heading,pitch"` opening camera (press **C** in the
page to capture the current view); model yaw tuning (number keys pick a model,
`[` / `]` rotate it, logged for `--yaw`).

Models live in `replay/models/` (glTF, GPLv2 from FlightAirMap - see
`replay/models/NOTICE.md`) and are referenced by URL so the browser caches them
across pages.

**Publishing:** the generated `*.html` plus the `models/` folder are copied to a
separate static-hosting repo (the personal website) under `flights/`, served at a
public URL. That website is only a *publishing target* - all project code, models
and CI live here. The Cesium version is pinned in the `CES` constant of
`make_replay.py`; a weekly GitHub Action (`.github/workflows/cesium-version-check.yml`)
opens an issue when a newer Cesium is released.

## Configuration

Edit `ognflights/config.py`:

- `GRANSDEN` - site name, lat/lon, airfield elevation (ft). Change to track a
  different field.
- `FILTER_RADIUS_KM` - how wide an area to subscribe to.
- Flight-detection thresholds (ground AGL, minimum peak/duration, max gap).

`OGNFLIGHTS_DB` env var overrides the SQLite path (default `data/ogn.sqlite`).

## Notes / caveats

- OGN altitude is GPS altitude relative to the WGS84 geoid, not baro "ft ASL".
  (The CGC backfill source supplies baro ft ASL; close enough for segmentation.)
- Coverage is best-effort: depends on receivers being up. Aircraft set to
  "no-track" are honoured and dropped; "stealth" may still appear without id.
- Timestamps are UTC throughout. Convert to local for display if you prefer
  (e.g. a 11:24 BST launch lists as 10:24).
- OGN identifies the **aircraft**, never the pilot, so picking "your" flights
  always means naming the glider + rough time.

## Status

Working and validated end to end:
- Live OGN capture near Gransden (resolved G-CHTV, G-CFYF, G-PRET via the
  UKGRLLP receiver).
- Flight segmentation + GPX/KML/IGC + single-file `earth` KML.
- CGC backfill recovered a full day (all 10 G-CKFY flights); launch classifier
  read morning circuits as winch, afternoon as aerotow.

To do (rough priority):
1. **Deploy the collector on perceptron** (systemd unit provided) so flying days
   are captured automatically from OGN. Until this runs continuously, OGN data
   only exists for the minutes the collector was up.
2. **Takeoff-proximity filter** - transiting ADS-B airliners currently register
   as "flights"; restrict to aircraft that launch near the field. (`--gliders`
   on `earth` is a stopgap that filters by aircraft type.)
3. Tune the winch/aerotow classifier against more known launches.
4. Optional: local-time display, daily auto-export, a small dashboard.

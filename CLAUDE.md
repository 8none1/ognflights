# ognflights — Claude context

## What this is

A standalone project that captures glider tracking data and extracts **individual
flights** to keep (GPX / KML / IGC). Built for Cambridge Gliding Centre / Gransden
Lodge but the site is configurable.

Origin: started by reverse-engineering the CGC tracking page, then pivoted to tap
the underlying data source — the **Open Glider Network (OGN)** — directly.

## Architecture

```
FLARM/ADS-B on aircraft  ->  OGN ground receivers (APRS-IS)  ->  collector -> SQLite
        (~1 Hz radio)            (e.g. UKGRLLP @ Gransden)            |
                                          DDB (device id -> registration)
                                                                      v
                                       flight segmentation -> GPX / KML / IGC / earth-KML
```

- `ognflights/aprs.py` — OGN APRS-IS client + beacon parser (stdlib sockets).
- `ognflights/ddb.py` — OGN Device Database: device hex id -> registration/CN/type. Cached 24 h.
- `ognflights/store.py` — SQLite: `fixes` + `devices`. `OGNFLIGHTS_DB` env overrides path.
- `ognflights/flights.py` — per-aircraft segmentation (ground-AGL + max-gap); winch/aerotow heuristic.
- `ognflights/export.py` — GPX, KML (`LineString`, Google-Earth-Web friendly), IGC, and `kml_tracks` (whole day, one file).
- `ognflights/cgc.py` — **dormant** manual-only historical backfill from the CGC API.
- `ognflights/collector.py` — streaming daemon (`collect`).
- `cli.py` — `collect | backfill | aircraft | flights | export | earth`.
- `ognflights.service` — systemd unit for the always-on collector (not yet deployed).

## Key facts / decisions

- **OGN APRS-IS is live-only — there is NO historical API.** The collector must
  run continuously or the data is gone. This is the central constraint.
- **Stdlib only, no pip deps.** PyPI is unreachable from the dev sandbox (raw
  sockets + HTTPS work, pip index does not), which conveniently forces a
  dependency-free design — ideal for a home-server daemon. Don't add `python-ogn-client`.
- **CGC backfill is manual-only and deliberately restrained.** Will asked not to
  hit CGC's servers routinely. `backfill` only runs when explicitly typed; nothing
  calls it automatically. A full day = ~2 requests, but do not loop it. Keep it as
  a "break glass" recovery tool, not a routine source.
- **OGN identifies the aircraft, never the pilot** — picking "your" flights always
  needs the glider registration + rough time.
- **Timestamps are UTC throughout.** Gransden local is BST (UTC+1) in summer.
- OGN altitude = GPS alt vs WGS84 geoid; CGC backfill = baro ft ASL. Both fine for
  the ground-threshold segmentation.

## Status

Validated end to end (live OGN capture, segmentation, all export formats, single-file
`earth` KML, and a CGC backfill that recovered all 10 G-CKFY flights for a day).

Open items: (1) **deploy collector to perceptron** so flying days are captured
automatically; (2) **takeoff-proximity filter** so transiting ADS-B airliners stop
registering as flights (`--gliders` is a type-based stopgap); (3) tune launch
classifier; (4) optional local-time display / dashboard.

## Conventions

- British English, no em dashes.
- Committed locally only; **do not push without asking**.
- Site/thresholds live in `ognflights/config.py`.

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

Validated end to end: OGN capture, segmentation, exports, `earth` KML, CGC backfill,
and CesiumJS 3D replays (`replay/make_replay.py`, published to www.whizzy.org/flights).

Capture is now the **`watch` daemon** (buddy-follow): subscribe to a catch circle,
detect launches inside the field geofence (a climb-out, so parked aircraft are
ignored), then follow each launched aircraft *anywhere* via a live APRS-IS `b/`
buddy filter until it lands. Type-agnostic; stores only our flights into
**year-partitioned** SQLite (`data/ogn-YYYY.sqlite`, WAL). Ships as a **Docker**
container (`Dockerfile` + `docker-compose.yml`).

Open items: (1) **deploy `watch` on perceptron** (`docker compose up -d --build`);
(2) tune the winch/aerotow classifier; (3) optional live mode / dashboard.

## Conventions

- British English, no em dashes.
- **GitHub repo: `8none1/ognflights` (public).** Never push to `main` without asking
  (per Will's hard rule). The website (`8none1.github.io`) is a separate publish
  target; the `watch`/replay tooling and CI live in this repo.
- Site, geofence and thresholds live in `ognflights/config.py`.

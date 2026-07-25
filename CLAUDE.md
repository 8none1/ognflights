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

- `ognflights/aprs.py` — OGN APRS-IS client + beacon parser (lat/lon/alt/speed/climb/**course/rot**). Stdlib sockets.
- `ognflights/ddb.py` — OGN Device Database: device hex id -> registration/CN/type. Cached 24 h.
- `ognflights/store.py` — SQLite `fixes` (+ `course`, `rot`) + `devices`, **year-partitioned** (`data/ogn-YYYY.sqlite`, WAL); idempotent ALTER-migrations. `OGNFLIGHTS_DB` overrides.
- `ognflights/flights.py` — per-aircraft segmentation (ground-AGL + max-gap); winch/aerotow heuristic.
- `ognflights/export.py` — GPX, KML, IGC, `kml_tracks` (whole day, one file).
- `ognflights/collector.py` — `collect` (legacy area capture) + **`watch`** (buddy-follow daemon); shares a status dict + live hub with the webapp.
- `ognflights/webapp.py` — stdlib HTTP server for the container: `/` `/live`(+SSE) `/replay` `/my-flights` `/pick` `/thermals` `/stats` `/healthz` and the JSON endpoints; injects the Cesium chrome.
- `ognflights/thermals.py` — thermal-hotspot detection + cache (`data/thermals.sqlite`).
- `ognflights/theme.py` — shared design system: CSS, nav, and the shared Cesium JS (help overlay, `ognThermalLayer`). Inlined by both webapp and make_replay so pages can't drift.
- `ognflights/cgc.py` — **dormant** manual-only CGC backfill.
- `replay/make_replay.py` — Cesium replay page builder (inline=private, external=public), the **flight-picker** page (`render_pick`), and the `collect`/`build_payload` the publisher reuses.
- `publish/sync_public.py` + `publish/worker.py` — build per-day JSON + `thermals.json` into a `public-data` git worktree; the worker pushes hourly and flattens the branch daily.
- `cli.py` — `watch | collect | healthcheck | aircraft | flights | export | backfill | publish | thermals | earth`.
- `ognflights.service` — systemd unit (unused; deployment is Docker/GHCR on perceptron).

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

## Status — deployed and live

Runs in production on **perceptron** as a Docker container (GHCR image
`ghcr.io/8none1/ognflights`), public via a **cloudflared** tunnel at
**https://ogn.8none1.org** and on the static site **https://www.whizzy.org/flights**.

**Capture:** the `watch` daemon (buddy-follow) — subscribe to a catch circle, detect a
launch from the field geofence, follow each launched aircraft anywhere via a live `b/`
buddy filter until it lands. Stores only our flights, kept indefinitely, and now also the
native **course** and **rot** (turn rate) per fix.

**Dashboard** (`ognflights/webapp.py`; Cesium chrome from `make_replay.py`):
- `/` home · `/live` real-time SSE 3D view (+ `?demo=1` kiosk/tour) · `/replay` day replay
  · `/my-flights` find a flight by date + rough time · `/pick` choose a day and tick
  individual flights (grouped by aircraft) to replay a subset or the whole day · `/thermals`
  hotspot map · `/stats` + `/healthz` health.
- Replay/live extras: climb/speed **colour trails**, a single **progressive trail** that
  draws as the glider flies, and the **thermal-hotspot** 3D drift-column overlay (toggle).
- The replay filters (`?address=`, `?t=`, **`?sel=`** subset) compose; `/pick` builds `?sel=`.

**Health:** `/stats` = per-component status (dashboard / OGN backend link / aircraft in
range / storage / publish worker); `/healthz` = JSON 200/503 driving the Docker HEALTHCHECK
(`cli.py healthcheck`). Link liveness ≠ traffic, so a quiet sky reads healthy.

**Thermal hotspots** (`ognflights/thermals.py`): recurring climbs (glider + climbing +
circling via `rot`, aerotow excluded by tug proximity), grid-binned + flood-fill clustered
into centroid / radius / altitude-band / base→top **drift**, cached in a *separate*
`data/thermals.sqlite` (no collector contention), recomputed daily on the publish worker's
rollover. Rendered as tilted 3D drift-columns via `ognThermalLayer` (theme.py) on `/thermals`,
the replay/live overlay, and the public site.

**Public site:** **all captured days** (indefinite; was a rolling 7) with a **month-calendar**
day picker and the flight picker (`flights/pick.html`). Each hourly run the worker rebuilds
*today* and pushes per-day `<YYYY-MM-DD>.json` + `manifest.json` + `thermals.json` to the
**`public-data`** branch (raw.githubusercontent, CORS `*`, ~5 min cache; page fetches from
`https://raw.githubusercontent.com/8none1/ognflights/public-data/`). On the UTC date rollover
it finalises the day that just ended and **flattens** the branch to one commit (force-push),
so `.git` stays bounded. Push auth = an SSH deploy key on perceptron. Build the public pages:
`python3 replay/make_replay.py --out publish/public-index.html --public --day <any> --title "Gransden flights" --link-single --path-resolution 3` and `--pick --out .../pick.html`; copy into `8none1.github.io/flights/` and push `master` (Pages).

**Deploy flow:** *dev mode* (bind-mount overlay, iterate on perceptron, no GitHub) vs
*production* (commit → push → GitHub Actions `build-image.yml` publishes the GHCR image →
`docker compose pull && up -d`). See memory `ognflights-deploy-modes` / `public-site-publishing`.
After a production deploy, reconcile perceptron's checkout (`git fetch && git merge --ff-only`).

## Open items
- Tune the winch/aerotow launch classifier against more known launches.
- (deferred) Give `/pick` the same month-calendar date picker as the replay (currently a native `<input type=date>`).
- (deferred) Live trails colour by the aircraft's *current* climb/speed (whole trail one colour); the replay does a per-vertex spatial gradient. Could bring the gradient to live.

## Conventions

- British English, no em dashes.
- **GitHub repo: `8none1/ognflights` (public).** Never push to `main` without asking
  (per Will's hard rule). The website (`8none1.github.io`) is a separate publish
  target; the `watch`/replay tooling and CI live in this repo.
- Site, geofence and thresholds live in `ognflights/config.py`.

#!/usr/bin/env python3
"""ognflights command-line interface.

  python3 cli.py collect [--minutes N] [--radius KM]      # stream OGN -> SQLite
  python3 cli.py aircraft --day YYYY-MM-DD                # what was seen that day
  python3 cli.py flights  --day YYYY-MM-DD [--reg G-XXXX] # list detected flights
  python3 cli.py export   --day YYYY-MM-DD --reg G-XXXX [--flights 4,5,6]
                          [--format gpx|kml|igc|all] [--outdir DIR]
"""
import argparse
import logging
import os
from datetime import datetime, timezone

from ognflights import export
from ognflights.collector import collect, watch
from ognflights.config import FILTER_RADIUS_KM, GRANSDEN
from ognflights.ddb import DDB
from ognflights.flights import classify_launch, segment
from ognflights.store import Store, store_for_day

DB_PATH = os.environ.get("OGNFLIGHTS_DB", "data/ogn.sqlite")


def _day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _hms(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def _day_store(a):
    """Store for a day: an explicit OGNFLIGHTS_DB (single file) wins, otherwise the
    year-partitioned file for --day (data/ogn-YYYY.sqlite)."""
    if os.environ.get("OGNFLIGHTS_DB"):
        return Store(DB_PATH)
    return store_for_day(_day(a.day))


def cmd_watch(a):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ddb = DDB(); ddb.load()
    status = {}
    hub = None
    if a.serve:
        import threading
        from ognflights import config, webapp
        hub = webapp.LiveHub()
        repo = os.path.dirname(os.path.abspath(__file__))
        replay_script = os.path.join(repo, "replay", "make_replay.py")
        models_dir = os.path.join(repo, "replay", "models")
        threading.Thread(
            target=webapp.serve,
            args=(a.port, status, config.DATA_DIR, replay_script, models_dir, hub),
            daemon=True).start()
        logging.info("web server on :%d  (/ = home, /replay, /live, /stats)", a.port)
    # Hourly public-data publisher (off unless OGNFLIGHTS_PUBLISH=1). Purely additive:
    # a daemon thread, fully isolated so a publish failure can never stall capture.
    from publish.worker import start_worker
    start_worker(status)
    n = watch(ddb, max_seconds=a.minutes * 60 if a.minutes else None,
              status=status, hub=hub)
    print(f"stored {n} fixes")


def cmd_healthcheck(a):
    """Probe the local /healthz endpoint; exit 0 if healthy, 1 otherwise.

    Used by the container HEALTHCHECK: it proves the whole chain (web server up
    AND the collector's backend link alive), not just that the process exists.
    Needs no extra tooling in the image - stdlib urllib only.
    """
    import sys
    import urllib.error
    import urllib.request
    url = f"http://127.0.0.1:{a.port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=a.timeout) as r:
            code, body = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:                # 503 = unhealthy, with a JSON body
        code = e.code
        body = e.read().decode("utf-8", "replace") if e.fp else str(e)
    except Exception as e:                             # server down / unreachable
        print(f"healthcheck: {e}", file=sys.stderr)
        return 1
    print(body)
    return 0 if code == 200 else 1


def cmd_collect(a):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store, ddb = Store(DB_PATH), DDB()
    ddb.load()
    n = collect(store, ddb, radius_km=a.radius,
                max_seconds=a.minutes * 60 if a.minutes else None)
    print(f"stored {n} fixes")


def cmd_aircraft(a):
    store = _day_store(a)
    rows = store.addresses_on_day(_day(a.day))
    print(f"{len(rows)} aircraft seen on {a.day}:")
    for addr, label, ac_type, count in rows:
        print(f"  {label:20} {ac_type:12} {count:6} fixes   ({addr})")


def _resolve_addresses(store, a):
    if a.reg:
        addrs = store.addresses_for_reg(a.reg)
        if not addrs:
            print(f"no aircraft matching '{a.reg}' in the store"); return []
        return addrs
    if a.address:
        return [a.address.upper()]
    return [r[0] for r in store.addresses_on_day(_day(a.day))]


def cmd_flights(a):
    store = _day_store(a)
    lo, hi = store.day_bounds(_day(a.day))
    for addr in _resolve_addresses(store, a):
        label, model = store.device_label(addr)
        flights = segment(addr, store.fixes_for(addr, lo, hi), GRANSDEN)
        if not flights:
            continue
        print(f"\n{label}  {model}")
        for i, fl in enumerate(flights, 1):
            launch = classify_launch(fl, GRANSDEN)
            print(f"  [{i}] {_hms(fl.start)}->{_hms(fl.end)}  "
                  f"{fl.duration_s//60}m{fl.duration_s%60:02d}s  "
                  f"max {int(fl.peak_alt_ft())}ft  {launch:7} {len(fl.fixes)} fixes")


def cmd_export(a):
    store = _day_store(a)
    lo, hi = store.day_bounds(_day(a.day))
    fmts = list(export.WRITERS) if a.format == "all" else [a.format]
    want = set(int(x) for x in a.flights.split(",")) if a.flights else None
    os.makedirs(a.outdir, exist_ok=True)
    written = 0
    for addr in _resolve_addresses(store, a):
        label, model = store.device_label(addr)
        flights = segment(addr, store.fixes_for(addr, lo, hi), GRANSDEN)
        for i, fl in enumerate(flights, 1):
            if want and i not in want:
                continue
            for fmt in fmts:
                doc = export.WRITERS[fmt](fl, label, model)
                path = os.path.join(a.outdir, export.filename(fl, label, fmt))
                with open(path, "w") as f:
                    f.write(doc)
                print("wrote", path); written += 1
    if not written:
        print("nothing matched")


def cmd_backfill(a):
    from ognflights import cgc
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cookie = a.cookie or os.environ.get("CAMGLIDING_COOKIE")
    if not cookie:
        print("need a CGC cookie: --cookie VALUE or CAMGLIDING_COOKIE env var"); return
    store = _day_store(a)
    n = cgc.backfill(store, _day(a.day), cookie)
    print(f"backfilled {n} fixes from CGC for {a.day}")


def cmd_publish(a):
    """Build the public per-day JSON + manifest for the last N days (Phase 1 pipeline)."""
    from datetime import timezone as _tz
    from publish.sync_public import sync, _commit
    from ognflights.config import DATA_DIR
    today = _day(a.today) if a.today else None
    written, manifest = sync(a.out, data_dir=a.data_dir or DATA_DIR, days=a.days,
                             models_url=a.models_url, today=today)
    print(f"days with flights: {len(manifest['days'])}; files written: "
          f"{', '.join(written) if written else '(none - all up to date)'}")
    for d in manifest["days"]:
        print(f"  {d['day']}  {d['flights']} flights, {d['aircraft']} aircraft")
    if a.commit:
        _commit(a.out, push=a.push)


def cmd_earth(a):
    """Dump every aircraft's full track for a day into one KML."""
    store = _day_store(a)
    lo, hi = store.day_bounds(_day(a.day))
    gliderish = {"glider", "tow", "motorglider"}
    tracks = []
    for addr, label, ac_type, _count in store.addresses_on_day(_day(a.day)):
        if a.gliders and ac_type not in gliderish:
            continue
        _, model = store.device_label(addr)
        fixes = store.fixes_for(addr, lo, hi)
        tracks.append((label, model or ac_type, fixes))
    if not tracks:
        print("no aircraft for that day"); return
    doc = export.kml_tracks(tracks, f"OGN capture {a.day}")
    with open(a.out, "w") as f:
        f.write(doc)
    print(f"wrote {a.out} ({len(tracks)} aircraft, {sum(len(t[2]) for t in tracks)} fixes)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="stream OGN feed into the store (legacy area capture)")
    c.add_argument("--minutes", type=int, help="stop after N minutes (default: run forever)")
    c.add_argument("--radius", type=int, default=FILTER_RADIUS_KM)
    c.set_defaults(func=cmd_collect)

    w = sub.add_parser("watch", help="buddy-follow daemon: track aircraft that launch from the field, anywhere")
    w.add_argument("--minutes", type=int, help="stop after N minutes (default: run forever)")
    w.add_argument("--serve", action="store_true", help="also serve the replay + /stats page over HTTP")
    w.add_argument("--port", type=int, default=8080, help="HTTP port for --serve (default 8080)")
    w.set_defaults(func=cmd_watch)

    hc = sub.add_parser("healthcheck", help="probe local /healthz; exit 0 healthy, 1 not (for Docker HEALTHCHECK)")
    hc.add_argument("--port", type=int, default=8080, help="HTTP port the dashboard serves on (default 8080)")
    hc.add_argument("--timeout", type=float, default=5, help="seconds to wait for /healthz (default 5)")
    hc.set_defaults(func=cmd_healthcheck)

    ac = sub.add_parser("aircraft", help="list aircraft seen on a day")
    ac.add_argument("--day", required=True)
    ac.set_defaults(func=cmd_aircraft)

    fl = sub.add_parser("flights", help="list detected flights")
    fl.add_argument("--day", required=True)
    fl.add_argument("--reg", help="registration/CN substring, e.g. G-CKFY or KFY")
    fl.add_argument("--address", help="OGN device hex id")
    fl.set_defaults(func=cmd_flights)

    ex = sub.add_parser("export", help="export flights to GPX/KML/IGC")
    ex.add_argument("--day", required=True)
    ex.add_argument("--reg")
    ex.add_argument("--address")
    ex.add_argument("--flights", help="comma list of flight numbers from `flights` (default: all)")
    ex.add_argument("--format", choices=[*export.WRITERS, "all"], default="all")
    ex.add_argument("--outdir", default=".")
    ex.set_defaults(func=cmd_export)

    bf = sub.add_parser("backfill", help="import a past day from the CGC tracking API")
    bf.add_argument("--day", required=True)
    bf.add_argument("--cookie", help="CGC session cookie (else CAMGLIDING_COOKIE env)")
    bf.set_defaults(func=cmd_backfill)

    pub = sub.add_parser("publish", help="build public per-day JSON + manifest for the dashboard")
    pub.add_argument("--out", required=True, help="output dir (or a public-data git worktree)")
    pub.add_argument("--data-dir", help="dir holding ogn-YYYY.sqlite (default: config DATA_DIR)")
    pub.add_argument("--days", type=int, default=7, help="how many days back to publish (default 7)")
    pub.add_argument("--models-url", default="models", help="relative URL to the .glb models")
    pub.add_argument("--today", help="override 'today' as YYYY-MM-DD (for testing)")
    pub.add_argument("--commit", action="store_true", help="git add/commit in --out (public-data worktree)")
    pub.add_argument("--push", action="store_true", help="with --commit, also push (Phase 2 only)")
    pub.set_defaults(func=cmd_publish)

    ea = sub.add_parser("earth", help="dump all tracks for a day into one KML")
    ea.add_argument("--day", required=True)
    ea.add_argument("--out", default="capture.kml")
    ea.add_argument("--gliders", action="store_true", help="only gliders/tugs (drop ADS-B airliners)")
    ea.set_defaults(func=cmd_earth)

    args = p.parse_args()
    raise SystemExit(args.func(args) or 0)


if __name__ == "__main__":
    main()

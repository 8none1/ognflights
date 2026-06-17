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
from ognflights.collector import collect
from ognflights.config import FILTER_RADIUS_KM, GRANSDEN
from ognflights.ddb import DDB
from ognflights.flights import classify_launch, segment
from ognflights.store import Store

DB_PATH = os.environ.get("OGNFLIGHTS_DB", "data/ogn.sqlite")


def _day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _hms(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def cmd_collect(a):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store, ddb = Store(DB_PATH), DDB()
    ddb.load()
    n = collect(store, ddb, radius_km=a.radius,
                max_seconds=a.minutes * 60 if a.minutes else None)
    print(f"stored {n} fixes")


def cmd_aircraft(a):
    store = Store(DB_PATH)
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
    store = Store(DB_PATH)
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
    store = Store(DB_PATH)
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


def cmd_earth(a):
    """Dump every aircraft's full track for a day into one KML."""
    store = Store(DB_PATH)
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

    c = sub.add_parser("collect", help="stream OGN feed into the store")
    c.add_argument("--minutes", type=int, help="stop after N minutes (default: run forever)")
    c.add_argument("--radius", type=int, default=FILTER_RADIUS_KM)
    c.set_defaults(func=cmd_collect)

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

    ea = sub.add_parser("earth", help="dump all tracks for a day into one KML")
    ea.add_argument("--day", required=True)
    ea.add_argument("--out", default="capture.kml")
    ea.add_argument("--gliders", action="store_true", help="only gliders/tugs (drop ADS-B airliners)")
    ea.set_defaults(func=cmd_earth)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

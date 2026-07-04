"""Tests for the watch() restart-recovery and hex-dedup fixes (stdlib unittest).

Run: python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ognflights import collector, config
from ognflights.aprs import Beacon
from ognflights.store import Store


def _beacon(source, address, addr_type, ts, lat, lon, alt_ft, ac_type="glider"):
    return Beacon(
        raw="", source=source, address=address, address_type=addr_type,
        aircraft_type=ac_type, stealth=False, no_track=False,
        ts=datetime.fromtimestamp(ts, tz=timezone.utc),
        lat=lat, lon=lon, altitude_ft=float(alt_ft),
        course=None, speed_kt=None, climb_fpm=None, receiver="TEST",
    )


class _FakeDDB:
    """Minimal DDB stand-in: resolve a couple of known hexes to registrations."""
    _known = {
        "DDB11F": ("G-CKFY", "KFY"),
        "405542": ("G-CHPX", "693"),
    }

    def lookup(self, address):
        from ognflights.ddb import Device
        r = self._known.get(address.upper())
        if not r:
            return None
        return Device(registration=r[0], cn=r[1], model="ASG29",
                      aircraft_type="glider", tracked=True, identified=True)


class _FakeClient:
    """Stand-in for OgnClient: replays a fixed list of beacons and records filters."""
    def __init__(self, beacons):
        self._beacons = beacons
        self.filters = []          # every filter string applied

    # watch() constructs OgnClient(build_filter(), reconnect=...); capture the first filter
    def __call__(self, filter_spec, reconnect=True):
        self.filters.append(filter_spec)
        return self

    def set_filter(self, spec):
        self.filters.append(spec)

    def beacons(self):
        yield from self._beacons


class SourceReconstructionTests(unittest.TestCase):
    def test_prefix_mapping(self):
        self.assertEqual(collector._source_for("DDB11F", "FLARM"), "FLRDDB11F")
        self.assertEqual(collector._source_for("405542", "ICAO"), "ICA405542")
        self.assertEqual(collector._source_for("abc123", "OGN"), "OGNABC123")

    def test_lowercase_type_ok(self):
        self.assertEqual(collector._source_for("DD8EE2", "flarm"), "FLRDD8EE2")

    def test_random_and_unknown_unreconstructable(self):
        self.assertIsNone(collector._source_for("204055", "RANDOM"))
        self.assertIsNone(collector._source_for("204055", ""))
        self.assertIsNone(collector._source_for("204055", "WHAT"))


class RecoveryQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "ogn.sqlite")
        self.store = Store(self.path)
        self.field = config.GRANSDEN.elevation_ft
        self.now = 1_800_000_000

    def tearDown(self):
        self.store.close()

    def _add(self, address, addr_type, ts, alt_ft):
        self.store.add_fix(_beacon("X" + address, address, addr_type, ts,
                                   52.0, -0.1, alt_ft))
        self.store.db.execute(
            "INSERT OR REPLACE INTO devices(address,address_type,last_seen) VALUES(?,?,?)",
            (address, addr_type, ts))
        self.store.commit()

    def test_airborne_recent_recovered_landed_and_old_skipped(self):
        # airborne + recent -> recovered
        self._add("DDB11F", "FLARM", self.now - 60, self.field + 3000)
        # recent but at/near field elevation (landed) -> skipped
        self._add("DD5258", "FLARM", self.now - 60, self.field + 50)
        # airborne but too old -> skipped
        self._add("405542", "ICAO", self.now - 7200, self.field + 3000)
        margin = self.field + config.RECOVERY_MIN_AGL_FT
        rows = self.store.recent_airborne(self.now, config.RECOVERY_WINDOW_S, margin)
        addrs = {r[0] for r in rows}
        self.assertEqual(addrs, {"DDB11F"})
        (addr, addr_type, last_ts, last_alt), = rows
        self.assertEqual(addr_type, "FLARM")
        self.assertEqual(last_ts, self.now - 60)

    def test_uses_only_the_latest_fix_altitude(self):
        # earlier airborne fix, then a later landed fix -> should be treated as landed
        self._add("DD8EE2", "FLARM", self.now - 300, self.field + 3000)
        self._add("DD8EE2", "FLARM", self.now - 30, self.field + 40)
        margin = self.field + config.RECOVERY_MIN_AGL_FT
        rows = self.store.recent_airborne(self.now, config.RECOVERY_WINDOW_S, margin)
        self.assertEqual(rows, [])


class WatchLogicTests(unittest.TestCase):
    """Drive watch() with a fake client + fake DDB, no sockets, bounded run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # point DATA_DIR at a temp dir so store_for_day writes there
        self._orig_data_dir = config.DATA_DIR
        config.DATA_DIR = self.tmp
        # store module caches DATA_DIR via import; year_file reads config.DATA_DIR live
        self.field = config.GRANSDEN.elevation_ft

    def tearDown(self):
        config.DATA_DIR = self._orig_data_dir

    def _run(self, beacons):
        fake = _FakeClient(beacons)
        orig = collector.OgnClient
        collector.OgnClient = fake
        try:
            n = collector.watch(_FakeDDB(), max_seconds=60)
        finally:
            collector.OgnClient = orig
        return n, fake

    def _launch_seq(self, source, address, addr_type, t0):
        """A low fix inside the geofence then a climb-out just above the ceiling."""
        lat, lon = config.LAUNCH_LAT, config.LAUNCH_LON
        low = _beacon(source, address, addr_type, t0, lat, lon, self.field + 50)
        high = _beacon(source, address, addr_type, t0 + 5, lat, lon,
                       self.field + config.LAUNCH_MAX_AGL_FT + 400)
        return [low, high]

    def test_flr_and_ica_same_hex_collapse_to_one(self):
        t0 = int(datetime.now(timezone.utc).timestamp()) - 10
        beacons = []
        # launch heard via FLARM link
        beacons += self._launch_seq("FLR405542", "405542", "FLARM", t0)
        # same aircraft now also heard via ADS-B link (ICA), same hex, airborne
        beacons.append(_beacon("ICA405542", "405542", "ICAO", t0 + 7,
                               config.LAUNCH_LAT, config.LAUNCH_LON,
                               self.field + 800))
        n, fake = self._run(beacons)
        # exactly one hex followed; buddy filter carries BOTH source variants
        last_filter = fake.filters[-1]
        self.assertIn("b/", last_filter)
        self.assertIn("FLR405542", last_filter)
        self.assertIn("ICA405542", last_filter)
        # DB: one address, and no duplicate rows for shared timestamps
        s = Store(os.path.join(self.tmp,
                  f"ogn-{datetime.now(timezone.utc).year}.sqlite"))
        try:
            addrs = [r[0] for r in s.db.execute(
                "SELECT DISTINCT address FROM fixes").fetchall()]
        finally:
            s.close()
        self.assertEqual(addrs, ["405542"])

    def test_two_distinct_aircraft_stay_separate(self):
        t0 = int(datetime.now(timezone.utc).timestamp()) - 10
        beacons = []
        beacons += self._launch_seq("FLR405542", "405542", "FLARM", t0)
        beacons += self._launch_seq("FLRDDB11F", "DDB11F", "FLARM", t0 + 1)
        n, fake = self._run(beacons)
        last_filter = fake.filters[-1]
        self.assertIn("FLR405542", last_filter)
        self.assertIn("FLRDDB11F", last_filter)
        s = Store(os.path.join(self.tmp,
                  f"ogn-{datetime.now(timezone.utc).year}.sqlite"))
        try:
            addrs = {r[0] for r in s.db.execute(
                "SELECT DISTINCT address FROM fixes").fetchall()}
        finally:
            s.close()
        self.assertEqual(addrs, {"405542", "DDB11F"})

    def test_recovery_seeds_owned_and_initial_buddy_filter(self):
        # pre-seed the year DB with one airborne-recent aircraft (should recover),
        # one landed-recent (skip) and one RANDOM airborne-recent (skip: no source).
        year = datetime.now(timezone.utc).year
        path = os.path.join(self.tmp, f"ogn-{year}.sqlite")
        now = int(datetime.now(timezone.utc).timestamp())
        s = Store(path)
        for addr, atype, dt, alt in [
            ("DDB11F", "FLARM", -120, self.field + 3000),   # recover
            ("DD5258", "FLARM", -120, self.field + 30),     # landed -> skip
            ("204055", "RANDOM", -120, self.field + 3000),  # anon -> skip
        ]:
            s.add_fix(_beacon("X", addr, atype, now + dt, 52.0, -0.1, alt))
            s.db.execute("INSERT OR REPLACE INTO devices(address,address_type,last_seen)"
                         " VALUES(?,?,?)", (addr, atype, now + dt))
        s.commit(); s.close()
        # run with NO beacons: the only source of `owned` is recovery.
        n, fake = self._run([])
        initial_filter = fake.filters[0]
        self.assertIn("b/FLRDDB11F", initial_filter)
        self.assertNotIn("DD5258", initial_filter)   # landed, not re-acquired
        self.assertNotIn("204055", initial_filter)   # RANDOM, cannot reconstruct

    def test_launch_detection_still_fires(self):
        t0 = int(datetime.now(timezone.utc).timestamp()) - 10
        n, fake = self._run(self._launch_seq("FLRDDB11F", "DDB11F", "FLARM", t0))
        # a launch stores the buffered low fix + the climb-out => at least 2 fixes
        self.assertGreaterEqual(n, 2)
        self.assertTrue(any("b/FLRDDB11F" in f for f in fake.filters))


if __name__ == "__main__":
    unittest.main()

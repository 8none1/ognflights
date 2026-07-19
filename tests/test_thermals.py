"""Tests for thermal-hotspot detection (synthetic fixes, so rot is populated).

Run: python3 -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ognflights import thermals
from ognflights.config import GRANSDEN

LAT, LON = GRANSDEN.lat, GRANSDEN.lon
import math
COSL = math.cos(math.radians(LAT))


def _ll(east_m, north_m):
    return (LAT + north_m / 111320.0, LON + east_m / (111320.0 * COSL))


def _build_db(path):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE fixes(address TEXT, ts INTEGER, lat REAL, lon REAL, alt_ft REAL,
        speed_kt INTEGER, climb_fpm INTEGER, receiver TEXT, course INTEGER, rot REAL,
        PRIMARY KEY(address, ts))""")
    db.execute("""CREATE TABLE devices(address TEXT PRIMARY KEY, address_type TEXT,
        aircraft_type TEXT, registration TEXT, cn TEXT, model TEXT, last_seen INTEGER)""")
    day0 = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp())

    def add_fix(addr, ts, east, north, alt, climb, rot):
        lat, lon = _ll(east, north)
        db.execute("INSERT OR IGNORE INTO fixes VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (addr, ts, lat, lon, alt, 48, climb, "RX", 90, rot))

    # THERMAL A at (+600 E, 0): 6 gliders, each on its own day, circling + climbing.
    # High-alt fixes shifted +200 m north => expect a northward drift.
    for i in range(6):
        g = "GLID%02d" % i
        db.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?)",
                   (g, "FLARM", "glider", "G-TST%d" % i, None, None, day0))
        base_ts = day0 + i * 86400 + 43200
        for k in range(30):
            alt = 1600 + k * 60                      # climbing 1600 -> ~3340 ft
            north = 0 + (200 if k > 15 else 0)       # drift north as it climbs
            add_fix(g, base_ts + k * 5, 600 + (k % 3) * 20, north, alt, 400, 6.0)

    # AEROTOW B at (-800 E, 0): a glider climbing + circling, but a tug is co-located
    # at every fix => must be excluded (not a thermal).
    db.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?)",
               ("GAERO", "FLARM", "glider", "G-AERO", None, None, day0))
    db.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?)",
               ("TUG1", "FLARM", "tow", "G-TUG", None, None, day0))
    for k in range(40):
        ts = day0 + 40000 + k * 4
        alt = 800 + k * 40
        add_fix("GAERO", ts, -800, 0, alt, 450, 6.0)
        add_fix("TUG1", ts, -800 + 30, 0, alt + 20, 450, 5.0)   # within 250 m / 300 ft / 12 s
    db.commit()
    db.close()


class ThermalDetectionTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        _build_db(os.path.join(self.d, "ogn-2026.sqlite"))
        self.lo = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp())
        self.hi = self.lo + 8 * 86400

    def test_finds_the_thermal_and_excludes_the_aerotow(self):
        hs = thermals.compute(self.d, GRANSDEN, self.lo, self.hi)
        self.assertEqual(len(hs), 1, "should find exactly the one recurring thermal")
        h = hs[0]
        # located near +600 m east of the field (aerotow at -800 E must be absent)
        east = (h["lon"] - LON) * 111320.0 * COSL
        self.assertGreater(east, 300)
        self.assertEqual(h["ac_days"], 6)
        self.assertAlmostEqual(h["climb_kt"], 400 / 101.3, places=1)

    def test_drift_points_north(self):
        h = thermals.compute(self.d, GRANSDEN, self.lo, self.hi)[0]
        self.assertGreater(h["drift_m"], 80)          # high-alt centroid shifted ~200 m
        self.assertTrue(h["drift_deg"] < 45 or h["drift_deg"] > 315, "≈ north")

    def test_store_round_trip(self):
        hs = thermals.compute(self.d, GRANSDEN, self.lo, self.hi)
        db = thermals.open_store(self.d)
        thermals.save(db, self.lo, self.hi, hs)
        back = thermals.load(db)
        db.close()
        self.assertEqual(len(back), len(hs))
        self.assertEqual(back[0]["ac_days"], hs[0]["ac_days"])


if __name__ == "__main__":
    unittest.main()

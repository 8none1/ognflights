"""Tests for native OGN field capture (course/rot) and the climb-rate ramp.

Run: python3 -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ognflights.aprs import parse_beacon
from ognflights.store import Store
from replay.make_replay import climb_rates_kt

# A real-shape OGN APRS line carrying course (090), speed (045 kt), climb (-019 fpm) and rot (+2.5).
_LINE = ("FLRDD8EE2>APRS,qAS,UKGRLLP:/165422h5210.98N/00006.71W'090/045/"
         "A=000236 !W58! id06DD8EE2 -019fpm +2.5rot")


class ParseTests(unittest.TestCase):
    def test_course_and_rot_parsed(self):
        b = parse_beacon(_LINE)
        self.assertEqual(b.course, 90)
        self.assertEqual(b.speed_kt, 45)
        self.assertEqual(b.climb_fpm, -19)
        self.assertEqual(b.turn_rate, 2.5)

    def test_turn_rate_optional(self):
        # a fix with no rot token still parses, turn_rate is None
        line = _LINE.rsplit(" ", 1)[0]  # drop the "+2.5rot"
        b = parse_beacon(line)
        self.assertIsNone(b.turn_rate)


class StoreTests(unittest.TestCase):
    def test_fresh_round_trip(self):
        d = tempfile.mkdtemp()
        s = Store(os.path.join(d, "fresh.sqlite"))
        b = parse_beacon(_LINE)
        self.assertTrue(s.add_fix(b))
        s.commit()
        ts = int(b.ts.timestamp())
        fx = s.fixes_for(b.address, ts - 10, ts + 10)[0]
        self.assertEqual(fx.course, 90)
        self.assertEqual(fx.rot, 2.5)
        s.close()

    def test_migrates_old_schema(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "old.sqlite")
        old = sqlite3.connect(p)
        old.execute(
            "CREATE TABLE fixes (address TEXT NOT NULL, ts INTEGER NOT NULL, lat REAL, "
            "lon REAL, alt_ft REAL, speed_kt INTEGER, climb_fpm INTEGER, receiver TEXT, "
            "PRIMARY KEY(address, ts))")
        old.execute("INSERT INTO fixes VALUES ('OLD', 1000, 52.1, -0.1, 500, 40, -100, 'RX')")
        old.commit()
        old.close()

        s = Store(p)  # opening should ALTER TABLE to add course + rot
        cols = {r[1] for r in s.db.execute("PRAGMA table_info(fixes)").fetchall()}
        self.assertIn("course", cols)
        self.assertIn("rot", cols)
        # legacy row preserved with NULL course/rot
        old_fx = s.fixes_for("OLD", 0, 2000)[0]
        self.assertIsNone(old_fx.course)
        self.assertIsNone(old_fx.rot)
        self.assertEqual(old_fx.speed_kt, 40)
        # new inserts work on the migrated file
        self.assertTrue(s.add_fix(parse_beacon(_LINE)))
        s.close()


class ClimbRampTests(unittest.TestCase):
    class _F:
        def __init__(self, ts, alt_ft):
            self.ts = ts
            self.alt_ft = alt_ft

    def test_sign_follows_altitude(self):
        climbing = [self._F(i * 4, 1000 + i * 40) for i in range(6)]
        sinking = [self._F(i * 4, 2000 - i * 80) for i in range(6)]
        self.assertGreater(climb_rates_kt(climbing)[3], 0)
        self.assertLess(climb_rates_kt(sinking)[3], 0)

    def test_degenerate_inputs(self):
        self.assertEqual(climb_rates_kt([]), [])
        self.assertEqual(climb_rates_kt([self._F(0, 100)]), [0.0])


if __name__ == "__main__":
    unittest.main()

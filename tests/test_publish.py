"""Tests for the public-data publisher: day-planning and accumulate/no-prune behaviour.

Run: python3 -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import publish.sync_public as sp
from publish.worker import _day_plan

# A prebuilt legacy single-file DB with flights on 2026-07-01 and 2026-06-17.
_FIXTURE = "/tmp/ogn-test.sqlite"


def _D(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


class DayPlanTests(unittest.TestCase):
    NOW = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    def _days(self, dl):
        return [d.strftime("%Y-%m-%d") for d in dl]

    def test_cold_start_builds_today_and_yesterday(self):
        dl, flatten, td = _day_plan(self.NOW, None)
        self.assertEqual(self._days(dl), ["2026-07-19", "2026-07-18"])
        self.assertFalse(flatten)

    def test_same_day_builds_today_only(self):
        dl, flatten, td = _day_plan(self.NOW, datetime(2026, 7, 19).date())
        self.assertEqual(self._days(dl), ["2026-07-19"])
        self.assertFalse(flatten)

    def test_rollover_finalises_prev_day_and_flattens(self):
        dl, flatten, td = _day_plan(self.NOW, datetime(2026, 7, 18).date())
        self.assertEqual(self._days(dl), ["2026-07-19", "2026-07-18"])
        self.assertTrue(flatten)

    def test_multi_day_gap_rebuilds_all_missed_days(self):
        dl, flatten, td = _day_plan(self.NOW, datetime(2026, 7, 16).date())
        self.assertEqual(self._days(dl),
                         ["2026-07-19", "2026-07-18", "2026-07-17", "2026-07-16"])
        self.assertTrue(flatten)


@unittest.skipUnless(os.path.exists(_FIXTURE), "needs /tmp/ogn-test.sqlite fixture")
class PublishDaysTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp()
        shutil.copy(_FIXTURE, os.path.join(self.data_dir, "ogn-2026.sqlite"))
        self.out = tempfile.mkdtemp()

    def _days(self, manifest):
        return {e["day"] for e in manifest["days"]}

    def test_accumulates_across_runs_without_pruning(self):
        sp.publish_days(self.out, data_dir=self.data_dir, day_list=[_D("2026-07-01")])
        _, m = sp.publish_days(self.out, data_dir=self.data_dir, day_list=[_D("2026-06-17")])
        # both days present in the manifest and on disk (older one NOT pruned)
        self.assertEqual(self._days(m), {"2026-07-01", "2026-06-17"})
        self.assertTrue(os.path.exists(os.path.join(self.out, "2026-07-01.json")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "2026-06-17.json")))

    def test_empty_day_adds_nothing_and_prunes_nothing(self):
        sp.publish_days(self.out, data_dir=self.data_dir, day_list=[_D("2026-07-01")])
        _, m = sp.publish_days(self.out, data_dir=self.data_dir, day_list=[_D("2026-07-05")])
        self.assertEqual(self._days(m), {"2026-07-01"})
        self.assertTrue(os.path.exists(os.path.join(self.out, "2026-07-01.json")))


if __name__ == "__main__":
    unittest.main()

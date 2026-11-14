"""Marathon slots (owner 2026-11-14: "no limit", picked "hourly, no daily cap"). Offline."""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CLIPBOT_ID", '{"login": "x", "brand": "X", "twitch_uid": "1", "channel": "UCx", "ig": "", "legacy": []}')

from clipbot import common as c  # noqa: E402


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Slots(unittest.TestCase):
    def fill(self, n, **kw):
        taken = []
        for _ in range(n):
            taken.append(iso(c.next_slot(taken, [], **kw)))
        return [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in taken]

    def test_marathon_is_hourly_with_no_daily_cap(self):
        ts = self.fill(40, marathon=True, min_lead_h=1)
        uk = [t.astimezone(c.UK) for t in ts]
        self.assertTrue(all(9 <= t.hour <= 23 and t.minute == 0 for t in uk), uk)
        self.assertEqual(ts, sorted(ts), "earliest free hour first")
        per_day = {}
        for t in uk:
            per_day[t.date()] = per_day.get(t.date(), 0) + 1
        full = [n for d, n in per_day.items() if d != uk[0].date() and d != uk[-1].date()]
        self.assertTrue(full and all(n == 15 for n in full), per_day)
        self.assertTrue(all((b - a) >= timedelta(minutes=c.MARATHON_GAP_MIN) for a, b in zip(ts, ts[1:])))
        self.assertGreaterEqual(ts[0], datetime.now(c.UTC) + timedelta(hours=1))

    def test_marathon_skips_an_hour_already_taken(self):
        first = c.next_slot([], [], marathon=True, min_lead_h=1)
        nxt = c.next_slot([iso(first)], [], marathon=True, min_lead_h=1)
        self.assertEqual(nxt - first, timedelta(hours=1))

    def test_normal_and_catchup_unchanged(self):
        uk = [t.astimezone(c.UK) for t in self.fill(10)]
        self.assertTrue(all(t.strftime("%H:%M") in ("18:00", "13:00") for t in uk))
        uk = [t.astimezone(c.UK) for t in self.fill(12, catchup=True, min_lead_h=1)]
        self.assertTrue(all(t.strftime("%H:%M") in ("18:00", "13:00", "21:00", "10:00") for t in uk))
        per_day = {}
        for t in uk:
            per_day[t.date()] = per_day.get(t.date(), 0) + 1
        self.assertTrue(all(n <= 4 for n in per_day.values()), per_day)


if __name__ == "__main__":
    unittest.main()

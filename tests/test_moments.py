"""/moment (Discord helper kit, 2026-10-29): marked moments always become clips in the live scan."""
import unittest
from unittest import mock

from clipbot import find_vod_clips as f


def words(n=400, step=1.5):
    return [{"w": f"w{i}", "s": i * step, "e": i * step + 0.5} for i in range(n)]


class Moments(unittest.TestCase):
    def test_window_filter_and_lane(self):
        r = {"moments": [{"t": 7300, "note": "heist  goes wrong"}, {"t": 100, "note": "before"}, {"t": "x"}]}
        with mock.patch.object(f.common, "worker", return_value=r), mock.patch.object(f.common, "WHO", "leon"):
            self.assertEqual(f.moments("1", 7200, 600), [{"t": 100.0, "note": "heist goes wrong"}])
        with mock.patch.object(f.common, "worker", return_value=r), mock.patch.object(f.common, "WHO", "glatiar"):
            self.assertEqual(f.moments("1", 7200, 600), [])

    def test_worker_down_means_no_moments(self):
        with mock.patch.object(f.common, "worker", side_effect=OSError("down")), mock.patch.object(f.common, "WHO", "leon"):
            self.assertEqual(f.moments("1", 0, 600), [])

    def test_looks_back_and_titles_from_note(self):
        ws = words()
        wins = [{"s": 200.0, "e": 215.0, "score": 9.0, "peak": -10}, {"s": 400.0, "e": 410.0, "score": 20.0, "peak": -5}]
        out = f.moment_cands([{"t": 240.0, "note": "heist goes wrong"}], wins, ws, 600, [])
        self.assertEqual(len(out), 1)
        self.assertLessEqual(out[0]["s"], 200.0)          # took the window before the mark, not the stronger later one
        self.assertEqual(out[0]["note"], "Heist goes wrong")

    def test_no_window_falls_back_to_30s_before(self):
        out = f.moment_cands([{"t": 300.0, "note": "lol"}], [], words(), 600, [])
        self.assertTrue(out and out[0]["s"] < 300.0 <= out[0]["e"] + 3)

    def test_skips_twitch_clipped_and_duplicates(self):
        ws = words()
        self.assertEqual(f.moment_cands([{"t": 300.0, "note": "x"}], [], ws, 600, [(280.0, 310.0)]), [])
        two = f.moment_cands([{"t": 300.0, "note": "a"}, {"t": 305.0, "note": "b"}], [], ws, 600, [])
        self.assertEqual(len(two), 1)

    def test_cap(self):
        marks = [{"t": 60.0 * i, "note": f"m{i}"} for i in range(1, 11)]
        self.assertEqual(len(f.moment_cands(marks, [], words(), 600, [])), f.MO_MAX)


if __name__ == "__main__":
    unittest.main()

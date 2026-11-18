"""Marathon mode on Kick (boss 18 Nov: the Twitch-only check returned False for the whole Kick marathon, so Shorts would
have gone out 2 a day instead of hourly). Offline: the Kick video list is faked."""
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CLIPBOT_ID", '{"login": "x", "brand": "X", "twitch_uid": "1", "channel": "UCx", "ig": "", "legacy": []}')

from clipbot import common as c  # noqa: E402
from polished import source  # noqa: E402

H = 3600


def kick(start_ago_h, dur_h=0.0, live=False):
    """One entry as kick.com/api/v2/channels/<slug>/videos gives it: UTC "YYYY-MM-DD HH:MM:SS", duration in ms
    (0 while live)."""
    st = datetime.fromtimestamp(time.time() - start_ago_h * H, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return {"start_time": st, "duration": int(dur_h * H * 1000), "is_live": live}


class KickMarathon(unittest.TestCase):
    def check(self, vids, who="leon"):
        with mock.patch.object(c, "WHO", who), mock.patch.object(source, "kick_videos", return_value=vids) as kv:
            return c.marathon_mode(), kv.called

    def test_before_the_marathon_is_false(self):
        self.assertEqual(self.check([kick(30, 3), kick(60, 4)])[0], False)
        self.assertEqual(self.check([])[0], False)

    def test_live_marathon_counts_once_it_passes_10h(self):
        self.assertTrue(self.check([kick(11, live=True)])[0])
        self.assertFalse(self.check([kick(2, live=True)])[0])     # launch night, first hours: still 2 a day

    def test_finished_marathon_counts_for_72h(self):
        self.assertTrue(self.check([kick(40, 12)])[0])            # ended 28 h ago
        self.assertFalse(self.check([kick(100, 12)])[0])          # ended 88 h ago
        self.assertFalse(self.check([kick(5, 4)])[0])             # a normal 4 h stream

    def test_any_error_is_false(self):
        with mock.patch.object(c, "WHO", "leon"), \
                mock.patch.object(source, "kick_videos", side_effect=RuntimeError("Kick slug 'x' is channel 9, not ours")):
            self.assertFalse(c.marathon_mode())

    def test_unreadable_start_is_skipped(self):
        self.assertFalse(self.check([{"start_time": "", "duration": 0, "is_live": True}])[0])

    def test_glatiar_never_asks_kick(self):
        on, asked = self.check([kick(11, live=True)], who="glatiar")
        self.assertFalse(on)
        self.assertFalse(asked)


if __name__ == "__main__":
    unittest.main()

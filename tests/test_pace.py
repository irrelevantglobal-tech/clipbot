"""Leon 24 Nov: spread the posts out (3 h apart, 4 a day per lane, platforms staggered) and use a real cover frame."""
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CLIPBOT_ID", '{"login": "x", "brand": "X", "twitch_uid": "1", "channel": "UCx", "ig": "", "legacy": []}')

from clipbot import common, instagram  # noqa: E402


def iso(h_ago):
    return (datetime.now(UTC) - timedelta(hours=h_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fake(own, today=0, others=None):
    others = others or {}

    def w(path, *a, **k):
        lane = path.split("lane=")[1]
        return {"at": own if lane == "ig" else others.get(lane), "today": today if lane == "ig" else 0}
    return w


class Pace(unittest.TestCase):
    def test_three_hours_apart(self):
        with mock.patch.object(common, "worker", fake(iso(2.5))):
            self.assertFalse(common.pace_ok("ig"))
        with mock.patch.object(common, "worker", fake(iso(3.2))):
            self.assertTrue(common.pace_ok("ig"))

    def test_four_a_day_cap(self):
        with mock.patch.object(common, "worker", fake(iso(5), today=4)):
            self.assertFalse(common.pace_ok("ig"))
        with mock.patch.object(common, "worker", fake(iso(5), today=3)):
            self.assertTrue(common.pace_ok("ig"))

    def test_other_platform_just_posted_makes_this_lane_wait(self):
        with mock.patch.object(common, "worker", fake(iso(5), others={"x": iso(0.3)})):
            self.assertFalse(common.pace_ok("ig"))
        with mock.patch.object(common, "worker", fake(iso(5), others={"x": iso(1.5)})):
            self.assertTrue(common.pace_ok("ig"))

    def test_unreadable_own_record_posts_nothing(self):
        def boom(path, *a, **k):
            raise RuntimeError("down")
        with mock.patch.object(common, "worker", boom):
            self.assertFalse(common.pace_ok("ig"))


class Cover(unittest.TestCase):
    def test_studio_cover_wins_default_is_30_percent(self):
        self.assertEqual(instagram.cover_ms({"cover_ms": 7300}, "x.mp4"), 7300)
        with mock.patch.object(instagram.render, "duration", return_value=40.0):
            self.assertEqual(instagram.cover_ms({}, "x.mp4"), 12000)
            self.assertEqual(instagram.cover_ms({"cover_ms": "junk"}, "x.mp4"), 12000)

    def test_reel_container_carries_thumb_offset(self):
        with mock.patch.object(instagram, "post_video", return_value=("1", "l")) as pv:
            instagram.post_reel({}, "x.mp4", "cap", True, {"cover_ms": 5000})
        self.assertEqual(pv.call_args[0][2]["thumb_offset"], "5000")


class Copy24Nov(unittest.TestCase):
    def test_instagram_keeps_five_hashtags(self):
        cap = "hook\n\nLive on Kick: kick.com/x\n\n#a #b #c #d #e #f #a #g"
        out = instagram.cap_tags(cap)
        self.assertEqual(out.split("\n\n")[-1].split(), ["#a", "#b", "#c", "#d", "#e"])
        self.assertIn("Live on Kick: kick.com/x", out)

    def test_x_text_replaces_the_hook_line_and_is_checked(self):
        from clipbot import x
        it = {"title": "GTA 6: T", "tiktok_caption": "old hook\n\n#gta6", "x_text": "Would you have jumped too?"}
        t = x.x_text(it)
        self.assertTrue(t.startswith("Would you have jumped too?"))
        self.assertNotIn("old hook", t)
        self.assertFalse(common.copy_ok({**it, "copy_std": "1119", "x_text": "see twinkiesghost on twitch.tv"}))


if __name__ == "__main__":
    unittest.main()


class TikTokNeverBlocksReelsOrX(unittest.TestCase):
    """25 Nov: TikTok refreshes its record every hour at :40, IG/X run at :06 -> a TikTok stagger starved them (0 posts)."""
    def test_recent_tiktok_does_not_block_ig_or_x(self):
        from datetime import datetime, timedelta
        from unittest import mock
        from clipbot import common
        now = datetime(2026, 11, 25, 11, 6, tzinfo=common.UTC)
        recs = {"ig": {"at": None, "today": 0}, "x": {"at": None, "today": 0},
                "tt": {"at": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"), "today": 2}}
        with mock.patch.object(common, "worker", lambda path, *a, **k: recs[path.rsplit("=", 1)[-1]]):
            self.assertTrue(common.pace_ok("ig", now=now))
            self.assertTrue(common.pace_ok("x", now=now))
            recs["ig"]["at"] = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
            self.assertFalse(common.pace_ok("x", now=now))   # Reels <-> X still staggered

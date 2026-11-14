"""Episode Stories (owner 2026-11-14). Offline: which uploads count, and the card renders inside Instagram's safe area."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CLIPBOT_ID", '{"login": "x", "brand": "X", "twitch_uid": "1", "channel": "UCx", "ig": "", "legacy": []}')

from clipbot import common, ep_story  # noqa: E402

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=common.UTC)


def up(vid, hours_ago, dur="PT40M", privacy="public", title="GTA 6 Part 3", desc=""):
    return {"id": vid, "status": {"privacyStatus": privacy}, "contentDetails": {"duration": dur},
            "snippet": {"title": title, "description": desc, "publishedAt": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")}}


class Pick(unittest.TestCase):
    def test_only_fresh_public_long_unsponsored(self):
        ups = [up("aaaaaaaaaaa", 1), up("bbbbbbbbbbb", 2, dur="PT58S"), up("ccccccccccc", 3, privacy="private"),
               up("ddddddddddd", 8), up("eeeeeeeeeee", 4, desc="#ad thanks to the sponsor"), up("fffffffffff", 5)]
        self.assertEqual([v["id"] for v in ep_story.new_episodes(ups, NOW)], ["fffffffffff", "aaaaaaaaaaa"])

    def test_card_fits(self):
        from PIL import Image
        from clipbot.style import gta_style
        d = tempfile.mkdtemp()
        src = os.path.join(d, "t.jpg")
        Image.new("RGB", (1280, 720), "#3366aa").save(src)
        out = os.path.join(d, "s.png")
        gta_style.episode_story(src, out=out)
        self.assertEqual(Image.open(out).size, (1080, 1920))


if __name__ == "__main__":
    unittest.main()

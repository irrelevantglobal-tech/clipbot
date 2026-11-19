"""Kick chat activity as the chat signal (19 Nov): relay per-minute counts -> the same timeline format as Twitch chat."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CLIPBOT_ID", '{"login": "x", "brand": "X", "twitch_uid": "1", "channel": "UCx", "ig": "", "legacy": []}')

from clipbot import chat  # noqa: E402

START = 1795045774


class KickChat(unittest.TestCase):
    def test_minutes_spread_reactions_first_in_vod_seconds(self):
        asked = []

        def get(path):
            asked.append(path)
            return {"ok": True, "minutes": [[START + 600, 4, 1], [START + 660, 2, 2]]}
        tl = chat.kick_timeline(str(START), 590, 720, get=get)
        self.assertIn(f"from={START + 590}", asked[0])
        self.assertEqual(len(tl), 6)
        self.assertEqual([round(t, 1) for t, _ in tl[:4]], [607.5, 622.5, 637.5, 652.5])
        self.assertEqual(sum(r for _, r in tl), 3)
        n, r = chat.around(tl, 600, 610)
        self.assertGreater(n, 0)
        self.assertLessEqual(chat.boost(n, r), 2.0)

    def test_any_failure_is_no_boost(self):
        def boom(path):
            raise RuntimeError("relay down")
        self.assertEqual(chat.kick_timeline(str(START), 0, 3600, get=boom), [])
        self.assertEqual(chat.kick_timeline("k123", 0, 3600, get=lambda p: {}), [])


if __name__ == "__main__":
    unittest.main()

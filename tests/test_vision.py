"""Vision check of machine picks (19 Nov quality check): verdict parsing and the fail-closed paths, offline."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CLIPBOT_ID", '{"login": "x", "brand": "X", "twitch_uid": "1", "channel": "UCx", "ig": "", "legacy": []}')

from clipbot import vision  # noqa: E402


class Parse(unittest.TestCase):
    def test_pass_reject_trim(self):
        self.assertEqual(vision.parse('{"verdict":"reject","reason":"radio wheel","title":"x","trim":null}', 20)["verdict"], "reject")
        p = vision.parse('Sure! {"verdict":"pass","reason":"ok","title":"I Ran Over A Tiger On The Freeway","trim":null}', 20)
        self.assertEqual((p["verdict"], p["title"]), ("pass", "I Ran Over A Tiger On The Freeway"))
        t = vision.parse('{"verdict":"trim","reason":"dead air first","title":"T","trim":[9.5, 21]}', 22)
        self.assertEqual(t["trim"], [9.5, 21.0])

    def test_bad_trim_and_garbage(self):
        self.assertEqual(vision.parse('{"verdict":"trim","trim":[18, 21]}', 22)["verdict"], "pass")     # < 8 s: keep whole
        self.assertEqual(vision.parse('{"verdict":"trim","trim":"soon"}', 22)["verdict"], "pass")
        self.assertIsNone(vision.parse("I think it is fine", 20))
        self.assertIsNone(vision.parse('{"verdict":"maybe"}', 20))
        self.assertEqual(len(vision.parse('{"verdict":"pass","title":"' + "A" * 80 + '"}', 20)["title"]), 50)

    def test_any_failure_is_unchecked_never_pass(self):
        with mock.patch.object(vision, "sheet", return_value=20.0), \
                mock.patch.object(vision, "budget", side_effect=RuntimeError("no GTA6_ANTHROPIC_KEY")):
            self.assertEqual(vision.check("x.mp4", "t", "words", workdir=".")["verdict"], "unchecked")

        class B:
            def call(self, *a, **k):
                return "no json here", 0.0004
        with mock.patch.object(vision, "sheet", return_value=20.0), mock.patch.object(vision, "budget", return_value=B()):
            r = vision.check("x.mp4", "t", "words", workdir=".")
        self.assertEqual(r["verdict"], "unchecked")


if __name__ == "__main__":
    unittest.main()

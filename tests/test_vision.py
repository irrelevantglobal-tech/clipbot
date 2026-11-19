"""Vision gate + copy writer (19 Nov quality check / Leon's copy standard): parsing, copy, and the fail-closed paths, offline."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CLIPBOT_ID", '{"login": "x", "brand": "X", "twitch_uid": "1", "channel": "UCx", "ig": "", "legacy": []}')

from clipbot import vision  # noqa: E402

FENCED = ('Sure! ```json\n{"verdict":"pass","reason":"ok","moment":"I Ran Over A Tiger On The Freeway","tag":"#Tiger Run!",'
          '"line":"I did not see it coming","hook":"why was there a tiger","trim":null,}\n```')


class Copy(unittest.TestCase):
    def test_leon_standard(self):
        vz = {"moment": "I Ran Over A Tiger On The Freeway", "line": "I did not see that tiger coming", "tag": "#TigerRunOver",
              "hook": "why is there a tiger on the freeway"}
        c = vision.copy_for(vz, "GTA 6", "OwnerBrand")
        self.assertEqual(c["title"], "GTA 6: I Ran Over A Tiger On The Freeway")
        self.assertEqual(c["hashtags"], ["#GTA6", "#GTAVI", "#TigerRunOver", "#Shorts", "#OwnerBrand"])
        self.assertEqual(c["tiktok_hashtags"], ["#gta6", "#gtavi", "#tigerrunover", "#gaming", "#ownerbrand"])
        self.assertEqual(c["description"], "I did not see that tiger coming")
        self.assertEqual(c["tiktok_hook"], "why is there a tiger on the freeway")
        self.assertIsNone(vision.copy_for({"moment": None}, "GTA 6", "B"))

    def test_game_names(self):
        self.assertEqual(vision.game_name("Grand Theft Auto VI"), "GTA 6")
        self.assertEqual(vision.game_name("WELCOME TO VICE CITY, GTA 6 LAUNCH MARATHON DAY 1"), "GTA 6")
        self.assertEqual(vision.game_name("Cyberpunk 2077"), "Cyberpunk 2077")
        self.assertEqual(vision.game_name("a very long stream title that is not a game name at all"), "")
        self.assertEqual(vision.game_tags("Cyberpunk 2077"), ["#Cyberpunk2077"])


class Parse(unittest.TestCase):
    def test_pass_reject_trim(self):
        self.assertEqual(vision.parse('{"verdict":"reject","reason":"radio wheel","trim":null}', 20)["verdict"], "reject")
        p = vision.parse(FENCED, 20)
        self.assertEqual((p["verdict"], p["moment"], p["tag"]), ("pass", "I Ran Over A Tiger On The Freeway", "#TigerRun"))
        t = vision.parse('{"verdict":"trim","reason":"dead air first","moment":"T","trim":[9.5, 21]}', 22)
        self.assertEqual(t["trim"], [9.5, 21.0])

    def test_bad_trim_caps_and_garbage(self):
        self.assertEqual(vision.parse('{"verdict":"trim","trim":[18, 21]}', 22)["verdict"], "pass")     # < 8 s: keep whole
        self.assertEqual(vision.parse('{"verdict":"trim","trim":"soon"}', 22)["verdict"], "pass")
        self.assertIsNone(vision.parse("I think it is fine", 20))
        self.assertIsNone(vision.parse('{"verdict":"maybe"}', 20))
        self.assertEqual(len(vision.parse('{"verdict":"pass","moment":"' + "A" * 80 + '"}', 20)["moment"]), 55)
        self.assertEqual(vision.parse('{"verdict":"pass","moment":"THIS GAME IS TOO COOL"}', 20)["moment"], "This Game Is Too Cool")

    def test_any_failure_is_unchecked_never_pass(self):
        with mock.patch.object(vision, "sheet", return_value=20.0), \
                mock.patch.object(vision, "_ask", side_effect=RuntimeError("no credentials")):
            self.assertEqual(vision.check("x.mp4", "t", "words", workdir=".")["verdict"], "unchecked")
        with mock.patch.object(vision, "sheet", return_value=20.0), \
                mock.patch.object(vision, "_ask", return_value=("no json here", 0.0004, "gemini x")):
            self.assertEqual(vision.check("x.mp4", "t", "words", workdir=".")["verdict"], "unchecked")


if __name__ == "__main__":
    unittest.main()

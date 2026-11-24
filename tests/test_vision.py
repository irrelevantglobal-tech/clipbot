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
        dup = vision.copy_for({"moment": "Cyberpunk 2077: Getting Crushed By Adam Smasher"}, "Cyberpunk 2077", "B")
        self.assertEqual(dup["title"], "Cyberpunk 2077: Getting Crushed By Adam Smasher")

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


class Sheet(unittest.TestCase):
    def test_vertical_clip_gets_vertical_tiles(self):
        """19 Nov: 16:9 tiles round a 9:16 Short left black bars the model called 'black frames'."""
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("ffmpeg"):
            self.skipTest("no ffmpeg")
        d = tempfile.mkdtemp()
        try:
            src = os.path.join(d, "v.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=108x192:d=2", "-pix_fmt", "yuv420p",
                            src], check=True)
            vision.sheet(src, os.path.join(d, "s.jpg"))
            from PIL import Image
            im = Image.open(os.path.join(d, "s.jpg")).convert("RGB")
            self.assertEqual(im.size, (4 * vision.H, 2 * vision.W))
            self.assertGreater(im.getpixel((10, 240))[0], 150)          # tile edge is picture, not a black bar
        finally:
            shutil.rmtree(d, ignore_errors=True)


class CopyRules(unittest.TestCase):
    """Leon's 19 Nov copy rules, coded 23 Nov (all 18 queued rows failed them)."""

    def good(self, **kw):
        it = {"title": "GTA 6: Boat Flips Into The Marina", "description": "I flipped it.", "copy_std": vision.COPY_STD,
              "hashtags": ["#GTA6", "#GTAVI", "#BoatFlip", "#Shorts", "#GigglesGhost"],
              "tiktok_caption": "Wait for it\n\n#gta6 #gtavi #boatflip", "tiktok_hashtags": ["#gta6", "#gtavi", "#boatflip"]}
        it.update(kw)
        return it

    def test_clean_item_passes_even_in_gta6_only_mode(self):
        from clipbot import common
        with mock.patch.dict(os.environ, {"CLIPBOT_GTA6_ONLY": "1"}):
            self.assertEqual(common.copy_problems(self.good()), [])
            self.assertTrue(common.copy_ok(self.good()))

    def test_other_games_old_twitch_line_and_missing_gta6_are_refused(self):
        from clipbot import common
        with mock.patch.dict(os.environ, {"CLIPBOT_GTA6_ONLY": "1"}):
            for bad in (self.good(hashtags=["#GTA6", "#GTAVI", "#gtav"]), self.good(title="GTA 6: Sea of Thieves Boat"),
                        self.good(description="Live on Twitch: twinkiesghost"), self.good(hashtags=["#Shorts"]),
                        self.good(title="Boat Flips Into The Marina")):
                self.assertFalse(common.copy_ok(bad), bad)
        self.assertFalse(common.copy_ok(self.good(copy_std="")))
        self.assertFalse(common.copy_ok(self.good(hashtags=["#ARK"])))
        self.assertTrue(common.copy_ok(self.good(description="a dark park at night")))    # no false hit on substrings

    def test_cast_name_needs_on_screen_evidence_and_is_otherwise_no_copy(self):
        vz = {"verdict": "pass", "moment": "Lucia Steals A Boat", "line": "I helped her.", "tag": "#BoatTheft", "hook": "Go Lucia", "names_seen": []}
        self.assertEqual(vision.unseen_names(vz), ["Lucia Caminos"])
        self.assertIsNone(vision.copy_for(vz, "GTA 6", "GigglesGhost"))
        self.assertIsNotNone(vision.copy_for({**vz, "names_seen": ["Lucia Caminos"]}, "GTA 6", "GigglesGhost"))
        self.assertIsNotNone(vision.copy_for({**vz, "moment": "The Girl Steals A Boat", "hook": "Go go go"}, "GTA 6", "GigglesGhost"))

    def test_gta6_copy_carries_gta6_tags(self):
        c = vision.copy_for({"verdict": "pass", "moment": "Boat Flips", "line": "x", "tag": "#BoatFlip", "hook": None}, "GTA 6", "GigglesGhost")
        self.assertTrue(c["title"].startswith("GTA 6: "))
        self.assertEqual(c["hashtags"][:2], ["#GTA6", "#GTAVI"])
        self.assertEqual(c["tiktok_hashtags"][:2], ["#gta6", "#gtavi"])


if __name__ == "__main__":
    unittest.main()

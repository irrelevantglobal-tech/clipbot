"""Camp stings (owner's pick 12 Nov): exact contract lengths, rotation, the retired-name guard, the media fetcher,
and the series index the conductor hands the intro.

    py -m unittest tests.test_camp_stings -v      (needs ffmpeg on PATH)
"""
import _ident  # noqa: F401
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from polished.brand import camp


def probe(p):
    r = json.loads(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
                                   "-of", "json", p], capture_output=True, text=True).stdout)
    v = next(s for s in r["streams"] if s["codec_type"] == "video")
    a = next(s for s in r["streams"] if s["codec_type"] == "audio")
    return v, a, float(r["format"]["duration"])


HAVE_MEDIA = not __import__("polished.brand.camp_fetch", fromlist=["x"]).missing()


@unittest.skipUnless(HAVE_MEDIA, "camp sting media not fetched (python -m polished.brand.camp_fetch)")
class CampStings(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        camp.DRAWN.clear()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_intro_contract(self):
        p = camp.intro("The Adventure Begins", "GTA 6", _ident.BRAND, _ident.LOGIN, self.d, [_ident.OLD])
        v, a, dur = probe(p)
        self.assertEqual((v["width"], v["height"], v["r_frame_rate"]), (1920, 1080, "60/1"))
        self.assertEqual(int(v["nb_read_frames"]), 276)               # 4.6 s x 60, the INTRO_D contract
        self.assertEqual(a["sample_rate"], "48000")
        self.assertAlmostEqual(dur, 4.6, delta=0.03)
        with open(os.path.join(self.d, "_drawn.json"), encoding="utf-8") as f:
            drawn = json.load(f)
        self.assertIn(_ident.BRAND.upper(), drawn)

    def test_outro_contract(self):
        p = camp.outro("GTA 6", _ident.BRAND, _ident.LOGIN, self.d, [_ident.OLD])
        v, a, dur = probe(p)
        self.assertEqual(int(v["nb_read_frames"]), 1200)              # 20.0 s x 60, the OUTRO_D contract
        self.assertAlmostEqual(dur, 20.0, delta=0.03)
        with open(os.path.join(self.d, "_drawn.json"), encoding="utf-8") as f:
            self.assertIn("TWITCH.TV/" + _ident.LOGIN.upper(), json.load(f))

    def test_retired_name_refused_everywhere(self):
        old = _ident.OLD
        split = "%s %s.%s" % (old[:2], old[2:4], old[4:])               # spaced/dotted still counts
        for fn in (lambda: camp.intro("Visiting " + split, "GTA 6", _ident.BRAND, _ident.LOGIN, self.d, [old]),
                   lambda: camp.intro("Ok", "GTA 6", old, _ident.LOGIN, self.d, [old]),
                   lambda: camp.outro("GTA 6", _ident.BRAND, old.lower(), self.d, [old]),
                   lambda: camp.outro(old, _ident.BRAND, _ident.LOGIN, self.d, [old])):
            with self.assertRaises(SystemExit):
                fn()
        self.assertEqual([f for f in os.listdir(self.d) if f.endswith(".mp4")], [])

    def test_refuses_without_a_list(self):
        with self.assertRaises(SystemExit):
            camp.intro("Ok", "GTA 6", _ident.BRAND, _ident.LOGIN, self.d, [""])

    def test_long_title_and_game_fit_the_frame(self):
        lines, k = camp.fit(camp.clean("Assassin's Creed Rogue Remastered Deluxe Edition"), (3, 2, 1))
        self.assertTrue(all(camp.width(ln, k) <= camp.MAXW for ln in lines))
        lines, _ = camp.fit(camp.clean("A very long episode title " * 4), (1,))
        self.assertLessEqual(len(lines), 3)
        self.assertTrue(all(camp.width(ln) <= camp.MAXW for ln in lines))

    def test_game_picks_its_camp_and_rotation_never_repeats(self):
        self.assertEqual(camp.camp_for("GTA 6"), "gta")
        self.assertEqual(camp.camp_for("Grand Theft Auto VI"), "gta")
        self.assertEqual(camp.camp_for("Cyberpunk 2077"), "cp")
        self.assertEqual(camp.camp_for("Assassin's Creed Rogue"), "autumn")
        for game in ("GTA 6", "Cyberpunk 2077", "Some Other Game"):
            seq = [camp.pick_variant(game, "t", i)["id"] for i in range(30)]
            self.assertTrue(all(a != b for a, b in zip(seq, seq[1:])), game)          # never twice in a row
            self.assertEqual(seq, [camp.pick_variant(game, "t", i)["id"] for i in range(30)])   # same index = same shot
        for vs in camp.VARIANTS.values():
            for v in vs:
                self.assertTrue(os.path.exists(os.path.join(camp.ASSETS, v["file"])), v["file"])

    def test_unsupported_characters_are_dropped_not_boxed(self):
        self.assertEqual(camp.clean("Café — “quotes” (x)"), "CAF - QUOTES X")


if __name__ == "__main__":
    unittest.main()


class CampFetch(unittest.TestCase):
    """The media is fetched by URL and must match manifest.json to the byte; nothing unchecked is ever kept."""
    def setUp(self):
        from polished.brand import camp_fetch
        self.cf = camp_fetch
        self.d = tempfile.mkdtemp()
        self.src = tempfile.mkdtemp()
        self.blob = b"camp footage bytes" * 100
        open(os.path.join(self.src, "x.mp4"), "wb").write(self.blob)
        import hashlib
        json.dump({"version": "t", "files": {"x.mp4": {"sha256": hashlib.sha256(self.blob).hexdigest(), "bytes": len(self.blob)}}},
                  open(os.path.join(self.d, "manifest.json"), "w"))
        self.url = "file:///" + self.src.replace("\\", "/").lstrip("/")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
        shutil.rmtree(self.src, ignore_errors=True)

    def test_fetches_and_then_is_a_no_op(self):
        self.assertEqual(self.cf.ensure(self.d, self.url, ""), 1)
        self.assertEqual(self.cf.ensure(self.d, "", ""), 0)            # present + exact: no URL needed

    def test_tampered_download_is_refused_and_not_kept(self):
        open(os.path.join(self.src, "x.mp4"), "wb").write(self.blob[:-1] + b"!")
        with self.assertRaises(SystemExit):
            self.cf.ensure(self.d, self.url, "")
        self.assertEqual(sorted(os.listdir(self.d)), ["manifest.json"])

    def test_missing_without_a_url_refuses(self):
        with self.assertRaises(SystemExit):
            self.cf.ensure(self.d, "", "")

    def test_token_never_in_the_error(self):
        with self.assertRaises(SystemExit) as e:
            self.cf.ensure(self.d, "file:///nonexistent-dir-xyz", "SECRET-TOKEN-123")
        self.assertNotIn("SECRET-TOKEN-123", str(e.exception))


class SeriesIndex(unittest.TestCase):
    """The intro rotation's index: earlier episodes of the same series, ordered by (VOD, start)."""
    def base(self, items, vod="200", start=500.0, series="GTA 6"):
        from polished import conductor

        class C:
            @staticmethod
            def worker(path):
                if items is None:
                    raise RuntimeError("down")
                return {"items": items}
        return conductor.series_base(C, series, vod, start)

    def test_counts_earlier_episodes_of_this_series_only(self):
        items = [{"marker": "clipbot:ep:100:10", "series": "GTA 6"}, {"marker": "clipbot:ep:100:4000", "series": "GTA 6"},
                 {"marker": "clipbot:ep:200:20", "series": "GTA 6"}, {"marker": "clipbot:ep:200:500", "series": "GTA 6"},
                 {"marker": "clipbot:ep:200:900", "series": "GTA 6"}, {"marker": "clipbot:ep:150:5", "series": "Other"}]
        self.assertEqual(self.base(items), 3)                          # 100:10, 100:4000, 200:20; not itself, not later
        self.assertEqual(self.base(items), self.base(items))           # a re-run gets the same index

    def test_unreadable_list_falls_back(self):
        self.assertIsNone(self.base(None))

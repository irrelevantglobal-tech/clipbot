"""Kick pivot (17 Nov): clipbot/kick_clips.py. No network, no ffmpeg/whisper: the clip list, the worker claim, the
download and the render are fakes. Run: python -m unittest tests.test_kick_clips"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CLIPBOT_ID", json.dumps({"login": "chan", "brand": "Brand", "twitch_uid": "1", "channel": "UCx"}))
from clipbot import kick_clips as K  # noqa: E402

NOW = 1795000000.0


def clip(i, age_min, title="boat flip", url="https://clips.kick.com/x/playlist.m3u8"):
    return {"id": f"clip_{i:026d}", "title": title, "created": NOW - age_min * 60, "duration": 20.0, "url": url,
            "creator": "viewer%d" % i, "livestream_id": "1"}


class Run(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.claimed = []
        self.listed = []
        self.dur = 25.0
        self.fail_ids = set()
        self.hooks = []
        self.vz = lambda raw, title, transcript, workdir=None, game="": {
            "verdict": "pass", "reason": "ok", "trim": None, "moment": "Boat Flips Into The Marina", "line": "I flipped it.",
            "tag": "#BoatFlip", "hook": "Wait for the splash", "title": "Boat Flips Into The Marina", "cost": 0.0, "lane": "test"}

        def worker(path, body=None):
            assert path == "/clipbot/kickclips"
            self.claimed.append([c["id"] for c in body["clips"]])
            return {"fresh": [c["id"] for c in body["clips"]][:2]}           # the worker says the first two are new

        def download(url, out):
            if any(i in url for i in self.fail_ids):
                raise RuntimeError("Kick clip is 1280x720, not 1920x1080")
            open(out, "wb").write(b"x")

        def rend(raw, out, prev, hook, words):
            self.hooks.append(hook)
            open(out, "wb").write(b"v")
            return {"duration": self.dur, "w": 1080, "h": 1920}
        self.p = [mock.patch.object(K.source, "kick_clips", side_effect=lambda since: [c for c in self.listed if c["created"] >= since]),
                  mock.patch.object(K.common, "worker", side_effect=worker),
                  mock.patch.object(K, "download", side_effect=download),
                  mock.patch.object(K.render, "require_1080", return_value=(1920, 1080, 60.0)),
                  mock.patch.object(K.render, "duration", side_effect=lambda p: self.dur),
                  mock.patch.object(K.render, "transcribe", return_value=[{"w": "hello", "s": 0, "e": 1}]),
                  mock.patch.object(K.render, "render", side_effect=rend),
                  mock.patch.object(K.vision, "check", side_effect=lambda *a, **k: self.vz(*a, **k)),
                  mock.patch.object(K.common, "clip_copy", return_value={"description": "d", "hashtags": ["#gta6"], "tags": [], "game": ""}),
                  mock.patch.object(K.time, "time", return_value=NOW)]
        for p in self.p:
            p.start()
            self.addCleanup(p.stop)
        self.gh = os.path.join(self.d, "gh_out")
        os.environ["GITHUB_OUTPUT"] = self.gh
        self.addCleanup(os.environ.pop, "GITHUB_OUTPUT", None)

    def run_it(self, *args):
        with redirect_stdout(io.StringIO()):
            rc = K.main(["--out", os.path.join(self.d, "out"), *args])
        man = os.path.join(self.d, "out", "manifest.json")
        return rc, (json.load(open(man, encoding="utf-8")) if os.path.exists(man) else None), open(self.gh).read()

    def test_new_settled_clips_are_claimed_rendered_and_offered_as_kick_clips(self):
        self.listed = [clip(1, 60), clip(2, 30), clip(3, 20), clip(4, 5)]     # clip 4 is 5 min old: not settled
        rc, man, out = self.run_it()
        self.assertEqual(rc, 0)
        self.assertEqual(self.claimed, [[clip(1, 0)["id"], clip(2, 0)["id"], clip(3, 0)["id"]]])
        self.assertEqual([c["slug"] for c in man["clips"]], [clip(1, 0)["id"], clip(2, 0)["id"]])   # only what the worker said is new
        self.assertEqual((man["kind"], man["platform"], man["vod_title"]), ("clips", "kick", "Kick clips"))
        self.assertEqual(man["clips"][0]["creator"], "viewer1")
        self.assertIn("batch=%d" % NOW, out)

    def test_nothing_new_exits_cleanly_with_no_batch(self):
        self.listed = [clip(4, 5)]
        rc, man, out = self.run_it()
        self.assertEqual((rc, man, self.claimed), (0, None, []))
        self.assertIn("batch=\n", out)                                         # the release + offer steps are skipped

    def test_a_soft_clip_is_skipped_the_rest_still_go(self):
        self.listed = [clip(1, 60), clip(2, 30)]
        self.fail_ids = {"x"}
        self.listed[0]["url"] = "https://clips.kick.com/bad/x/playlist.m3u8"
        self.listed[1]["url"] = "https://clips.kick.com/good/playlist.m3u8"
        self.fail_ids = {"/bad/"}
        rc, man, _ = self.run_it()
        self.assertEqual([c["n"] for c in man["clips"]], [1])
        self.assertEqual(man["clips"][0]["slug"], clip(2, 0)["id"])

    def test_a_whole_vod_posing_as_a_clip_is_refused(self):
        self.listed = [clip(1, 60)]
        self.dur = 600.0
        rc, man, _ = self.run_it()
        self.assertEqual((rc, man), (0, None))

    def test_rerun_by_id_does_not_claim(self):
        self.listed = [clip(1, 60), clip(2, 30)]
        rc, man, _ = self.run_it("--clips", clip(2, 0)["id"])
        self.assertEqual(self.claimed, [])
        self.assertEqual([c["slug"] for c in man["clips"]], [clip(2, 0)["id"]])

    def test_viewer_title_is_a_record_only_never_the_title_or_the_burned_in_hook(self):
        self.listed = [clip(1, 60, title="He made it on Claude"), clip(2, 30, title="Sea of Thieves lol")]
        for c in self.listed:
            c["category"] = "Grand Theft Auto VI"
        rc, man, _ = self.run_it()
        self.assertEqual(len(man["clips"]), 2)
        for c in man["clips"]:
            self.assertNotEqual(c["title"].lower(), c["viewer_title"].lower())
            self.assertNotIn(c["viewer_title"].lower(), c["title"].lower())
            self.assertTrue(c["title"].startswith("GTA 6: "))
            self.assertEqual(c["copy_std"], K.vision.COPY_STD)
            self.assertIn("#GTA6", c["hashtags"])
        self.assertEqual(man["clips"][0]["viewer_title"], "He made it on Claude")
        for h in self.hooks:
            self.assertNotIn(h.lower(), ("he made it on claude", "sea of thieves lol"))
            self.assertTrue(h.startswith("GTA 6: "))

    def test_no_vision_copy_means_no_hook_no_copy_std_and_never_the_viewer_title(self):
        self.listed = [clip(1, 60, title="He made it on Claude")]
        self.vz = lambda *a, **k: {"verdict": "reject", "reason": "menu", "trim": None, "moment": None, "line": None, "tag": None,
                                   "hook": None, "title": None, "cost": 0.0, "lane": "test"}
        rc, man, _ = self.run_it()
        c = man["clips"][0]
        self.assertEqual(self.hooks, [""])
        self.assertNotIn("copy_std", c)
        self.assertEqual(c["hashtags"], [])
        self.assertEqual(c["description"], "")
        self.assertNotIn("claude", c["title"].lower())
        self.assertEqual(c["viewer_title"], "He made it on Claude")

    def test_unchecked_vision_is_treated_as_no_copy(self):
        self.listed = [clip(1, 60, title="Hello")]
        self.vz = lambda *a, **k: {"verdict": "unchecked", "reason": "boom", "trim": None, "moment": None, "line": None, "tag": None,
                                   "hook": None, "title": None, "cost": 0.0, "lane": None}
        rc, man, _ = self.run_it()
        self.assertEqual((self.hooks, "copy_std" in man["clips"][0]), ([""], False))


if __name__ == "__main__":
    unittest.main()

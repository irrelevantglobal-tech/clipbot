"""Real-name guard (boss 19 Nov), offline. The real terms live only in the CLIPBOT_REALNAME secret; these tests use a made-up
name. The OCR itself runs on the runner (realname-probe.yml)."""
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CLIPBOT_ID", '{"login": "x", "brand": "X", "twitch_uid": "1", "channel": "UCx", "ig": "", "legacy": []}')

from clipbot import episodes, realname as rn  # noqa: E402

FAKE = "Jane Doelittle,Doelittle"


class Guard(unittest.TestCase):
    def test_terms_come_from_the_secret_only(self):
        with mock.patch.dict(os.environ, {"CLIPBOT_REALNAME": FAKE}):
            self.assertEqual(rn.terms(), ["janedoelittle", "doelittle"])
        with mock.patch.dict(os.environ, {"CLIPBOT_REALNAME": ""}):
            self.assertEqual(rn.terms(), [])
        self.assertEqual(rn.terms("ab, , x"), [])            # too short to match safely: ignored

    def test_ocr_noise_still_matches(self):
        w = rn.terms(FAKE)
        hits = ["JANE DOELITTLE", "Jane  Doe1ittle", "J a n e D o e l i t t l e", "jane\ndoelittle", "D0ELITTLE"]
        miss = ["Jane Doe", "GigglesGhost", "Vice City", "", "doe little league"[:3]]
        self.assertEqual(rn.count_hits(hits, w), len(hits))
        self.assertEqual(rn.count_hits(miss, w), 0)

    def test_fail_closed(self):
        with mock.patch.dict(os.environ, {"CLIPBOT_REALNAME": ""}):
            r = rn.check("nothing.mp4")
        self.assertFalse(r["ok"])
        self.assertIn("no CLIPBOT_REALNAME", r["error"])
        with mock.patch.dict(os.environ, {"CLIPBOT_REALNAME": FAKE}), mock.patch.object(rn.shutil, "which", return_value=None):
            r = rn.check("nothing.mp4")
        self.assertFalse(r["ok"])
        with mock.patch.dict(os.environ, {"CLIPBOT_REALNAME": FAKE}), mock.patch.object(rn.shutil, "which", return_value="/x"), \
                mock.patch.object(rn, "_frames", return_value=[]), mock.patch.object(rn, "_ocr", return_value=[]):
            r = rn.check("empty.mp4")
        self.assertFalse(r["ok"])                              # zero frames read is never "clear"
        with mock.patch.dict(os.environ, {"CLIPBOT_REALNAME": FAKE}), mock.patch.object(rn.shutil, "which", return_value="/x"), \
                mock.patch.object(rn, "_frames", side_effect=RuntimeError("ffmpeg died")):
            r = rn.check("bad.mp4")
        self.assertFalse(r["ok"])

    def test_clear_and_hit(self):
        env = {"CLIPBOT_REALNAME": FAKE}
        with mock.patch.dict(os.environ, env), mock.patch.object(rn.shutil, "which", return_value="/x"), \
                mock.patch.object(rn, "_frames", return_value=["a", "b", "c"]), \
                mock.patch.object(rn, "_ocr", return_value=["WASTED", "", "Vice City"]):
            self.assertTrue(rn.check("ok.mp4")["ok"])
        with mock.patch.dict(os.environ, env), mock.patch.object(rn.shutil, "which", return_value="/x"), \
                mock.patch.object(rn, "_frames", return_value=["a", "b"]), \
                mock.patch.object(rn, "_ocr", return_value=["Who's playing?", "JANE DOELITTLE  Online"]):
            r = rn.check("login.mp4")
        self.assertFalse(r["ok"])
        self.assertEqual(r["hits"], 1)
        self.assertNotIn("doelittle", rn.line(r).lower())     # the log line never carries the name

    def test_gate_holds_only_that_lane_and_passes_others(self):
        from clipbot import common
        sent = []
        with mock.patch.object(common, "WHO", "leon"), mock.patch.object(common, "worker", lambda p, b=None: sent.append((p, b)) or {"ok": True}),                 mock.patch.object(rn, "check", return_value={"ok": False, "frames": 15, "text_frames": 9, "hits": 1, "error": None}):
            self.assertFalse(rn.gate("c.mp4", "vod:1:1", "ig"))
        self.assertEqual(sent[0][0], "/clipbot/hold")
        self.assertEqual((sent[0][1]["id"], sent[0][1]["lane"]), ("vod:1:1", "ig"))
        with mock.patch.object(common, "WHO", "leon"),                 mock.patch.object(rn, "check", return_value={"ok": True, "frames": 15, "text_frames": 9, "hits": 0, "error": None}):
            self.assertTrue(rn.gate("c.mp4", "vod:1:1", "tt"))
        with mock.patch.object(common, "WHO", "glatiar"), mock.patch.object(rn, "check", side_effect=AssertionError("not his lane")):
            self.assertTrue(rn.gate("c.mp4", "x", "tt"))

    def test_quota_reset_is_pacific_midnight(self):
        self.assertEqual(episodes.quota_reset(datetime(2026, 11, 19, 5, 30, tzinfo=timezone.utc)).isoformat(), "2026-11-19T08:00:00+00:00")
        self.assertEqual(episodes.quota_reset(datetime(2026, 11, 19, 8, 30, tzinfo=timezone.utc)).isoformat(), "2026-11-20T08:00:00+00:00")


if __name__ == "__main__":
    unittest.main()

"""Reels lane (2026-10-29). Offline: worker, Graph API and the clip file are stubbed.

    py -m unittest tests.test_instagram -v
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("CLIPBOT_TOKEN", "tok")
from clipbot import common, instagram, publish, tiktok  # noqa: E402

UTC = common.UTC


class Fake:
    def __init__(self, items, recent=(), tok_age_days=1, login=True):
        self.calls = []
        self.items = items
        self.recent = list(recent)
        self.tok = {"token": "T1", "user_id": "42", "username": "twinkiesghost",
                    "at": (datetime.now(UTC) - timedelta(days=tok_age_days)).isoformat()}
        self.login = login

    def worker(self, path, payload=None, method=None):
        self.calls.append(("worker", path, payload, method))
        if path == "/clipbot/igqueue":
            return {"items": self.items, "login": self.login}
        if path == "/clipbot/igtoken" and payload is None:
            return self.tok
        return {"ok": True}

    def http(self, url, data=None, headers=None, method=None, timeout=60, retries=3):
        self.calls.append(("http", url.split("?")[0], data, retries))
        if url.startswith("https://graph.instagram.com/refresh_access_token"):
            return {"access_token": "T2", "expires_in": 5184000}
        if url.endswith("/clipbot/igfile"):
            return {"key": "k" * 32, "url": "https://w/clipbot/igfile/" + "k" * 32}
        if "/42/media?" in url:
            return {"data": self.recent}
        if url.endswith("/42/media"):
            return {"id": "C1"}
        if "/C1?" in url:
            return {"status_code": "FINISHED"}
        if url.endswith("/42/media_publish"):
            return {"id": "M1"}
        if "/M1?" in url:
            return {"permalink": "https://www.instagram.com/reel/X/"}
        raise AssertionError("unexpected " + url)


def item(i, hours_ago):
    return {"id": f"vod:1:{i}", "n": i, "title": f"Clip {i}", "run_id": 1, "batch": "1",
            "at": (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tiktok_caption": f"hook number {i} here\n\n#gaming #fyp #clips"}


class ReelsTest(unittest.TestCase):
    def run_main(self, fake, dry="0"):
        tmp = tempfile.mkdtemp()
        clip = os.path.join(tmp, "clip.mp4")
        for name in ("clip.mp4", "clip2.mp4", "poster.jpg"):
            with open(os.path.join(tmp, name), "wb") as f:
                f.write(b"\0" * 100)
        saved = (common.worker, common.http, publish.video_file, os.environ.get("IG_DRY"))
        common.worker, common.http, publish.video_file = fake.worker, fake.http, lambda it, w: clip
        os.environ["IG_DRY"] = dry
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            instagram.main()
        finally:
            os.chdir(cwd)
            common.worker, common.http, publish.video_file = saved[:3]
            os.environ["IG_DRY"] = saved[3] or ""

    def test_posts_due_only_oldest_first_cap_two(self):
        f = Fake([item(1, 2), item(2, 30), item(3, 1), item(4, -5)])
        self.run_main(f)
        done = [c[2]["id"] for c in f.calls if c[1] == "/clipbot/igdone"]
        self.assertEqual(done, ["vod:1:2", "vod:1:1"])          # 4 is in the future, 3 waits for the next run
        pub = [c for c in f.calls if c[0] == "http" and c[1].endswith("media_publish")]
        self.assertEqual(len(pub), 4)                            # 2 Reels + their 2 Stories
        self.assertTrue(all(c[3] == 0 for c in pub), "media_publish is never retried")
        self.assertTrue(any(c[1] == "/clipbot/igfile?key=" + "k" * 32 and c[3] == "DELETE" for c in f.calls))

    def test_caption_twitch_line_no_filler_tags_and_token_not_in_urls(self):
        f = Fake([item(1, 1)])
        self.run_main(f)
        create = next(c for c in f.calls if c[0] == "http" and c[1].endswith("/42/media") and c[2])
        body = create[2].decode()
        self.assertIn("Live+on+Twitch%3A+twinkiesghost", body)
        for filler in ("%23reels", "%23fyp", "%23gaming+", "%23gaming%0A"):
            self.assertNotIn(filler, body)
        self.assertIn("%23twinkiesghost", body)
        self.assertIn("access_token=T1", body)
        self.assertFalse(any("T1" in c[1] for c in f.calls if c[0] == "http"))

    def test_approved_ig_caption_is_used_verbatim(self):
        it = dict(item(1, 1), ig_caption="POV: you trust me with petrol\n\nLive on Twitch: twinkiesghost\n\n#arcticdrive #twinkiesghost")
        self.assertEqual(instagram.caption_for(it), it["ig_caption"])

    def test_old_clip_gets_fresh_ig_copy_from_its_transcript(self):
        import json as _json
        work = tempfile.mkdtemp()
        os.makedirs(os.path.join(work, "art-1"))
        with open(os.path.join(work, "art-1", "manifest.json"), "w", encoding="utf-8") as f:
            _json.dump({"vod_title": "stream", "clips": [{"n": 1, "quote": "did I just drink the gas", "game": "Arctic Drive"}]}, f)
        saved = common.worker
        seen = {}

        def fake(path, payload=None, method=None):
            seen["payload"] = payload
            return {"ig_hook": "Who let me near the petrol can?", "ig_hashtags": ["#arcticdrive", "#gaming", "#petrolstation", "#carfail"]}
        common.worker = fake
        try:
            cap = instagram.caption_for(item(1, 1), work)
        finally:
            common.worker = saved
        self.assertEqual(seen["payload"]["transcript"], "did I just drink the gas")
        self.assertEqual(cap, "Who let me near the petrol can?\n\nLive on Twitch: twinkiesghost\n\n#arcticdrive #petrolstation #carfail #twinkiesghost")

    def test_duplicate_caption_is_not_reposted(self):
        cap = instagram.caption_for(item(1, 1))
        f = Fake([item(1, 1)], recent=[{"id": "OLD", "caption": cap, "permalink": "p"}])
        self.run_main(f)
        self.assertFalse(any(c[1].endswith("/42/media") and c[2] for c in f.calls if c[0] == "http"))
        done = next(c[2] for c in f.calls if c[1] == "/clipbot/igdone")
        self.assertEqual((done["media_id"], done["dup"]), ("OLD", True))

    def test_stale_slot_skipped(self):
        f = Fake([item(1, 24 * 4)])
        self.run_main(f)
        self.assertFalse(any(c[1].endswith("media_publish") for c in f.calls if c[0] == "http"))
        self.assertEqual(next(c[2] for c in f.calls if c[1] == "/clipbot/igdone")["dup"], True)

    def test_old_token_refreshed_and_saved(self):
        f = Fake([item(1, 1)], tok_age_days=8)
        self.run_main(f)
        saved = next(c[2] for c in f.calls if c[1] == "/clipbot/igtoken" and c[2])
        self.assertEqual(saved["token"], "T2")
        create = next(c for c in f.calls if c[0] == "http" and c[1].endswith("/42/media") and c[2])
        self.assertIn("access_token=T2", create[2].decode())

    def test_dry_run_publishes_nothing_and_reports_nothing(self):
        f = Fake([item(1, 1)])
        self.run_main(f, dry="1")
        self.assertFalse(any(c[1].endswith("media_publish") for c in f.calls if c[0] == "http"))
        self.assertFalse(any(c[1] == "/clipbot/igdone" for c in f.calls))

    def test_each_new_reel_also_goes_out_as_a_story(self):
        f = Fake([item(1, 1)])
        self.run_main(f)
        made = [c[2].decode() for c in f.calls if c[0] == "http" and c[1].endswith("/42/media") and c[2]]
        self.assertEqual(len(made), 2)
        self.assertIn("media_type=REELS", made[0])
        self.assertIn("media_type=STORIES", made[1])
        self.assertNotIn("caption=", made[1])
        names = [c[1] for c in f.calls]
        self.assertLess(names.index("/clipbot/igdone"), len(names) - 1 - names[::-1].index("/clipbot/igfile?key=" + "k" * 32),
                        "the Reel is recorded done before the Story is attempted")

    def test_failed_story_is_reported_and_never_fails_the_reel(self):
        f = Fake([item(1, 1)])
        real = f.http
        n = {"media": 0}

        def http(url, data=None, **kw):
            if url.endswith("/42/media") and data:
                n["media"] += 1
                if n["media"] == 2:
                    raise RuntimeError("HTTP 400 story rejected")
            return real(url, data, **kw)
        f.http = http
        self.run_main(f)                                         # no SystemExit
        self.assertEqual(next(c[2] for c in f.calls if c[1] == "/clipbot/igdone")["media_id"], "M1")
        fail = next(c[2] for c in f.calls if c[1] == "/clipbot/failed")
        self.assertEqual(fail["id"], "Story vod:1:1")

    def test_duplicate_reel_gets_no_story(self):
        cap = instagram.caption_for(item(1, 1))
        f = Fake([item(1, 1)], recent=[{"id": "OLD", "caption": cap, "permalink": "p"}])
        self.run_main(f)
        self.assertFalse(any(c[0] == "http" and c[1].endswith("/42/media") and c[2] for c in f.calls))

    def test_carousel_item_also_goes_out_as_a_swipe_post(self):
        it = dict(item(1, 1), carousel=["clip2.mp4", "poster.jpg"], post_caption="the post caption")
        f = Fake([it])
        self.run_main(f)
        made = [c[2].decode() for c in f.calls if c[0] == "http" and c[1].endswith("/42/media") and c[2]]
        self.assertEqual(len(made), 5)                           # Reel, Story, 2 slides, the post
        self.assertIn("is_carousel_item=true", made[2])
        self.assertIn("media_type=VIDEO", made[2])
        self.assertIn("image_url=", made[3])
        self.assertNotIn("media_type", made[3])
        self.assertIn("media_type=CAROUSEL", made[4])
        self.assertIn("children=C1%2CC1", made[4])
        self.assertIn("caption=the+post+caption", made[4])
        pub = [c for c in f.calls if c[0] == "http" and c[1].endswith("media_publish")]
        self.assertEqual(len(pub), 3)                            # Reel, Story, post
        self.assertEqual(sum(1 for c in f.calls if c[3] == "DELETE"), 4, "every parked file is removed")

    def test_failed_post_is_reported_and_never_fails_the_reel(self):
        it = dict(item(1, 1), carousel=["clip2.mp4", "poster.jpg"])
        f = Fake([it])
        real = f.http

        def http(url, data=None, **kw):
            if url.endswith("/42/media") and data and b"CAROUSEL" in data:
                raise RuntimeError("HTTP 400 carousel rejected")
            return real(url, data, **kw)
        f.http = http
        self.run_main(f)                                         # no SystemExit
        self.assertEqual(next(c[2] for c in f.calls if c[1] == "/clipbot/igdone")["media_id"], "M1")
        self.assertEqual(next(c[2] for c in f.calls if c[1] == "/clipbot/failed")["id"], "Post vod:1:1")

    def test_plain_item_gets_no_post(self):
        f = Fake([item(1, 1)])
        self.run_main(f)
        self.assertFalse(any(c[0] == "http" and c[2] and b"CAROUSEL" in c[2] for c in f.calls))

    def test_no_login_does_nothing(self):
        f = Fake([item(1, 1)], login=False)
        self.run_main(f)
        self.assertEqual([c[1] for c in f.calls], ["/clipbot/igqueue"])


if __name__ == "__main__":
    unittest.main()

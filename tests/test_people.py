"""Per-person ClipBot (2026-10-29, Titan's lane). Offline: every network call is stubbed.

    py -m unittest discover -s tests -v

* Leon regression: publish.main() of the base commit (BASE, git show) and of this tree run the same queue
  against the same fake YouTube/worker; every call they make must be identical.
* Titan: his channel gate, his token env names only (no fallback to Leon's), his uploads playlist for the
  2-a-day slots, his copy/title rules, and refusing an older worker that would hand over Leon's queue.
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
BASE = os.environ.get("CLIPBOT_BASE", "1fc884d")
LEON_CH, GLAT_CH = "UCu6aZl_RZJAeIYJ3QgLAs8A", "UC0y2IwGQUSMhsAN1i6BJR4A"
UTC = ZoneInfo("UTC")


def load(who):
    """Fresh clipbot.common + clipbot.publish for one CLIPBOT_WHO."""
    if who is None:
        os.environ.pop("CLIPBOT_WHO", None)
    else:
        os.environ["CLIPBOT_WHO"] = who
    import clipbot.common
    import clipbot.publish
    common = importlib.reload(clipbot.common)
    publish = importlib.reload(clipbot.publish)
    return common, publish


def load_base():
    """The base commit's common/render/publish as package clipbot_base (Leon only, no CLIPBOT_WHO)."""
    d = tempfile.mkdtemp(prefix="clipbot-base-")
    pkg = os.path.join(d, "clipbot_base")
    os.makedirs(pkg)
    for f in ("__init__.py", "common.py", "render.py", "publish.py"):
        src = subprocess.run(["git", "show", f"{BASE}:clipbot/{f}"], cwd=ROOT, capture_output=True, check=True).stdout
        with open(os.path.join(pkg, f), "wb") as fh:
            fh.write(src)
    sys.path.insert(0, d)
    for m in [m for m in sys.modules if m.startswith("clipbot_base")]:
        del sys.modules[m]
    os.environ.pop("CLIPBOT_WHO", None)
    import clipbot_base.common
    import clipbot_base.publish
    sys.path.remove(d)
    return clipbot_base.common, clipbot_base.publish


def fake_world(common, publish, queue, uploads, channel, log):
    """Stub every network edge of common/publish; record each call in `log`."""
    sched = {}

    def worker(path, payload=None, method=None):
        log.append(("worker", path, json.dumps(payload, sort_keys=True) if payload else None))
        return queue if path.startswith("/clipbot/queue") else {"ok": True}

    def yt(path, **params):
        log.append(("yt", path, json.dumps(params, sort_keys=True)))
        if path == "channels":
            return {"items": [{"id": channel}]}
        if path == "playlistItems":
            return {"items": [{"contentDetails": {"videoId": v["id"]}} for v in uploads]}
        if path == "videos" and params.get("part") == "status":
            return {"items": [{"status": {"privacyStatus": "private", "publishAt": sched.get(params["id"], "2099-01-01T00:00:00Z")}}]}
        if path == "videos" and params.get("part") == "snippet,status":
            v = next(v for v in uploads if v["id"] == params["id"])
            return {"items": [{"snippet": {**v["snippet"], "categoryId": "20"}, "status": v["status"]}]}
        if path == "videos":
            return {"items": uploads}
        raise AssertionError(path)

    def http(url, data=None, headers=None, method=None, **k):
        log.append(("http", method, url.split("?")[0], json.dumps(data, sort_keys=True) if isinstance(data, dict) else None))
        if isinstance(data, dict) and data.get("id"):
            sched[data["id"]] = data["status"]["publishAt"]
        return {}

    def upload(path, title, description, at, tags=()):
        log.append(("upload", path, title, description, at.isoformat(), list(tags)))
        sched["NEWVID"] = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return "NEWVID"

    common.worker, common.yt, common.http, common.upload = worker, yt, http, upload
    common.yt_token = lambda: "tok"
    common.helix = lambda path: (log.append(("helix", path)), {"data": []})[1]
    publish.video_file = lambda it, work: f"clip-{it['id']}.mp4"
    publish.time = types.SimpleNamespace(sleep=lambda s: None)
    publish.HORIZON_H = 24 * 365                 # release everything: the result must not depend on the time of day


def short(vid, marker, status, published="2026-10-01T10:00:00Z"):
    return {"id": vid, "snippet": {"title": "t", "description": f"words\n\n{marker}" if marker else "x", "publishedAt": published},
            "status": status, "contentDetails": {"duration": "PT40S"}}


def leon_queue(with_who=None):
    now = datetime.now(UTC)
    items = [
        {"id": "vod:1:1", "kind": "vod", "batch": "1", "vod": "1", "n": 1, "title": "Car Chaos", "description": "He drives into a lake.",
         "hashtags": ["#gta"], "tags": ["gta"], "run_id": "5"},
        {"id": "vod:1:2", "kind": "vod", "batch": "1", "vod": "1", "n": 2, "title": "Panic | TwinkiesGhost", "description": "",
         "hashtags": [], "tags": [], "run_id": "5"},
        {"id": "clip:Slug", "kind": "clip", "batch": "9", "vod": "9", "n": 1, "title": "Done already", "description": "d twinkiesghost",
         "hashtags": [], "tags": [], "run_id": "4"},
    ]
    uploads = [
        short("OLDPRIV", "clipbot:vod:1:2", {"privacyStatus": "private"}),
        short("DONE", "clipbot:clip:Slug", {"privacyStatus": "private", "publishAt": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}),
    ]
    q = {"items": items, "ledger": []}
    if with_who:
        q["who"] = with_who
    return q, uploads


class LeonRegression(unittest.TestCase):
    def test_publish_main_identical_to_base(self):
        logs = []
        for mods, q_who in ((load_base(), None), (load(None), "leon")):
            common, publish = mods
            log = []
            q, uploads = leon_queue(q_who)
            fake_world(common, publish, q, uploads, LEON_CH, log)
            cwd = os.getcwd()
            os.chdir(tempfile.mkdtemp())
            try:
                publish.main()
            finally:
                os.chdir(cwd)
            logs.append(log)
        old, new = logs
        self.assertGreater(len(old), 8)
        self.assertTrue(any(c[0] == "upload" for c in old) and any(c[0] == "http" for c in old), "scenario exercised upload + reschedule")
        self.assertEqual(new, old)

    def test_leon_constants_unchanged(self):
        common, publish = load(None)
        self.assertEqual((common.WHO, common.CHANNEL_ID, common.TWITCH_UID), ("leon", LEON_CH, "1409819988"))
        self.assertEqual(publish.SUFFIX, " | TwinkiesGhost")
        self.assertEqual(publish.LIVE, "Catch the chaos live: https://twitch.tv/twinkiesghost")
        self.assertEqual(common.release_name("123-0"), "clips-123-0")
        self.assertEqual(common.who_fields(), {})


class Glatiar(unittest.TestCase):
    def setUp(self):
        self.common, self.publish = load("glatiar")

    def tearDown(self):
        load(None)

    def test_constants(self):
        c = self.common
        self.assertEqual((c.WHO, c.CHANNEL_ID, c.TWITCH_UID, c.LOGIN), ("glatiar", GLAT_CH, "1119913225", "glatiar"))
        self.assertEqual(c.who_fields(), {"who": "glatiar"})
        self.assertEqual(c.release_name("1700000000"), "clips-glatiar-1700000000")

    def test_unknown_person_refused(self):
        with self.assertRaises(SystemExit):
            load("wish")

    def test_channel_gate_both_ways(self):
        for who, own, other in (("glatiar", GLAT_CH, LEON_CH), (None, LEON_CH, GLAT_CH)):
            common, _ = load(who)
            common.yt = lambda path, **p: {"items": [{"id": other}]}
            with self.assertRaises(SystemExit) as e:
                common.channel_gate()
            self.assertIn("WRONG CHANNEL", str(e.exception))
            common.yt = lambda path, **p: {"items": [{"id": own}]}
            common.channel_gate()                                   # own channel passes

    def test_token_uses_only_his_env_names(self):
        c = self.common
        seen = []
        c.http = lambda url, data=None, **k: (seen.append(data), {"access_token": "a", "expires_in": 3600})[1]
        keep = {k: os.environ.get(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN", "GLAT_YT_CLIENT_ID", "GLAT_YT_CLIENT_SECRET", "GLAT_YT_REFRESH_TOKEN")}
        try:
            for k in keep:
                os.environ.pop(k, None)
            os.environ.update(YT_CLIENT_ID="leonid", YT_CLIENT_SECRET="leonsec", YT_REFRESH_TOKEN="leonref")
            c._yt.clear()
            with self.assertRaises(RuntimeError) as e:
                c.yt_token()                                         # Leon's are set, his are not: refuse, never borrow
            self.assertIn("GLAT_YT_CLIENT_ID", str(e.exception))
            self.assertEqual(seen, [])
            os.environ.update(GLAT_YT_CLIENT_ID="gid", GLAT_YT_CLIENT_SECRET="gsec", GLAT_YT_REFRESH_TOKEN="gref")
            c.yt_token()
            self.assertIn(b"client_id=gid", seen[0])
            self.assertNotIn(b"leon", seen[0])
        finally:
            for k, v in keep.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            c._yt.clear()

    def test_two_a_day_reads_his_channel(self):
        c = self.common
        asked = []

        def yt(path, **p):
            asked.append((path, p))
            if path == "playlistItems":
                return {"items": [{"contentDetails": {"videoId": "a"}}, {"contentDetails": {"videoId": "b"}}]}
            day = datetime.now(UTC) + timedelta(days=2)
            at = lambda h: day.replace(hour=h, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"items": [short("a", None, {"privacyStatus": "private", "publishAt": at(12)}),
                              short("b", None, {"privacyStatus": "private", "publishAt": at(17)})]}
        c.yt = yt
        ups = c.recent_uploads(50)
        self.assertEqual(asked[0][1]["playlistId"], "UU" + GLAT_CH[2:])
        # fill every day up to his two scheduled ones: the slot after them must skip that full day
        day2 = (datetime.now(UTC) + timedelta(days=2)).astimezone(c.UK).date()
        taken = []
        for _ in range(8):
            s = c.next_slot(taken, ups)
            taken.append(s.strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertNotEqual(s.astimezone(c.UK).date(), day2, "his full day got a third Short")
        per_day = {}
        for t in taken:
            d = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(c.UK).date()
            per_day[d] = per_day.get(d, 0) + 1
        self.assertTrue(all(n <= 2 for n in per_day.values()), per_day)

    def test_marathon_is_leon_only(self):
        c = self.common
        c.helix = lambda p: (_ for _ in ()).throw(AssertionError("asked Twitch"))
        self.assertFalse(c.marathon_mode())

    def test_titles_and_description_are_his(self):
        p = self.publish
        self.assertEqual(p.yt_title("Big Panic | Glatiar"), "Big Panic | Glatiar")
        self.assertEqual(p.yt_title("Big Panic"), "Big Panic | Glatiar")
        d = p.build_description({"description": "He fell off. Fuck. https://kick.com/glatiar", "hashtags": ["#aussie"]}, "clipbot:vod:1:1")
        self.assertIn("https://twitch.tv/glatiar", d)
        self.assertNotIn("twinkiesghost", d.lower())
        self.assertNotIn("kick.com", d)
        self.assertNotIn("Fuck", d)
        self.assertTrue(d.endswith("clipbot:vod:1:1"))

    def test_old_worker_queue_is_refused(self):
        """An older worker ignores ?who and returns Leon's queue with no `who`: nothing may happen."""
        log = []
        q, uploads = leon_queue(None)
        fake_world(self.common, self.publish, q, uploads, GLAT_CH, log)
        with self.assertRaises(SystemExit) as e:
            self.publish.main()
        self.assertIn("deploy the Forge first", str(e.exception))
        self.assertEqual([c[:2] for c in log], [("worker", "/clipbot/queue?who=glatiar")])

    def test_only_his_items_are_published(self):
        log = []
        q, uploads = leon_queue("glatiar")                       # worker says glatiar, but items are unlabelled (Leon's)
        q["items"].append({"id": "vod:7:1", "kind": "vod", "batch": "7", "vod": "7", "n": 1, "title": "Aussie Chaos",
                           "description": "Mate.", "hashtags": [], "tags": [], "run_id": "6", "who": "glatiar"})
        fake_world(self.common, self.publish, q, [], GLAT_CH, log)
        cwd = os.getcwd()
        os.chdir(tempfile.mkdtemp())
        try:
            self.publish.main()
        finally:
            os.chdir(cwd)
        ups = [c for c in log if c[0] == "upload"]
        self.assertEqual(len(ups), 1)
        self.assertEqual(ups[0][2], "Aussie Chaos | Glatiar")
        self.assertIn("glatiar", ups[0][5])
        self.assertNotIn("twinkiesghost", ups[0][5])
        reports = [json.loads(c[2]) for c in log if c[0] == "worker" and c[1] == "/clipbot/published"]
        self.assertEqual([(r["id"], r["who"]) for r in reports], [("vod:7:1", "glatiar")])
        self.assertTrue(any(c[0] == "yt" and c[1] == "playlistItems" and ("UU" + GLAT_CH[2:]) in c[2] for c in log))

    def test_offer_preflight_refuses_old_worker(self):
        c = self.common
        c.worker = lambda path, payload=None, method=None: {"items": [], "ledger": []}
        with self.assertRaises(SystemExit):
            c.check_worker_knows_who()
        c.worker = lambda path, payload=None, method=None: {"items": [], "ledger": [], "who": "glatiar"}
        c.check_worker_knows_who()

    def test_vod_must_be_his(self):
        import clipbot.find_vod_clips as f
        f = importlib.reload(f)
        f.common.helix = lambda p: {"data": [{"id": "1", "user_id": "1409819988", "user_login": "twinkiesghost", "duration": "1h", "created_at": "x", "title": "t"}]}
        sys.argv = ["x", "--vod", "1234567", "--out", tempfile.mkdtemp()]
        with self.assertRaises(SystemExit) as e:
            f.main()
        self.assertIn("belongs to twinkiesghost", str(e.exception))


if __name__ == "__main__":
    unittest.main()

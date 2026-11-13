"""Polished conductor failure paths, offline (no GPU, Twitch, YouTube, Discord, Modal or Vast account).

    py -m unittest discover -s tests -p test_polished_faults.py -v

Every status line is printed instead of posted (no CLIPBOT_WORKER), so the tests read the exact wording.
The cloud-only switches (kill_render, client_dies, stall, vast_leftover on a real box) are for the QA run:
see conductor.FAULT_HELP.
"""
import _ident  # noqa: F401
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from polished import conductor as C, vast as V  # noqa: E402
from clipbot import common, episodes  # noqa: E402

VID = {"data": [{"title": "t", "created_at": "2026-11-11T03:49:37Z", "duration": "10h41m57s"}]}
CLEAN_ENV = {k: "" for k in ("MODAL_TOKEN_ID", "VAST_API_KEY", "CLIPBOT_WORKER", "CLIPBOT_TOKEN", "GITHUB_TOKEN",
                             "GTA6_API_BUDGET", "POLISHED_FAULTS")}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        C.FAULTS.clear()
        self.env = mock.patch.dict(os.environ, CLEAN_ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        C.FAULTS.clear()
        shutil.rmtree(self.d, ignore_errors=True)

    def main(self, *extra, faults=""):
        os.environ["POLISHED_FAULTS"] = faults
        argv = ["conductor", "--vod", "2925545033", "--from", "0", "--to", "14400", "--state",
                os.path.join(self.d, "state"), "--out", os.path.join(self.d, "out"), *extra]
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch("clipbot.common.helix", return_value=VID), \
                mock.patch("polished.fetch.rendition", return_value=("u", 60.0, 38517.0)), contextlib.redirect_stdout(buf):
            try:
                rc, err = C.main(), None
            except Exception as e:   # noqa: BLE001
                rc, err = None, e
        mp = os.path.join(self.d, "out", "episodes.json")
        if not os.path.exists(mp):
            raise AssertionError("no manifest; main raised %r | %s" % (err, buf.getvalue()[-1500:]))
        with open(mp, encoding="utf-8") as f:
            man = json.load(f)
        return rc, err, man, buf.getvalue()


class Faults(Base):
    def test_no_1080_refuses_before_any_gpu(self):
        rc, err, man, out = self.main(faults="no_1080")
        self.assertIn("1920x1080", str(err))
        self.assertIn("1920x1080", man["error"])
        self.assertNotIn("lane", man)                                   # no lane was ever picked
        self.assertIn("Nothing half-made was uploaded", out)
        self.assertTrue(os.path.exists(os.path.join(self.d, "out", "status_died.txt")))

    def test_all_lanes_out_holds_and_pings(self):
        rc, err, man, out = self.main(faults="all_lanes_out")
        self.assertEqual(rc, 0)
        self.assertTrue(man["partial"])
        self.assertNotIn("next_from", man)                              # the worker keeps the window
        self.assertIn("no GPU lane", man["held"])
        self.assertIn("🛑", out)
        self.assertIn("phone ping skipped", out)                        # urgent; dry run does not ping

    def test_faults_ignored_when_uploading(self):
        os.environ["POLISHED_FAULTS"] = "no_1080"
        rc, err, man, out = self.main("--upload", faults="no_1080")
        self.assertEqual(C.FAULTS, set())
        self.assertIn("POLISHED_FAULTS ignored", out)
        self.assertNotIn("error", man)                                  # it went on to (no) lanes instead

    def test_api_dry_budget_is_zero(self):
        C.FAULTS.add("api_dry")
        os.environ["GTA6_API_BUDGET"] = "4.90"
        r = C.Run(SimpleNamespace(state=os.path.join(self.d, "s"), out=os.path.join(self.d, "o"), lane="auto"))
        self.assertEqual(r.api_budget(), 0.0)
        self.assertIn("0.0000", " ".join(r.api_left_args()))


class Lanes(Base):
    def run_(self):
        r = C.Run(SimpleNamespace(state=os.path.join(self.d, "s"), out=os.path.join(self.d, "o"), lane="auto"))
        r.st = C.Status("1", 0, 14400, dry=True)
        return r

    def test_near_cap_warns_then_stays_on_modal(self):
        os.environ["MODAL_TOKEN_ID"] = "x"
        C.FAULTS.add("modal_near_cap")
        buf = io.StringIO()
        with mock.patch.object(C.ledger, "modal_spent", return_value=1.0), contextlib.redirect_stdout(buf):
            lane = self.run_().pick_lane(4.0)
        self.assertEqual(lane, "modal")
        self.assertIn("NEXT one moves to Vast", buf.getvalue())

    def test_at_cap_moves_to_vast_and_says_why(self):
        os.environ.update(MODAL_TOKEN_ID="x", VAST_API_KEY="k")
        C.FAULTS.add("modal_at_cap")
        buf = io.StringIO()
        with mock.patch.object(C.ledger, "modal_spent", return_value=1.0), \
                mock.patch.object(V.Vast, "balance", return_value=9.5), contextlib.redirect_stdout(buf):
            lane = self.run_().pick_lane(4.0)
        self.assertEqual(lane, "vast")
        self.assertIn("Moved to Vast.ai", buf.getvalue())
        self.assertIn("hand-over mark", buf.getvalue())

    def test_vast_zero_holds(self):
        os.environ.update(MODAL_TOKEN_ID="x", VAST_API_KEY="k")
        C.FAULTS.update({"modal_at_cap", "vast_zero"})
        r = self.run_()
        with mock.patch.object(C.ledger, "modal_spent", return_value=1.0), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(C.Hold, "Vast balance \\$0.00"):
                r.pick_lane(4.0)
        self.assertTrue(r.all_out)


class FakeGpu:
    """Renders: episode 2 fails a data gate. Records whether it was told to stop."""

    def __init__(self, d, bad=2):
        self.d, self.bad, self.saw_stop = d, bad, None

    def render_many(self, vod, jobs, sink, stop):
        for n, (k, p, t) in enumerate(jobs, 1):
            if stop.is_set():
                self.saw_stop = n
                break
            f = os.path.join(self.d, k + ".mp4")
            open(f, "wb").write(b"x" * 10)
            sink.put((k, f, {"gates": {"pass": n != self.bad, "verify_rc": 0 if n != self.bad else 1, "av_sync": {}},
                             "size": 10, "gpu": "fake", "container_s": 1}))
            if n == self.bad:
                stop.wait(5)                                            # the conductor must say stop
        sink.put(None)


def plans(n):
    out = []
    for i in range(n):
        s = i * 1200.0
        out.append({"keep": [[s, s + 1190]], "cold": [s + 10, s + 20], "title": "T%d" % i, "bounds": [s, s + 1200],
                    "description": "Part {PART}. x", "youtube_title": "x", "chapters": [], "stats": {"problems": []}})
    return out


class Process(Base):
    def setUp(self):
        super().setUp()
        self.run = C.Run(SimpleNamespace(state=os.path.join(self.d, "s"), out=os.path.join(self.d, "o"), lane="auto",
                                         vod="1", upload=False, keep=0, series="GTA 6", answers=""))
        self.run.st = C.Status("1", 0, 14400, dry=True)
        self.run.brand = "OwnerBrand"
        self.man = {"episodes": [], "holes": []}

    def test_failed_gate_holds_and_stops_later_renders(self):
        gpu = FakeGpu(self.d)
        with mock.patch.object(C, "review", return_value={"verdict": "pass"}), contextlib.redirect_stdout(io.StringIO()):
            hold = C.process(self.run, gpu, {}, plans(4), 0.0, self.man, common, episodes)
        self.assertIn("episode 2", hold)
        self.assertIn("data gate failed", hold)
        self.assertEqual([e["i"] for e in self.man["episodes"]], [1])  # nothing after the held one
        self.assertEqual(gpu.saw_stop, 3)                              # episode 3 was never rendered

    def test_review_fail_holds(self):
        gpu = FakeGpu(self.d, bad=99)
        rv = iter([{"verdict": "pass"}, {"verdict": "fail", "issues": [{"severity": "blocker", "what": "menu 30 s"}]}])
        with mock.patch.object(C, "review", side_effect=lambda *a: next(rv)), contextlib.redirect_stdout(io.StringIO()):
            hold = C.process(self.run, gpu, {}, plans(3), 0.0, self.man, common, episodes)
        self.assertIn("review fail: menu 30 s", hold)
        self.assertEqual(len(self.man["episodes"]), 1)

    def test_episodes_are_contiguous_in_the_manifest(self):
        gpu = FakeGpu(self.d, bad=99)
        with mock.patch.object(C, "review", return_value={"verdict": "pass"}), contextlib.redirect_stdout(io.StringIO()):
            hold = C.process(self.run, gpu, {}, plans(3), 100.0, self.man, common, episodes)
        self.assertIsNone(hold)
        eps = self.man["episodes"]
        self.assertEqual([e["start"] for e in eps], [100.0, 1300.0, 2500.0])
        for x, y in zip(eps, eps[1:]):
            self.assertEqual(x["end"], y["start"])
        self.assertTrue(all(e["marker"].startswith("clipbot:ep:1:") for e in eps))

    def test_fail_window(self):
        C.FAULTS.add("fail_window")
        with self.assertRaisesRegex(RuntimeError, "fail_window"):
            C.process(self.run, FakeGpu(self.d), {}, plans(2), 0.0, self.man, common, episodes)

    def test_duplicate_guard_unreadable_stops_before_any_upload(self):
        C.FAULTS.add("dup_unreadable")
        real = common.recent_uploads
        try:
            with self.assertRaisesRegex(RuntimeError, "could not read the channel"):
                C.process(self.run, FakeGpu(self.d), {}, plans(2), 0.0, self.man, common, episodes)
        finally:
            common.recent_uploads = real
        self.assertEqual(self.man["episodes"], [])


class FakeVast:
    def __init__(self, bal=5.0, fail=False):
        self.bal, self.fail, self.destroyed, self.made = bal, fail, [], 0

    def balance(self):
        return self.bal

    def sweep(self, keep=None):
        return []

    def offers(self):
        return [{"id": i, "dph_total": 0.2, "gpu_name": "RTX 3090"} for i in range(3)]

    def create(self, oid, label):
        self.made += 1
        if self.fail:
            raise V.VastError("rent refused")
        return 100 + oid

    def attach_key(self, iid, pub):
        pass

    def destroy(self, iid):
        self.destroyed.append(iid)
        return True


class VastJob(unittest.TestCase):
    def test_start_failure_tries_offers_then_gives_up_with_nothing_running(self):
        fv = FakeVast(fail=True)
        j = V.Job(fv, 1.0, 5, 3)
        with mock.patch.object(V, "keypair", return_value=("k", "pub")), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(V.VastError, "could not get a working instance"):
                j.__enter__()
        self.assertEqual(fv.made, 3)
        self.assertIsNone(j.iid)

    def test_instance_destroyed_even_when_ssh_never_comes_up(self):
        fv = FakeVast()
        j = V.Job(fv, 1.0, 5, 3)
        with mock.patch.object(V, "keypair", return_value=("k", "pub")), \
                mock.patch.object(V.Job, "_wait", side_effect=V.VastError("not reachable")), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(V.VastError):
                j.__enter__()
        self.assertEqual(sorted(fv.destroyed), [100, 101, 102])        # every rented box destroyed

    def test_balance_floor_stops_before_renting(self):
        fv = FakeVast(bal=0.2)
        j = V.Job(fv, 1.0, 5, 3)
        with mock.patch.object(V, "keypair", return_value=("k", "pub")), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(V.VastError, "HOLD"):
                j.__enter__()
        self.assertEqual(fv.made, 0)

    def test_leftover_fault_keeps_it_and_sweep_removes_it(self):
        fv = FakeVast()
        j = V.Job(fv, 1.0, 5, 3)
        j.iid, j.keep_instance = 7, True
        with contextlib.redirect_stdout(io.StringIO()):
            j.__exit__(None, None, None)
        self.assertEqual(fv.destroyed, [])
        real = V.Vast.__new__(V.Vast)
        real.mine = lambda: [{"id": 7, "label": "gta6-polished-123"}, {"id": 8, "label": "someone-else"}]
        real.destroy = lambda iid: fv.destroyed.append(iid) or True
        self.assertEqual(V.Vast.sweep(real), [7])                      # ours only


if __name__ == "__main__":
    unittest.main()


class VastSetupFailure(unittest.TestCase):
    def test_box_is_destroyed_when_setup_fails_after_renting(self):
        fv = FakeVast()
        run = C.Run(SimpleNamespace(state=tempfile.mkdtemp(), out=tempfile.mkdtemp(), lane="vast"))
        lane = C.VastLane.__new__(C.VastLane)
        lane.run, lane.vast, lane.v = run, V, fv
        lane.job = V.Job(fv, 1.0, 5, 3)
        lane.job.iid = 100
        lane.job.__enter__ = lambda: object()
        with mock.patch.object(C.VastLane, "_setup", side_effect=V.VastError("setup failed")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(V.VastError):
                lane.__enter__()
        self.assertEqual(fv.destroyed, [100])


class RenderRetry(unittest.TestCase):
    """Fault QA 13 Nov: one transient render failure held a whole window for 6 h. Retry once first."""

    def test_modal_render_retried_once_then_succeeds(self):
        lane = C.ModalLane.__new__(C.ModalLane)
        lane.run = SimpleNamespace(st=C.Status("1", 0, 100, dry=True), a=SimpleNamespace(series="GTA 6"))
        calls = []

        def attempt(*a):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("container lost")
            return {"ok": True}
        lane._render_attempt = attempt
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lane._render_one("1", {}, "t", {}, "k"), {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_modal_render_gives_up_after_two(self):
        lane = C.ModalLane.__new__(C.ModalLane)
        lane.run = SimpleNamespace(st=C.Status("1", 0, 100, dry=True), a=SimpleNamespace(series="GTA 6"))
        lane._render_attempt = lambda *a: (_ for _ in ()).throw(RuntimeError("still lost"))
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "still lost"):
                lane._render_one("1", {}, "t", {}, "k")

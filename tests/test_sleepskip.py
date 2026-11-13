"""Sleep skip (polished/sleepskip.py), offline: no GPU, no network, no models (a fake picture model + transcriber).

    py -m unittest tests.test_sleepskip -v

* The decision: only a long Resting-screen stretch with no word is skipped, minus a margin at both ends; any word,
  any picture that is not Resting, any frame or audio that cannot be read keeps the stretch (fail open).
* The 💤 button: widens the picture rule only while the picture stays the same as at the press, ignores a press made
  during gameplay, and ends at the first different picture or word.
* EQUIVALENCE: a window with the sleep skipped plans EXACTLY the same episodes as the same window fully analysed.
* The conductor: off unless GTA6_SLEEP_SKIP=1, any failure = nothing skipped, a retry reuses the decision.
Real-footage calibration (13 Nov, laptop CPU) is in the module doc and the PR: 2 whole real VODs (1,193 frames) and
1,332 frames of official GTA 6 footage never reach the Resting threshold; the camp's Resting sets always do.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from polished import sleepskip as ss, stages  # noqa: E402
from tests.test_polished_plan import chain, synth  # noqa: E402

REST = np.zeros(512, np.float32)
REST[0] = 1.0
UNSEEN = np.zeros(512, np.float32)       # a Resting set the library has never seen: 0.80 to the library
UNSEEN[0], UNSEEN[1] = 0.80, 0.60


def game_vec(t):
    v = np.random.default_rng(int(t)).normal(0, 1, 512).astype(np.float32)
    v[0] = 0.0
    return v / np.linalg.norm(v)


class Src:
    """timeline: [(a, b, kind)] kind in rest / unseen / game / broken; words: VOD seconds he speaks at."""

    def __init__(self, total, timeline, words=(), audio_fails=False):
        self.total, self.timeline, self.words, self.audio_fails = total, timeline, list(words), audio_fails
        self.recording = False
        self.audio_calls = []

    def kind(self, t):
        return next((k for a, b, k in self.timeline if a <= t < b), "game")

    def frame(self, t, work):
        k = self.kind(t)
        if k == "broken":
            raise RuntimeError("segment head did not decode")
        return t, np.full((224, 224, 3), {"rest": 1, "unseen": 2, "game": 3}[k], np.uint8) + np.uint8(int(t) % 200 * (k == "game"))

    def audio(self, a, b, out):
        self.audio_calls.append((a, b))
        if self.audio_fails:
            raise RuntimeError("no audio rendition")
        open(out, "w").write(json.dumps([w for w in self.words if a <= w <= b]))
        return a


class FakeClip:
    def embed(self, frames):
        e, s = [], []
        for f in frames:
            k = int(f[0, 0, 0])
            if k == 1:
                e.append(REST); s.append(0.6)
            elif k == 2:
                e.append(UNSEEN / np.linalg.norm(UNSEEN)); s.append(0.55)
            else:
                e.append(game_vec(k)); s.append(0.05)
        return np.array(e, np.float32), np.array(s, np.float32)


def fake_words(path, origin):
    with open(path) as f:
        return json.load(f)


def decide(src, lo, hi, presses=()):
    return ss.decide(src, lo, hi, presses, refs=REST[None, :], clip=FakeClip(), work=tempfile.mkdtemp(),
                     transcribe=fake_words)


class Decide(unittest.TestCase):
    def test_sleep_with_one_word_in_it_is_skipped_around_the_word_with_margins(self):
        src = Src(4 * 3600, [(1800, 9000, "rest")], words=[100, 200, 5400, 9500])
        r = decide(src, 0, 4 * 3600)
        m, p = ss.MARGIN, ss.SPEECH_PAD
        self.assertEqual(r["skips"], [[1800 + m, 5400 - p - m], [5400 + p + m, 8940 - m]])   # 8940 = last Resting sample
        for a, b in r["skips"]:
            self.assertGreaterEqual(b - a, ss.MIN_SKIP)

    def test_gameplay_only_skips_nothing_and_never_asks_for_audio(self):
        src = Src(4 * 3600, [])
        r = decide(src, 0, 4 * 3600)
        self.assertEqual(r["skips"], [])
        self.assertEqual(src.audio_calls, [])

    def test_short_rest_is_not_worth_it(self):
        src = Src(4 * 3600, [(3600, 3600 + 25 * 60, "rest")])            # 25 min: under MIN_SKIP + 2 margins
        self.assertEqual(decide(src, 0, 4 * 3600)["skips"], [])

    def test_no_audio_means_no_skip(self):
        src = Src(4 * 3600, [(1800, 9000, "rest")], audio_fails=True)
        r = decide(src, 0, 4 * 3600)
        self.assertEqual(r["skips"], [])
        self.assertTrue(any("audio check failed" in n for n in r["notes"]))

    def test_unreadable_picture_splits_the_run(self):
        src = Src(4 * 3600, [(5400, 5460, "broken"), (1800, 9000, "rest")])
        r = decide(src, 0, 4 * 3600)
        for a, b in r["skips"]:
            self.assertFalse(a <= 5400 <= b, r["skips"])                   # the unknown minute is never inside a skip

    def test_whole_window_asleep_needs_no_gpu(self):
        src = Src(8 * 3600, [(0, 8 * 3600, "rest")])
        r = decide(src, 3600, 3600 + 4 * 3600)
        self.assertEqual(r["skips"], [[3600, 3600 + 4 * 3600]])           # edge to edge: no margin at window edges
        plan = ss.chunk_plan(3600, 3600 + 4 * 3600, r["skips"], 1800)
        self.assertEqual([x[2] for x in plan], [True])

    def test_unseen_resting_set_is_not_skipped_without_a_press(self):
        src = Src(4 * 3600, [(1800, 9000, "unseen")])
        self.assertEqual(decide(src, 0, 4 * 3600)["skips"], [])

    def test_press_on_an_unseen_resting_set_skips_it_and_ends_when_the_picture_changes(self):
        src = Src(4 * 3600, [(1800, 9000, "unseen")])
        r = decide(src, 0, 4 * 3600, [{"t": 1830, "mode": "asleep", "by": "mod-a"}])
        self.assertEqual(len(r["skips"]), 1)
        a, b = r["skips"][0]
        self.assertGreaterEqual(a, 1860 + ss.MARGIN)
        self.assertLessEqual(b, 9000 - ss.MARGIN)
        self.assertEqual(r["presses"][0]["end"], 9000)                     # first gameplay sample
        self.assertIn("picture changed", r["presses"][0]["why"])

    def test_press_during_gameplay_is_ignored(self):
        src = Src(4 * 3600, [])
        r = decide(src, 0, 4 * 3600, [{"t": 600, "mode": "asleep", "by": "mod-b"}])
        self.assertEqual(r["skips"], [])
        self.assertIn("not a Resting/stream screen", r["presses"][0]["why"])

    def test_forgotten_press_never_reaches_later_gameplay(self):
        # pressed on an unseen Resting set, he wakes and plays SILENTLY for an hour, nobody presses awake
        src = Src(4 * 3600, [(0, 3600, "unseen")])
        r = decide(src, 0, 4 * 3600, [{"t": 60, "mode": "asleep", "by": "mod-c"}])
        for a, b in r["skips"]:
            self.assertLessEqual(b, 3600)

    def test_awake_press_ends_a_manual_stretch(self):
        src = Src(4 * 3600, [(0, 4 * 3600, "unseen")])
        r = decide(src, 0, 4 * 3600, [{"t": 60, "mode": "asleep", "by": "mod-a"}, {"t": 7200, "mode": "awake", "by": "mod-b"}])
        for a, b in r["skips"]:
            self.assertLessEqual(b, 7200)
        self.assertEqual(r["presses"][0]["end"], 7200)

    def test_a_word_ends_a_manual_stretch_for_good(self):
        # unseen Resting set all window, 💤 at 1 min, he says something at 1 h: nothing after it is skipped
        src = Src(4 * 3600, [(0, 4 * 3600, "unseen")], words=[3600])
        r = decide(src, 0, 4 * 3600, [{"t": 60, "mode": "asleep", "by": "mod-a"}])
        self.assertTrue(r["skips"])
        for a, b in r["skips"]:
            self.assertLessEqual(b, 3600 - ss.SPEECH_PAD - ss.MARGIN)
        self.assertEqual(r["presses"][0]["why"], "he talked")

    def test_a_word_inside_an_automatic_skip_only_splits_it(self):
        # the library knows this screen: after a word the skip resumes (the planner cuts that stretch anyway)
        src = Src(4 * 3600, [(0, 4 * 3600, "rest")], words=[3600])
        r = decide(src, 0, 4 * 3600)
        self.assertEqual(len(r["skips"]), 2)


class Equivalence(unittest.TestCase):
    """The rule everything rests on: skipping the sleep changes no episode."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    @staticmethod
    def chunked(an, plan, vod_total):
        """Cut a synth analysis into analyze_chunk-format pieces; skipped pieces come from sleepskip.skipped_chunk."""
        fetch = {"segments": [0, 0], "recording": False, "vod_total": vod_total, "fps": 60.0}
        out = []
        for a, b, sk in plan:
            if sk:
                out.append(ss.skipped_chunk("1", a, b, fetch))
                continue
            i0, i1 = int(round(a / 0.1)), int(round(b / 0.1))
            am = {k: an["audio_map"][k][i0:i1] for k in ("rms", "flux", "low")}
            segs = [sg for sg in an["transcript"]["segments"] if a <= sg["words"][0]["s"] < b]
            out.append({"vod": "1", "lo": a, "hi": b, "origin": a, "fetch": fetch, "hop": 0.1, "audio_map": am,
                        "visual": [v for v in an["visual"] if a <= v["t"] < b],
                        "menu": [{"t": float(t), "menu": 0.05, "scene": 0.05} for t in range(int(a), int(b))],
                        "static_runs": [], "segments": segs, "timings": {}, "gpu": "test"})
        return stages.merge_chunks(out)

    def test_skipped_sleep_plans_the_same_episodes(self):
        dur, sleep = 4 * 3600.0, (5000.0, 5000.0 + 80 * 60)
        an = synth(dur, away=[sleep])
        full = self.chunked(an, [(a, b, False) for a, b in [(t, min(dur, t + 1800)) for t in np.arange(0, dur, 1800)]], dur)
        skips = [[sleep[0] + ss.MARGIN, sleep[1] - ss.MARGIN]]
        part = self.chunked(an, ss.chunk_plan(0.0, dur, skips, 1800), dur)
        self.assertEqual(part["duration"], full["duration"])
        for k in ("full", "part"):
            os.makedirs(os.path.join(self.d, k))
        p_full, t_full = chain(full, os.path.join(self.d, "full"), final=True)
        p_part, t_part = chain(part, os.path.join(self.d, "part"), final=True)
        self.assertGreaterEqual(len(p_full), 4)
        self.assertEqual([(p["bounds"], p["keep"]) for p in p_part], [(p["bounds"], p["keep"]) for p in p_full])
        self.assertEqual(t_part, t_full)
        for p in p_part:                                                   # nothing kept from the skipped stretch
            for s, e in p["keep"]:
                self.assertTrue(e <= skips[0][0] or s >= skips[0][1], (s, e))


class ChunkPlan(unittest.TestCase):
    def test_covers_the_window_exactly(self):
        plan = ss.chunk_plan(100.0, 100.0 + 4 * 3600, [[2000.0, 7000.0], [9000.0, 12000.0]], 1800)
        self.assertEqual(plan[0][0], 100.0)
        self.assertEqual(plan[-1][1], 100.0 + 4 * 3600)
        for x, y in zip(plan, plan[1:]):
            self.assertEqual(x[1], y[0])
        self.assertEqual([x[:2] for x in plan if x[2]], [(2000.0, 7000.0), (9000.0, 12000.0)])
        self.assertTrue(all(b - a <= 1800 for a, b, sk in plan if not sk))


class Conductor(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        from polished import conductor
        self.c = conductor
        a = mock.Mock(state=os.path.join(self.d, "state"), out=os.path.join(self.d, "out"))
        self.run = conductor.Run(a)
        self.run.st = mock.Mock()
        self.run.st.sleep_log.return_value = []

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {"GTA6_SLEEP_SKIP": ""}), mock.patch.object(ss, "TwitchSource") as ts:
            r = self.run.sleep_skip("1", 0.0, 14400.0, "2026-11-19T00:00:00Z")
        self.assertEqual(r["skips"], [])
        ts.assert_not_called()

    def test_failure_scans_everything_and_says_so(self):
        with mock.patch.dict(os.environ, {"GTA6_SLEEP_SKIP": "1"}), \
                mock.patch.object(ss, "TwitchSource", side_effect=RuntimeError("twitch down")):
            r = self.run.sleep_skip("1", 0.0, 14400.0, "2026-11-19T00:00:00Z")
        self.assertEqual(r["skips"], [])
        self.assertIn("twitch down", r["error"])
        self.assertIn("nothing skipped", self.run.st.event.call_args[0][1])

    def test_retry_reuses_the_decision_and_press_times_map_onto_the_vod(self):
        self.run.st.sleep_log.return_value = [{"t": 1763510400.0 + 3600, "mode": "asleep", "by": "mod-a"}]
        seen = {}

        def fake_decide(src, lo, hi, presses):
            seen["presses"] = presses
            return {"window": [lo, hi], "skips": [[1800.0, 9000.0]], "notes": [], "presses": []}
        src = mock.Mock(recording=True, total=20000.0)
        with mock.patch.dict(os.environ, {"GTA6_SLEEP_SKIP": "1"}), mock.patch.object(ss, "TwitchSource", return_value=src), \
                mock.patch.object(ss, "decide", side_effect=fake_decide) as d:
            r1 = self.run.sleep_skip("1", 0.0, 14400.0, "2025-11-19T00:00:00Z")
            r2 = self.run.sleep_skip("1", 0.0, 14400.0, "2025-11-19T00:00:00Z")
        self.assertEqual(d.call_count, 1)
        self.assertEqual(r1["skips"], r2["skips"])
        self.assertEqual(seen["presses"][0]["t"], 3600.0)                  # unix press -> VOD second
        self.assertEqual(r1["fetch"]["vod_total"], 20000.0)

    def test_analyse_never_sends_skipped_stretches_to_the_gpu(self):
        sleep = {"skips": [[1800.0, 9000.0]], "fetch": {"segments": [0, 0], "recording": False, "vod_total": 14400.0, "fps": 60.0}}
        asked = []

        class Gpu:
            def analyze_many(self, vod, todo):
                for c in todo:
                    asked.append(c)
                    n = int(round((c[1] - c[0]) / 0.1))
                    yield c, {"vod": "1", "lo": c[0], "hi": c[1], "origin": c[0], "fetch": sleep["fetch"], "hop": 0.1,
                              "audio_map": {"rms": [-30.0] * n, "flux": [0.1] * n, "low": [-40.0] * n},
                              "visual": [], "menu": [], "static_runs": [], "segments": [], "timings": {"total_s": 1},
                              "gpu": "fake"}
        an = self.run.analyse("1", 0.0, 14400.0, Gpu(), sleep)
        self.assertEqual(an["duration"], 14400.0)
        for a, b in asked:
            self.assertTrue(b <= 1800.0 or a >= 9000.0, (a, b))
        self.assertEqual(sum(b - a for a, b in asked), 14400.0 - 7200.0)
        asked.clear()                                                      # a retry: everything from the state dir
        self.run.analyse("1", 0.0, 14400.0, Gpu(), sleep)
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()

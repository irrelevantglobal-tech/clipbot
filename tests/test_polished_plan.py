"""Polished marathon planner (polished/plan_cut.py), offline: no GPU, no API, no network.

    py -m unittest tests.test_polished_plan -v

* Episodes of one window are chained: each starts exactly where the last ended, so no stream time
  falls between two episodes (T2 lesson: whatever an episode drops at its end must roll into the next).
* A 24/7 stream: a sleep on a resting screen that plays music and moves is cut (no word for 20+ min),
  the episode before it closes there, and the next one starts after it.
* A window that is not final hands its tail to the next window (exit 2 + tail.json).
* No budget = HOLD (exit 3), never a plan with code-default copy.
* Merging analysed chunks refuses a hole between them.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from polished import stages  # noqa: E402

HOP = 0.1


def synth(dur, away=()):
    """Talk everywhere except the `away` spans: a word every 1.2 s, a 4 s breath every 5 min.
    Away = resting screen: music (not quiet) and a moving picture, no words."""
    words, t, last_breath = [], 2.0, 0.0
    while t < dur - 1:
        if any(a <= t < b for a, b in away):
            t = next(b for a, b in away if a <= t < b) + 2.0
            continue
        words.append({"w": "word.", "s": round(t, 2), "e": round(t + 0.4, 2), "p": 0.9})
        t += 1.2
        if t - last_breath >= 300:
            t += 4.0
            last_breath = t
    ns = int(round(dur / HOP))
    rng = np.random.default_rng(1)
    rms = (-30 + rng.normal(0, 2, ns)).round(2).tolist()          # talk or music: never "absolutely quiet"
    vis = [{"t": round(i / 2, 2), "diff": 9.0 + (i % 7), "luma": 80.0, "p95": 200.0} for i in range(int(dur * 2))]
    segs = [{"s": w["s"], "e": w["e"], "text": w["w"], "words": [w]} for w in words]
    return {"duration": dur, "audio_map": {"hop": HOP, "rms": rms, "flux": rng.random(ns).round(3).tolist(),
                                            "low": (-40 + rng.normal(0, 2, ns)).round(2).tolist()},
            "visual": vis, "menu": [], "static": {"runs": []}, "transcript": {"segments": segs}}


def plan(an_path, work, *args):
    os.makedirs(work, exist_ok=True)
    r = subprocess.run([sys.executable, "-m", "polished.plan_cut", an_path, work, "--brand", "OwnerBrand", *args],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return r


def chain(an, d, final, vod_start=False, limit=30):
    ap = os.path.join(d, "an.json")
    json.dump(an, open(ap, "w"))
    plans, start, tail = [], None if vod_start else 0.0, None
    for k in range(limit):
        w = os.path.join(d, "ep%02d" % k)
        args = ["--no-llm"] + (["--final"] if final else []) + (["--vod-start"] if start is None else ["--start", str(start)])
        r = plan(ap, w, *args)
        if r.returncode == 2:
            tail = json.load(open(os.path.join(w, "tail.json")))
            break
        if r.returncode:
            raise AssertionError("plan_cut rc %d: %s %s" % (r.returncode, r.stdout[-800:], r.stderr[-800:]))
        p = json.load(open(os.path.join(w, "plan.json"), encoding="utf-8"))
        plans.append(p)
        start = p["next_start"]
        if final and p.get("closes_session") and start >= an["duration"] - 1:
            break
    return plans, tail


def content(p):
    return sum(e - s for s, e in p["keep"])


class Chain(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_window_is_chained_without_loss(self):
        an = synth(3 * 3600)
        plans, tail = chain(an, self.d, final=False)
        self.assertGreaterEqual(len(plans), 6)
        self.assertEqual(plans[0]["bounds"][0], 0.0)                     # starts exactly at the window start
        for x, y in zip(plans, plans[1:]):
            self.assertEqual(y["bounds"][0], x["next_start"])            # no gap between two episodes
            self.assertEqual(x["next_start"], x["bounds"][1])
        for p in plans:
            self.assertTrue(900 <= content(p) <= 1500, content(p))
            self.assertIn("Part {PART}.", p["description"])
            self.assertTrue(p["youtube_title"].endswith(" | OwnerBrand"))
        self.assertIsNotNone(tail)                                       # not final: the tail moves on
        self.assertEqual(tail["next_start"], plans[-1]["next_start"])
        self.assertLess(tail["left_content_s"], 900 + 1)

    def test_sleep_on_a_moving_resting_screen_is_cut_and_closes_the_session(self):
        sleep = (5000.0, 5000.0 + 45 * 60)
        an = synth(3 * 3600, away=[sleep])
        plans, tail = chain(an, self.d, final=False)
        before = [p for p in plans if p["bounds"][1] <= sleep[0] + 5]
        after = [p for p in plans if p["bounds"][0] >= sleep[1] - 5]
        self.assertEqual(len(before) + len(after), len(plans), [p["bounds"] for p in plans])   # nobody spans the sleep
        self.assertTrue(before[-1]["closes_session"])
        self.assertLessEqual(after[0]["bounds"][0], sleep[1])            # the next one starts at the end of the cut
        self.assertGreater(after[0]["bounds"][0], sleep[1] - 5)
        kept_in_sleep = sum(max(0, min(e, sleep[1] - 3) - max(s, sleep[0] + 3)) for p in plans for s, e in p["keep"])
        self.assertEqual(kept_in_sleep, 0)
        for x, y in zip(plans, plans[1:]):
            self.assertEqual(y["bounds"][0], x["next_start"])

    def test_final_window_emits_the_tail(self):
        an = synth(2 * 3600 + 700)
        plans, tail = chain(an, self.d, final=True)
        self.assertIsNone(tail)
        self.assertGreater(plans[-1]["bounds"][1], an["duration"] - 20)  # the last words are in the last episode
        for x, y in zip(plans, plans[1:]):
            self.assertEqual(y["bounds"][0], x["next_start"])

    def test_vod_start_begins_just_before_the_first_word(self):
        an = synth(2 * 3600, away=[(0, 400)])
        plans, _ = chain(an, self.d, final=False, vod_start=True, limit=1)
        self.assertAlmostEqual(plans[0]["bounds"][0], 402.0 - 3.0, delta=0.6)

    def test_no_budget_holds_instead_of_shipping_defaults(self):
        an = synth(3600)
        ap = os.path.join(self.d, "an.json")
        json.dump(an, open(ap, "w"))
        env_key = os.environ.pop("GTA6_ANTHROPIC_KEY", None)
        try:
            r = plan(ap, os.path.join(self.d, "h"), "--start", "0", "--budget-limit", "0.01", "--budget-spent", "0")
        finally:
            if env_key:
                os.environ["GTA6_ANTHROPIC_KEY"] = env_key
        self.assertEqual(r.returncode, 3, r.stdout[-500:])
        self.assertTrue(os.path.exists(os.path.join(self.d, "h", "plan_prompt.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "h", "plan.json")))
        prompt = open(os.path.join(self.d, "h", "plan_prompt.txt"), encoding="utf-8").read()
        self.assertIn("OwnerBrand", prompt)
        self.assertNotIn("{BRAND}", prompt)


class Merge(unittest.TestCase):
    def chunk(self, lo, hi):
        n = int(round((hi - lo) / HOP))
        return {"vod": "1", "lo": lo, "hi": hi, "fetch": {"recording": False, "vod_total": 9999.0},
                "audio_map": {"rms": [-30.0] * n, "flux": [0.1] * n, "low": [-40.0] * n},
                "visual": [{"t": lo + i / 2, "diff": 5.0, "luma": 50.0, "p95": 90.0} for i in range(int((hi - lo) * 2))],
                "menu": [{"t": lo + i, "menu": 0.1} for i in range(int(hi - lo))],
                "static_runs": [[hi - 30, hi]] if lo == 1000 else [[lo, lo + 40]],
                "segments": [{"s": lo + 5, "e": lo + 6, "text": "hi", "words": [{"w": "hi", "s": lo + 5, "e": lo + 6}]}],
                "timings": {}, "gpu": "test"}

    def test_window_seconds_and_static_run_across_the_edge(self):
        m = stages.merge_chunks([self.chunk(2800, 4600), self.chunk(1000, 2800)])
        self.assertEqual(m["base"], 1000)
        self.assertEqual(m["duration"], 3600)
        self.assertEqual(len(m["audio_map"]["rms"]), 36000)
        self.assertEqual(m["transcript"]["segments"][1]["words"][0]["s"], 1805)
        self.assertIn([1770, 1840], [r[:2] for r in m["static"]["runs"]])   # one run, not two

    def test_hole_between_chunks_refused(self):
        with self.assertRaisesRegex(RuntimeError, "do not meet"):
            stages.merge_chunks([self.chunk(1000, 2800), self.chunk(2900, 4600)])


if __name__ == "__main__":
    unittest.main()


class QualityFixes(unittest.TestCase):
    """12 Nov quality-session fixes, ported: static-gap cut, load-fade black cut at the start (start stays
    chained), cold-open chapter label repeat, and the stings' retired-name guard."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_wordless_stretch_inside_a_talked_over_still_screen_is_cut(self):
        an = synth(3600)
        # a 30 s still screen at 600-630 s where he talks only at the end: words removed 600-624
        segs = [s for s in an["transcript"]["segments"] if not (600 <= s["s"] < 624)]
        an["transcript"]["segments"] = segs
        an["static"]["runs"] = [[600.0, 630.0]]
        ap = os.path.join(self.d, "an.json")
        json.dump(an, open(ap, "w"))
        r = plan(ap, os.path.join(self.d, "w"), "--no-llm", "--start", "0")
        self.assertEqual(r.returncode, 0, r.stdout[-600:])
        p = json.load(open(os.path.join(self.d, "w", "plan.json"), encoding="utf-8"))
        kinds = {c[2] for c in p["cuts"] if c[0] < 640 and c[1] > 590}
        self.assertIn("static-gap", kinds)

    def test_load_fade_at_the_start_is_cut_but_the_start_stays_chained(self):
        an = synth(3600)
        an["transcript"]["segments"] = [s for s in an["transcript"]["segments"] if not 1197 <= s["s"] < 1206]
        for v in an["visual"]:
            if 1200.5 <= v["t"] < 1205:
                v["luma"] = 2.0
        ap = os.path.join(self.d, "an.json")
        json.dump(an, open(ap, "w"))
        r = plan(ap, os.path.join(self.d, "w"), "--no-llm", "--start", "1200")
        self.assertEqual(r.returncode, 0, r.stdout[-600:])
        p = json.load(open(os.path.join(self.d, "w", "plan.json"), encoding="utf-8"))
        self.assertEqual(p["bounds"][0], 1200.0)                       # chained start kept
        self.assertGreaterEqual(p["keep"][0][0], 1204.9)               # the black is not in the episode
        self.assertTrue(any("load fade" in x for x in p["stats"]["problems"]))

    def test_sting_refuses_a_retired_name_before_any_frame(self):
        import _ident
        from polished.brand import stings
        out = os.path.join(self.d, "b")
        with self.assertRaises(SystemExit):
            stings.setup("GTA 6", _ident.OLD, "ownerlogin", out, retired=[_ident.OLD])
        with self.assertRaises(SystemExit):
            stings.setup("GTA 6", "OwnerBrand", "ownerlogin", out, retired=[])   # identity fallback has OLD
            stings.ws.make_intro(None, "the " + " ".join(_ident.OLD) + " heist", frame_png=os.path.join(out, "f.png"))
        self.assertFalse(os.path.exists(os.path.join(out, "f.png")))

    def test_sting_logs_clean_strings(self):
        from polished.brand import stings
        out = os.path.join(self.d, "c")
        stings.DRAWN.clear()
        stings.setup("GTA 6", "OwnerBrand", "ownerlogin", out, retired=["OldName"])
        stings.ws.make_intro(None, "A clean title", frame_png=os.path.join(out, "f.png"))
        self.assertTrue(os.path.exists(os.path.join(out, "f.png")))
        self.assertIn("A CLEAN TITLE", stings.DRAWN)
        self.assertIn("OWNERBRAND", stings.DRAWN)


class StreamScenes(unittest.TestCase):
    """T3 12 Nov: a 9-min STARTING SOON countdown was kept in Part 1 (menu candidate, model left it in)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_plan(self, an, *args):
        ap = os.path.join(self.d, "an.json")
        json.dump(an, open(ap, "w"))
        r = plan(ap, os.path.join(self.d, "w"), "--no-llm", *args)
        self.assertEqual(r.returncode, 0, r.stdout[-600:])
        return json.load(open(os.path.join(self.d, "w", "plan.json"), encoding="utf-8"))

    def test_quiet_starting_screen_is_cut_by_code(self):
        an = synth(3600)
        # 0-560 s: still countdown card, menu-like, only a few words
        an["transcript"]["segments"] = [s for s in an["transcript"]["segments"] if s["s"] >= 560 or s["s"] % 60 < 1.3]
        an["menu"] = [{"t": float(t), "menu": 0.97 if t < 560 else 0.05, "scene": 0.0} for t in range(3600)]
        for v in an["visual"]:
            if v["t"] < 560:
                v["diff"] = 1.3
        p = self.run_plan(an, "--vod-start")
        self.assertGreaterEqual(p["keep"][0][0], 555.0, p["keep"][:2])

    def test_stream_scene_is_cut_even_when_he_talks_over_it(self):
        an = synth(3600)
        an["menu"] = [{"t": float(t), "menu": 0.1, "scene": 0.9 if 1000 <= t < 1300 else 0.02} for t in range(3600)]
        p = self.run_plan(an, "--start", "0")
        kept_in_scene = sum(max(0, min(e, 1295) - max(s, 1005)) for s, e in p["keep"])
        self.assertEqual(kept_in_scene, 0)
        self.assertIn("scene", {c[2] for c in p["cuts"]})


class BrbOverPausedGame(unittest.TestCase):
    """13 Nov Vast T3: a BE RIGHT BACK card over a paused game (CLIP saw the game: menu 0.1, scene 0.02), picture
    barely moving, whisper inventing lines over the music, then him coming back and talking."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_card_stretch_cut_his_return_kept(self):
        an = synth(3600)
        segs = [s for s in an["transcript"]["segments"] if not 600 <= s["s"] < 810]
        for t0 in (650.0, 700.0, 760.0):              # hallucinated fragments over the music
            segs.append({"s": t0, "e": t0 + 3, "text": "Thank you for watching.",
                         "words": [{"w": "Thank", "s": t0, "e": t0 + 0.5, "p": 0.9},
                                   {"w": "you.", "s": t0 + 0.6, "e": t0 + 1.0, "p": 0.9}]})
        segs.sort(key=lambda s: s["s"])
        an["transcript"]["segments"] = segs
        for v in an["visual"]:
            if 600 <= v["t"] < 830:
                v["diff"] = 1.3                         # paused game + animated card: just above the static limit
        an["menu"] = [{"t": float(t), "menu": 0.1, "scene": 0.02} for t in range(3600)]
        ap = os.path.join(self.d, "an.json")
        json.dump(an, open(ap, "w"))
        r = plan(ap, os.path.join(self.d, "w"), "--no-llm", "--start", "0")
        self.assertEqual(r.returncode, 0, r.stdout[-600:])
        p = json.load(open(os.path.join(self.d, "w", "plan.json"), encoding="utf-8"))
        kept_card = sum(max(0, min(e, 805) - max(s, 605)) for s, e in p["keep"])
        self.assertLess(kept_card, 5, p["keep"][:6])
        kept_return = sum(max(0, min(e, 829) - max(s, 811)) for s, e in p["keep"])
        self.assertGreater(kept_return, 15)              # his words when he is back stay in


def synth_sentences(dur, n=6):
    """Run-on speech: n words back to back (no gap, no punctuation) then 'word.' and a 0.8 s pause.
    Only a sentence end or a pause is a legal cold-open edge here."""
    an = synth(dur)
    words, t, k, last_breath = [], 2.0, 0, 0.0
    while t < dur - 1:
        k += 1
        end = k % n == 0
        words.append({"w": " word." if end else " word", "s": round(t, 2), "e": round(t + 0.4, 2), "p": 0.9})
        t += 0.4 + (0.8 if end else 0.0)
        if end and t - last_breath >= 300:              # a 4 s breath every 5 min: the legal episode ends
            t += 4.0
            last_breath = t
    an["transcript"] = {"segments": [{"s": w["s"], "e": w["e"], "text": w["w"], "words": [w]} for w in words]}
    return an, words


class ColdOpenSentences(unittest.TestCase):
    """Run 43354198510 (13 Nov) was held by the review: the cold open ended on the 'The' of a repeated
    "The cake's alive." and began 2 ms into "I'm". snap_cold now never puts an edge inside a sentence."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_cold_open_is_whole_sentences(self):
        an, words = synth_sentences(3600)
        ap = os.path.join(self.d, "an.json")
        json.dump(an, open(ap, "w"))
        r = plan(ap, os.path.join(self.d, "ep"), "--no-llm", "--start", "0")
        self.assertEqual(r.returncode, 0, r.stdout[-600:] + r.stderr[-600:])
        s, e = json.load(open(os.path.join(self.d, "ep", "plan.json"), encoding="utf-8"))["cold"]
        inside = [i for i, w in enumerate(words) if s <= w["s"] and w["e"] <= e]
        self.assertTrue(inside)
        first, last = inside[0], inside[-1]
        self.assertFalse(any(w["s"] < s < w["e"] or w["s"] < e < w["e"] for w in words), (s, e))   # no word sliced
        self.assertTrue(first == 0 or words[first - 1]["w"].endswith("."), (s, e))               # opens a sentence
        self.assertTrue(words[last]["w"].endswith("."), (s, e))                                  # ends one
        self.assertTrue(5.0 <= e - s <= 15.5)


class SeamPolish(unittest.TestCase):
    """13 Nov BRB-proof review: a line heard twice across a cut, and an episode opening on whisper noise."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def plan_of(self, an, *args):
        ap = os.path.join(self.d, "an.json")
        json.dump(an, open(ap, "w"))
        r = plan(ap, os.path.join(self.d, "w"), "--no-llm", *args)
        self.assertEqual(r.returncode, 0, r.stdout[-600:])
        return json.load(open(os.path.join(self.d, "w", "plan.json"), encoding="utf-8"))

    def test_line_said_again_after_a_cut_is_heard_once(self):
        an = synth(3600)
        line = ["where's", "the", "clothing", "store"]
        segs = [s for s in an["transcript"]["segments"] if not 600 <= s["s"] < 700]
        for t0 in (600.0, 690.0):                      # said, a long silence (cut), said again
            ws = [{"w": w, "s": t0 + i * 0.4, "e": t0 + i * 0.4 + 0.3, "p": 0.9} for i, w in enumerate(line)]
            segs.append({"s": t0, "e": t0 + 1.6, "text": " ".join(line), "words": ws})
        segs.sort(key=lambda s: s["s"])
        an["transcript"]["segments"] = segs
        for i in range(int(603.0 / 0.1), int(689.0 / 0.1)):   # the silence between is absolutely quiet
            an["audio_map"]["rms"][i] = -80.0
        for v in an["visual"]:
            if 603 <= v["t"] < 689:
                v["diff"] = 0.5
        p = self.plan_of(an, "--start", "0")
        kept_first = sum(max(0, min(e, 601.5) - max(s, 600.0)) for s, e in p["keep"])
        kept_second = sum(max(0, min(e, 691.5) - max(s, 690.0)) for s, e in p["keep"])
        self.assertLess(kept_first, 0.3, p["keep"][:6])
        self.assertGreater(kept_second, 1.3)

    def test_noise_head_before_a_dead_stretch_is_not_the_opening(self):
        an = synth(3600)
        an["transcript"]["segments"] = [s for s in an["transcript"]["segments"] if not 3 <= s["s"] < 300]
        for v in an["visual"]:
            if 3 <= v["t"] < 300:
                v["diff"] = 1.3
        an["menu"] = [{"t": float(t), "menu": 0.1, "scene": 0.02} for t in range(3600)]
        p = self.plan_of(an, "--start", "0")
        self.assertGreater(p["keep"][0][0], 250.0, p["keep"][:3])

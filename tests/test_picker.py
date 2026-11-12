"""Clip picker v2 (2026-11-12 quality pass): live-window timing, cut boundaries, footage check, judge + floor."""
import _ident  # noqa: F401  (sets CLIPBOT_ID before clipbot is imported)
import unittest
from unittest import mock

from clipbot import find_vod_clips as f
from clipbot import judge as j


def phrase(text, t, rate=0.3):
    """Words of one phrase starting at t, one word every `rate` s (no pauses inside)."""
    return [{"w": w, "s": round(t + k * rate, 2), "e": round(t + k * rate + rate * 0.8, 2)} for k, w in enumerate(text.split())]


class LiveWindowTiming(unittest.TestCase):
    def test_hour_zero_window_seeks(self):
        # 0 is "no start time" to yt-dlp and a live VOD then records from the live edge
        self.assertEqual(f.audio_base(0, 7200), 1.0)
        self.assertEqual(f.audio_base(7200, 14400), 7200)
        self.assertEqual(f.audio_base(14400, 0), 14400)
        self.assertEqual(f.audio_base(0, 0), 0.0)            # whole VOD: no section at all

    def test_screen_guards_only_at_the_streams_own_start_and_end(self):
        words = phrase("oh my god what the hell oh my god no way", 100) + phrase("oh my god what the hell no way", 7150)
        pk = [-5.0] * 15000
        first = f.windows(words, pk, 7200, [], base=1.0, tail=False)
        self.assertEqual([round(w["s"]) for w in first], [7150])      # stream start (<8 min) skipped, window end kept
        later = f.windows(words, pk, 7200, [], base=7200, tail=False)
        self.assertEqual(len(later), 2)                                 # 8 min into a later window is fine
        tail = f.windows(words, pk, 7200, [], base=7200, tail=True)
        self.assertEqual(len(tail), 1)                                  # the stream's last minute is the end screen


class Snap(unittest.TestCase):
    def setUp(self):
        self.ws = (phrase("so we drive up to the gas station and fill the car", 10)
                   + phrase("wait is this the gas can or the water", 15)
                   + phrase("oh no I drank it I drank the gas", 20)
                   + phrase("ha ha ha ha", 22.6)
                   + phrase("anyway let us go check the map now", 30))

    def test_starts_on_a_phrase_and_never_inside_a_word(self):
        s, e = f.snap(self.ws, 16.0, 22.0, 600)               # judge said 16 s: that is mid-phrase
        self.assertIn(s, (9.6, 14.6))                         # a phrase start (grown to 14 s with its setup)
        for w in self.ws:
            self.assertFalse(w["s"] < s < w["e"], f"start {s} cuts {w}")
            self.assertFalse(w["s"] < e < w["e"], f"end {e} cuts {w}")

    def test_end_waits_for_the_laugh_but_not_the_next_topic(self):
        s, e = f.snap(self.ws, 15.0, 21.0, 600)
        self.assertGreaterEqual(e, 22.6 + 3 * 0.3 + 0.24)     # the laugh is in
        self.assertLess(e, 30.0)                              # "anyway let us go check the map" is not

    def test_a_single_line_gets_its_setup_not_silence(self):
        s, e = f.snap(self.ws, 20.0, 22.0, 600)
        self.assertGreaterEqual(e - s, f.CUT_MIN - 0.5)
        self.assertLessEqual(s, 15.0)                         # grew backwards into the setup phrases

    def test_max_length_keeps_the_payoff(self):
        ws = []
        for k in range(30):
            ws += phrase(f"line {k} goes on and on", 100 + k * 3)
        s, e = f.snap(ws, 100.0, 190.0, 600)
        self.assertLessEqual(e - s, f.MAX_LEN + 0.01)
        self.assertGreater(e, 189.0)

    def test_floor_pads_a_lonely_line(self):
        s, e = f.snap(phrase("no way", 300), 300.0, 300.6, 600)
        self.assertGreaterEqual(round(e - s, 2), f.CUT_FLOOR - 0.01)


class Aligned(unittest.TestCase):
    # real 11 Nov reject: the card's transcript vs what the cut footage actually says
    claim = phrase("Wait it wasn't hostile over here Oh okay Have they forgiven me Have I been forgiven per chance "
                   "I ain't even do anything How did he even know it was me", 0)
    wrong = phrase("OV is at a hundred No if we could just get like a hundred and twenty that would be nice "
                   "All right Where are you Oh here you are Wait isn't this what I", 0)
    right = phrase("wait it wasn't hostile over here oh okay have they forgiven me have I been forgiven perchance "
                   "I ain't even do nothing how did he know it was me", 0)

    def test_wrong_footage_is_refused(self):
        self.assertFalse(f.aligned(self.claim, self.wrong)[0])

    def test_same_moment_passes_despite_transcription_noise(self):
        ok, r = f.aligned(self.claim, self.right)
        self.assertTrue(ok)
        self.assertGreater(r, 0.6)

    def test_too_few_words_is_not_verified(self):
        self.assertFalse(f.aligned(phrase("oh no", 0), phrase("oh no", 0))[0])


class JudgeParse(unittest.TestCase):
    def cands(self, n=3):
        return [{"s": 100.0 * k, "e": 100.0 * k + 20, "region": (100.0 * k - 20, 100.0 * k + 35), "words": [], "score": 10.0}
                for k in range(1, n + 1)]

    def test_validates_and_converts_times(self):
        cs = self.cands()
        r = {"judged": [
            {"i": 0, "score": 8, "who": "streamer", "kind": "fail", "start": 5, "end": 40, "title": "I Drank The Gas — Again"},
            {"i": 1, "score": "11", "who": "streamer"},                  # out of range -> dropped
            {"i": 2, "score": 6, "who": "game", "kind": "weird", "start": 30, "end": 10},  # bad bounds, unknown kind
            {"i": 0, "score": 1},                                         # duplicate -> ignored
            {"i": 9, "score": 9}, "junk"]}
        v = j.parse(r, cs)
        self.assertEqual(sorted(v), [0, 2])
        self.assertEqual(v[0]["bounds"], (85.0, 120.0))
        self.assertEqual(v[0]["title"], "I Drank The Gas , Again")       # no em dashes on screen
        self.assertIsNone(v[2]["bounds"])
        self.assertEqual(v[2]["kind"], "")

    def test_veto_rules_and_rank(self):
        base = {"score": 3, "who": "streamer", "kind": "chat", "bounds": None, "title": ""}
        self.assertFalse(j.vetoed(base))                                # a low score alone never vetoes (calibration)
        self.assertTrue(j.vetoed({**base, "who": "game"}))              # a cutscene's script is not his reaction
        for k in ("menu", "loading", "logistics", "sponsor"):
            self.assertTrue(j.vetoed({**base, "kind": k}), k)
        self.assertAlmostEqual(j.rank({"score": 20.0, "judge": {**base, "score": 4}}), 2.0)
        self.assertAlmostEqual(j.rank({"score": 20.0}), 1.0)

    def test_garbage_answer_is_no_verdicts(self):
        self.assertEqual(j.parse(None, self.cands()), {})
        self.assertEqual(j.parse({"picks": []}, self.cands()), {})

    def test_prompt_has_every_candidate_with_relative_times(self):
        cs = self.cands(2)
        cs[0]["words"] = phrase("oh my god he ran me over", 82.0)
        p = j.prompt(cs, "GTA 6 DAY ONE")
        self.assertIn("#0 (stretch is 55 s long)", p)
        self.assertIn("[2.0] oh my god he ran me over", p)
        self.assertIn("#1 (stretch is 55 s long)\n(no speech)", p)
        self.assertIn("GTA 6 DAY ONE", p)


class Choose(unittest.TestCase):
    def cands(self):
        out = []
        for k, sc in enumerate([30.0, 22.0, 12.0, 9.0]):
            t = 1000.0 * (k + 1)
            out.append({"s": t, "e": t + 25, "score": sc, "peak": -5, "text": "x",
                        "words": phrase("so here is the setup line for this one", t - 3) + phrase("oh no he crashed the car again", t + 1)})
        return out

    def run_choose(self, verdicts, cands=None):
        cs = cands or self.cands()

        def fake_judge(wav, cands, dur, title, ask=None):
            for i, c in enumerate(cands):
                if i in verdicts:
                    c["judge"] = verdicts[i]
            return True
        with mock.patch.object(f.judge, "judge", side_effect=fake_judge):
            return f.choose(cs, "a.wav", 7200, "t", 3, [])

    def v(self, score, **kw):
        return {"score": score, "who": "streamer", "kind": "reaction", "bounds": None, "title": f"T{score}", **kw}

    def test_ranked_best_first_vetoes_and_floor_drop(self):
        # ranks: 30/20+5/4=2.75, 22/20+8/4=3.1, 12/20+1/4=0.85 (under the floor), 9/20+9/4=2.7 but menu (veto)
        out = self.run_choose({0: self.v(5), 1: self.v(8), 2: self.v(1), 3: self.v(9, kind="menu")})
        self.assertEqual([t for _, t in out], ["T8", "T5"])

    def test_a_weak_verdict_needs_chat_to_get_over_the_floor(self):
        cs = self.cands()
        self.assertEqual(self.run_choose({0: self.v(2)}, [cs[0]]), [])          # 1.5 + 0.5 = 2.0
        cs[0]["chat"] = (1, 1)                                                   # + 1.2
        self.assertEqual(len(self.run_choose({0: self.v(2)}, [cs[0]])), 1)

    def test_slow_window_offers_nothing(self):
        cs = [{**c, "score": 4.0} for c in self.cands()]
        self.assertEqual(self.run_choose({0: self.v(2), 1: self.v(1), 2: self.v(9, who="game"), 3: self.v(8, kind="sponsor")}, cs), [])

    def test_cut_is_snapped_and_text_follows_the_cut(self):
        out = self.run_choose({0: self.v(9, bounds=(1001.5, 1012.0))}, [self.cands()[0]])
        c = out[0][0]
        self.assertLessEqual(c["s"], 997.0)                   # back to the setup phrase
        self.assertIn("setup", c["text"])
        self.assertIn("crashed", c["text"])

    def test_judge_down_uses_floor_then_ranker_order(self):
        cs = self.cands()
        with mock.patch.object(f.judge, "judge", return_value=False),                 mock.patch.object(f, "rerank", return_value=[(1, "Ranked"), (0, "Top")]) as rr:
            out = f.choose(cs, "a.wav", 7200, "t", 3, [])
        self.assertEqual(len(rr.call_args[0][0]), 2)          # only the 30 and 22 cleared the floor of 20
        self.assertEqual([t for _, t in out], ["Ranked", "Top"])

    def test_judge_crash_falls_back_instead_of_losing_the_window(self):
        with mock.patch.object(f.judge, "judge", side_effect=RuntimeError("model download failed")),                 mock.patch.object(f, "rerank", return_value=None):
            out = f.choose(self.cands(), "a.wav", 7200, "t", 3, [])
        self.assertEqual(len(out), 2)

    def test_nothing_above_floor_without_judge_is_nothing(self):
        cs = [c for c in self.cands() if c["score"] < 20]
        with mock.patch.object(f.judge, "judge", return_value=False):
            self.assertEqual(f.choose(cs, "a.wav", 7200, "t", 3, []), [])


class Stillness(unittest.TestCase):
    def make(self, src, path):
        import subprocess
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-t", "6", "-pix_fmt", "yuv420p", path], check=True)

    def test_menu_like_static_vs_moving_picture(self):
        import os
        import tempfile
        d = tempfile.mkdtemp()
        still, moving = os.path.join(d, "s.mp4"), os.path.join(d, "m.mp4")
        self.make("color=c=0x202030:s=640x360:r=30,drawtext=text='INVENTORY':x=40:y=40:fontsize=40:fontcolor=white", still)
        self.make("testsrc2=s=640x360:r=30", moving)
        self.assertGreaterEqual(f.stillness(still), f.STILL_MAX)
        self.assertLess(f.stillness(moving), 0.5)
        self.assertEqual(f.stillness(os.path.join(d, "missing.mp4")), 1.0)   # unreadable = never offered blind


class GtaScoring(unittest.TestCase):
    def test_gta_moments_and_laughs_score(self):
        dead = phrase("no no no I got wasted again by the cops", 0)
        calm = phrase("okay let me just go to the shop and buy some", 0)
        self.assertGreater(f.score(dead), f.score(calm) + 5)
        self.assertGreater(f.score(phrase("he ran me over haha haha", 0)), 4)


if __name__ == "__main__":
    unittest.main()


class Chat(unittest.TestCase):
    def test_offset_paging_dedupes_and_flags_reactions(self):
        from clipbot import chat
        msgs = [(100, "hello"), (130, "LUL"), (130, "KEKW that was mad"), (400, "gg"), (401, "ok")]

        def page(vod, t):   # each page: up to 3 messages from t on, the first repeated from the last page
            got = [m for m in msgs if m[0] >= t - 1][:3]
            return {"edges": [{"node": {"id": f"{o}-{x}", "contentOffsetSeconds": o, "message": {"fragments": [{"text": x}]}}}
                              for o, x in got]}
        tl = chat.timeline("1", 0, 450, page=page, pause=0)
        self.assertEqual(tl, [(100, False), (130, True), (130, True), (400, False), (401, False)])
        self.assertEqual(chat.around(tl, 95, 110), (3, 2))         # from 3 s in to 30 s after the end
        self.assertEqual(chat.boost(2, 2), 2.0)
        self.assertEqual(chat.boost(1, 0), 0.4)

    def test_failure_is_no_boost_not_a_failed_run(self):
        from clipbot import chat

        def page(vod, t):
            raise OSError("integrity check")
        self.assertEqual(chat.timeline("1", 0, 100, page=page, pause=0), [])

    def test_chat_lifts_a_quiet_moment_over_the_floor(self):
        c = {"score": 16.0, "judge": {"score": 4, "who": "streamer", "kind": "reaction"}}
        self.assertLess(j.rank(c), j.FLOOR)
        self.assertGreater(j.rank({**c, "chat": (2, 1)}), j.FLOOR)


class Titles(unittest.TestCase):
    def test_titles_about_him_are_dropped(self):
        cs = [{"s": 100.0, "e": 120.0, "region": (80.0, 135.0), "words": [], "score": 10.0}] * 2
        v = j.parse({"judged": [{"i": 0, "score": 6, "title": "Streamer Announces Enemies Are Down"},
                                {"i": 1, "score": 6, "title": "I Barely Escaped The Game Swallowing Me"}]}, cs)
        self.assertEqual(v[0]["title"], "")
        self.assertEqual(v[1]["title"], "I Barely Escaped The Game Swallowing Me")

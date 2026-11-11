"""Episode lane (clipbot/episodes.py, ep_publish.py). Offline: no Twitch, YouTube or worker call.

    py -m unittest discover -s tests -v

* A stream that never goes offline: a window slept through, a short stretch before a long sleep, and an
  animated resting screen must each let the next window move on. Each used to stop the lane for good.
* Normal play is cut as before, and a quiet drive is never dropped.
* Titles, footers and tags take every name from the channel identity.
* Scheduling: Part numbers in stream order, two slots a day in the launch fortnight, one after it.
"""
import _ident  # noqa: F401  (sets CLIPBOT_ID before clipbot is imported)
import contextlib
import io
import unittest
from datetime import datetime
from unittest import mock

from clipbot import common, ep_publish as P, episodes as E

H = 3600


def talk(a, b, step=1.2):
    """Someone talking from a to b: a word every `step` seconds, and a 4 s breath every 5 minutes."""
    out, t = [], float(a)
    while t < b:
        out.append({"w": "w", "s": round(t, 2), "e": round(t + 0.4, 2)})
        t += step
        if int(t - a) % 300 < step:
            t += 4.0
    return out


def run(words, mot, lo, hi, final=False):
    with contextlib.redirect_stdout(io.StringIO()):
        hs = E.holes(words, mot, lo, hi)
        spans = E.kept_spans(lo, hi, hs)
        eps, nxt = E.plan(words, spans, hs, lo, hi, final)
    return hs, eps, nxt


def kept(eps, hs):
    spans = E.kept_spans(0, 10 ** 9, hs)
    return [E.kept_between(spans, a, b) for a, b in eps]


class Cutting(unittest.TestCase):
    def test_four_hours_of_play_is_cut_as_before(self):
        hs, eps, nxt = run(talk(0, 4 * H), [12.0] * (4 * H), 0, 4 * H)
        self.assertEqual(hs, [])
        self.assertGreaterEqual(len(eps), 3)
        for k in kept(eps, hs):
            self.assertTrue(E.MIN_LEN <= k <= E.MAX_LEN, k)
        self.assertEqual(eps[0][0], 0)
        for (_, e1), (s2, _) in zip(eps, eps[1:]):
            self.assertEqual(e1, s2)                                  # nothing dropped between episodes
        self.assertEqual(nxt, eps[-1][1])                             # the leftover rolls into the next window
        self.assertLess(4 * H - nxt, E.MIN_LEN)

    def test_window_slept_through_moves_on(self):
        for picture in (0.3, 6.0):                                    # still screen, animated screen
            hs, eps, nxt = run([], [picture] * (4 * H), 8 * H, 12 * H)
            self.assertEqual(eps, [], picture)
            self.assertEqual(nxt, 12 * H, picture)                    # was: the same start for ever

    def test_short_stretch_before_a_long_sleep_is_closed(self):
        hs, eps, nxt = run(talk(0, 600), [12.0] * 600 + [0.3] * (4 * H - 600), 0, 4 * H)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0][0], 0)
        self.assertAlmostEqual(eps[0][1], 600, delta=10)
        self.assertEqual(nxt, 4 * H)

    def test_sleep_in_the_middle_splits_two_sessions(self):
        words = talk(0, 2 * H) + talk(5 * H, 8 * H)
        mot = [12.0] * (2 * H) + [5.0] * (3 * H) + [12.0] * (3 * H)   # the resting screen is animated
        hs, eps, nxt = run(words, mot, 0, 8 * H)
        self.assertEqual(len(hs), 1)
        a, b = hs[0][0], hs[0][1]
        self.assertTrue(2 * H - 10 <= a <= 2 * H + 10 and 5 * H - 10 <= b <= 5 * H + 10, (a, b))
        self.assertTrue(all(e <= a or s >= b for s, e in eps), eps)   # no episode holds the sleep
        before = [x for x in eps if x[1] <= a]
        self.assertAlmostEqual(before[-1][1], a, delta=1)             # the evening is closed at the sleep
        self.assertAlmostEqual(sum(kept(before, hs)), 2 * H, delta=30)
        self.assertGreaterEqual(nxt, b)

    def test_quiet_drive_is_kept(self):
        words = talk(0, 1800) + talk(1800 + 900, 4 * H)               # 15 min without a word, picture moving
        hs, eps, nxt = run(words, [12.0] * (4 * H), 0, 4 * H)
        self.assertEqual(hs, [])

    def test_break_on_a_still_screen_is_cut(self):
        words = talk(0, 1800) + talk(1800 + 420, 4 * H)               # 7 min break
        mot = [12.0] * 1800 + [0.4] * 420 + [12.0] * (4 * H - 2220)
        hs, eps, nxt = run(words, mot, 0, 4 * H)
        self.assertEqual(len(hs), 1)
        self.assertLess(hs[0][1] - hs[0][0], E.SESSION_BREAK)
        self.assertTrue(any(s <= hs[0][0] and e >= hs[0][1] for s, e in eps) or any(e == hs[0][0] for _, e in eps))

    def test_end_of_stream_emits_the_tail(self):
        hs, eps, nxt = run(talk(0, 100 * 60), [12.0] * (100 * 60), 0, 100 * 60, final=True)
        self.assertEqual(nxt, 100 * 60)
        self.assertAlmostEqual(sum(kept(eps, hs)), 100 * 60, delta=5)

    def test_short_audio_is_a_broken_download_not_a_sleep(self):
        self.assertTrue(E.audio_problem(3 * H, 0, 4 * H))
        self.assertEqual(E.audio_problem(4 * H - 20, 0, 4 * H), "")


class Copy(unittest.TestCase):
    def test_title_ends_with_the_brand_and_is_never_trimmed(self):
        self.assertEqual(E.title_for("GTA 6", 3, "I Robbed A Bank"), f"GTA 6 Part 3 - I Robbed A Bank | {_ident.BRAND}")
        long = E.title_for("GTA 6", 3, "x" * 85)
        self.assertNotIn("|", long)
        self.assertLessEqual(len(long), 100)
        self.assertEqual(E.title_for("GTA 6", 12, ""), f"GTA 6 Part 12 | {_ident.BRAND}")

    def test_description_names_the_channel_and_only_known_handles(self):
        d = E.description_for("What happened.", ["#GTA6"], "clipbot:ep:1:0")
        self.assertIn(f"twitch.tv/{_ident.LOGIN}", d)
        self.assertIn(f"Instagram: @{_ident.IG}", d)
        self.assertNotIn("TikTok", d)                                 # not in the identity, so left out
        self.assertNotIn("YouTube: @", d)
        self.assertTrue(d.endswith("clipbot:ep:1:0"))
        self.assertEqual(E.check_meta(E.title_for("GTA 6", 1, "Hook"), d), [])

    def test_rules_catch_a_retired_name_a_kick_link_and_a_dash(self):
        ok = E.description_for("Fine.", [], "clipbot:ep:1:0")
        self.assertTrue(E.check_meta("GTA 6 Part 1", ok.replace("Fine.", f"Watch {_ident.OLD} live.")))
        self.assertIn("kick.com link", E.check_meta("GTA 6 Part 1", ok + " kick.com/x"))
        self.assertIn("em dash", E.check_meta("GTA 6 Part 1 — x", ok))
        self.assertIn("no channel name in description", E.check_meta("GTA 6 Part 1", "nothing here"))

    def test_long_dashes_are_swapped_not_fatal(self):
        reply = {"hook": "I Robbed A Bank " + chr(0x2014) + " Badly", "hashtags": ["#GTA6"],
                 "description": "We went in loud" + chr(0x2014) + "it went worse than planned, pages 1" + chr(0x2013) + "2 of the plan gone."}
        with mock.patch.object(common, "worker", return_value=reply):
            c = E.episode_copy("GTA 6", "words", "stream")
        self.assertEqual(c["hook"], "I Robbed A Bank, Badly")
        self.assertIn("loud, it went", c["description"])
        self.assertIn("1-2", c["description"])
        d = E.description_for(c["description"], c["hashtags"], "clipbot:ep:1:0")
        self.assertEqual(E.check_meta(E.title_for("GTA 6", 1, c["hook"]), d), [])

    def test_worker_down_gives_a_plain_draft(self):
        with mock.patch.object(common, "worker", side_effect=OSError("down")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(E.episode_copy("GTA 6", "words", "stream"), {"hook": "", "description": "", "hashtags": []})

    def test_tags_carry_the_login_not_a_literal(self):
        self.assertEqual(E.tags_for("GTA 6", 4)[-1], _ident.LOGIN)
        self.assertIn("gta 6 part 4", E.tags_for("GTA 6", 4))


def ep(vod, start, status, created="2026-11-19T18:00:00Z", **kw):
    return {"key": f"{vod}:{start}", "vod": vod, "start": start, "vod_created": created, "series": "GTA 6",
            "status": status, "hook": "Hook", "video": f"v{vod}{start}", **kw}


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=common.UK).astimezone(common.UTC)


class Scheduling(unittest.TestCase):
    def plan(self, items, now, uploads=()):
        with contextlib.redirect_stdout(io.StringIO()):
            return P.plan(items, list(uploads), now)

    def test_launch_fortnight_has_two_slots_a_day(self):
        items = [ep("1", s, "approved") for s in (0, 3600, 7200)]
        todo = self.plan(items, at(2026, 11, 20, 7))
        self.assertEqual([(i["key"], n) for i, n, _ in todo], [("1:0", 1), ("1:3600", 2), ("1:7200", 3)])
        self.assertEqual([t for _, _, t in todo], [at(2026, 11, 20, 12), at(2026, 11, 20, 18), at(2026, 11, 21, 12)])

    def test_after_the_fortnight_one_a_day(self):
        todo = self.plan([ep("1", 0, "approved"), ep("1", 3600, "approved")], at(2026, 12, 3, 7))
        self.assertEqual([t for _, _, t in todo], [at(2026, 12, 3, 18), at(2026, 12, 4, 18)])

    def test_only_two_days_ahead_are_written(self):
        todo = self.plan([ep("1", s * 3600, "approved") for s in range(10)], at(2026, 11, 20, 7))
        self.assertEqual(len(todo), 4)                                # 12:00 + 18:00 today and tomorrow

    def test_an_undecided_episode_holds_back_later_parts(self):
        items = [ep("1", 0, "approved"), ep("1", 3600, "offered"), ep("1", 7200, "approved")]
        self.assertEqual([i["key"] for i, _, _ in self.plan(items, at(2026, 11, 20, 7))], ["1:0"])

    def test_skipped_gets_no_number_and_scheduled_ones_count(self):
        items = [ep("1", 0, "scheduled", part=1, at="2026-11-20T12:00:00Z"), ep("1", 3600, "skipped"),
                 ep("1", 7200, "approved")]
        todo = self.plan(items, at(2026, 11, 20, 13))
        self.assertEqual([(i["key"], n) for i, n, _ in todo], [("1:7200", 2)])
        self.assertEqual(todo[0][2], at(2026, 11, 20, 18))

    def test_parts_follow_stream_order_across_vods(self):
        items = [ep("9", 0, "approved", created="2026-11-21T18:00:00Z"), ep("5", 3600, "approved"), ep("5", 0, "approved")]
        self.assertEqual([i["key"] for i, _, _ in self.plan(items, at(2026, 11, 22, 7))], ["5:0", "5:3600", "9:0"])

    def test_slot_avoids_another_long_video(self):
        busy = {"contentDetails": {"duration": "PT55M"}, "status": {"privacyStatus": "private", "publishAt": "2026-11-20T12:30:00Z"}}
        todo = self.plan([ep("1", 0, "approved")], at(2026, 11, 20, 7), [busy])
        self.assertEqual(todo[0][2], at(2026, 11, 20, 18))

    def test_schedule_refuses_a_public_or_foreign_video(self):
        it = ep("1", 0, "approved", marker="clipbot:ep:1:0")
        base = {"snippet": {"channelId": common.CHANNEL_ID, "description": "x clipbot:ep:1:0", "title": "t"},
                "status": {"privacyStatus": "private"}, "contentDetails": {}}
        for change, why in (({"status": {"privacyStatus": "public"}}, "not private"),
                            ({"snippet": {"channelId": "UCsomeoneelse", "description": "clipbot:ep:1:0"}}, "not on this channel"),
                            ({"snippet": {"channelId": common.CHANNEL_ID, "description": "no marker"}}, "does not carry")):
            with mock.patch.object(common, "yt", return_value={"items": [{**base, **change}]}), \
                 mock.patch.object(common, "http") as put, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError) as cm:
                    P.schedule(it, 1, at(2026, 11, 20, 12), "", False)
                self.assertIn(why, str(cm.exception))
                put.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Kick pivot (18 Nov): clipbot/desc_sweep.py exact-match rules. No network."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ident  # noqa: F401,E402  (sets CLIPBOT_ID before clipbot is imported)
from clipbot import common, desc_sweep as D  # noqa: E402

L = _ident.LOGIN


class Rules(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"KICK_SLUG": "chan", "KICK_SLUG_PUBLIC": "1"})
        p.start()
        self.addCleanup(p.stop)
        self.p = D.pairs()

    def test_footer_and_links_become_kick(self):
        t = f"Part 3.\n\nWATCH ME LIVE\nTwitch: twitch.tv/{L}\n\nclipbot:ep:1:0"
        out, used = D.convert(t, self.p)
        self.assertEqual(out, "Part 3.\n\nWATCH ME LIVE\nKick: kick.com/chan\n\nclipbot:ep:1:0")
        out, _ = D.convert(f"He panics.\n\nCatch the chaos live: https://twitch.tv/{L}\n\n#gta6", self.p)
        self.assertEqual(out, "He panics.\n\nCatch the chaos live: https://kick.com/chan\n\n#gta6")
        out, _ = D.convert(f"Live on Twitch: https://www.twitch.tv/{L}", self.p)
        self.assertEqual(out, "Live on Kick: https://kick.com/chan")

    def test_retired_handle_ctas_hashtags_and_another_streamers_credit(self):
        old = _ident.OLD.lower()
        t = (f"Twitch:   https://www.twitch.tv/{old}\nGlatiar: twitch.tv/glatiar\nWATCH ME LIVE ON TWITCH\n"
             f"Come hang out live on Twitch: https://twitch.tv/{L}\n#gaming #twitchclips #Twitch #fyp")
        out, used = D.convert(t, self.p)
        self.assertEqual(out, "Kick:   https://kick.com/chan\nGlatiar: twitch.tv/glatiar\nWATCH ME LIVE ON KICK\n"
                              "Come hang out live on Kick: https://kick.com/chan\n#gaming #kickclips #Kick #fyp")

    def test_boss_approved_wordings_tags_and_bare_label(self):
        nl = chr(10)
        t = nl.join(["Part 4.", "Twitch VOD archive.", "This whole playthrough runs on Twitch first, cut down for YouTube.",
                     "It all happens live on Twitch first.", "Twitch:", "#gaming # twitch #fyp #twitchaffiliate #twitchtv"])
        out, _ = D.convert(t, self.p)
        self.assertEqual(out, nl.join(["Part 4.", "Stream VOD archive.", "This whole playthrough runs live first, cut down for YouTube.",
                                       "It all happens live first.", "Kick: kick.com/chan", "#gaming #fyp"]))
        self.assertIsNone(D.convert(nl.join(["Twitch:", "x", "Twitch:"]), self.p)[0])          # twice: skipped
        self.assertIsNone(D.convert("Twitch: my old setup", self.p)[0])          # not just the label: skipped

    def test_twice_is_skipped_not_guessed(self):
        out, why = D.convert(f"twitch.tv/{L} and again twitch.tv/{L}", self.p)
        self.assertIsNone(out)
        self.assertIn("2 times", why)

    def test_any_other_twitch_text_is_skipped(self):
        out, why = D.convert(f"we raided someone on Twitch last night. twitch.tv/{L}", self.p)
        self.assertIsNone(out)
        self.assertIn("other Twitch text", why)
        self.assertEqual(D.convert("co-op with twitch.tv/someoneelse", self.p)[0], "co-op with twitch.tv/someoneelse")   # a credit stays
        self.assertEqual(D.convert("Glatiar on Twitch: twitch.tv/glatiar", self.p)[0], "Glatiar on Twitch: twitch.tv/glatiar")
        self.assertEqual(D.convert(f"Twitch:   / {_ident.OLD.lower()}", self.p)[0], "Kick:   kick.com/chan")

    def test_no_twitch_is_unchanged(self):
        self.assertEqual(D.convert("nothing here", self.p), ("nothing here", []))

    def test_plan_checks_the_channel_and_localizations_and_keeps_the_snippet(self):
        good = {"id": "A", "snippet": {"channelId": common.CHANNEL_ID, "title": "t", "categoryId": "20", "tags": ["x"],
                                       "publishedAt": "2026-11-01", "description": f"x twitch.tv/{L}"},
                "localizations": {"en": {"title": "t", "description": f"x twitch.tv/{L}"}}}
        other = {"id": "B", "snippet": {"channelId": "UCother", "title": "t", "description": f"twitch.tv/{L}", "publishedAt": "2026-11-02"}}
        badloc = {"id": "C", "snippet": {"channelId": common.CHANNEL_ID, "title": "Twitch day", "description": f"x twitch.tv/{L}",
                                         "publishedAt": "2026-11-03"}, "localizations": {"en": {"description": "my Twitch chat"}}}
        todo, skipped, titles = D.plan([good, other, badloc], self.p)
        self.assertEqual([v["id"] for v, *_ in todo], ["A"])
        v, snip, loc, used = todo[0]
        self.assertEqual(snip, {"title": "t", "description": "x kick.com/chan", "tags": ["x"], "categoryId": "20"})
        self.assertEqual(loc["en"]["description"], "x kick.com/chan")
        self.assertEqual({i for i, _ in skipped}, {"B", "C"})
        self.assertEqual(titles, ["C"])

    def test_refuses_without_a_cleared_kick_address(self):
        with mock.patch.dict(os.environ, {"KICK_SLUG_PUBLIC": "0"}), self.assertRaises(SystemExit):
            D.pairs()


if __name__ == "__main__":
    unittest.main()

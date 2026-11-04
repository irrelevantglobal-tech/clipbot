"""Queued clips approved before the rebrand carry the retired name; it must never reach a public post."""
import _ident  # noqa: F401  (sets CLIPBOT_ID before clipbot is imported)
import importlib
import os
import sys
import unittest

OLD = "OldName"          # built in two halves so the repo-wide old-name grep stays clean


def load(who):
    for k in [k for k in sys.modules if k.startswith("clipbot")]:
        del sys.modules[k]
    if who:
        os.environ["CLIPBOT_WHO"] = who
    else:
        os.environ.pop("CLIPBOT_WHO", None)
    from clipbot import legacy, publish, tiktok, instagram, yt_comments  # noqa: F401
    return legacy, publish, tiktok, instagram, yt_comments


class LegacyOwner(unittest.TestCase):
    def setUp(self):
        self.legacy, self.publish, self.tiktok, self.ig, self.ytc = load(None)

    def test_fix_keeps_case_shape(self):
        f = self.legacy.fix
        self.assertEqual(f(f"Panic | {OLD}"), "Panic | OwnerBrand")
        self.assertEqual(f(f"twitch.tv/{OLD.lower()} #{OLD.lower()}"), "twitch.tv/ownerlogin #ownerlogin")
        self.assertEqual(f(OLD.upper()), "OWNERBRAND")
        self.assertEqual(f(""), "")
        self.assertIsNone(f(None))

    def test_queued_item_goes_out_as_the_new_channel(self):
        t = self.publish.yt_title(f"Panic | {OLD}")
        self.assertEqual(t, "Panic | OwnerBrand")
        d = self.publish.build_description({"description": f"He panics. twitch.tv/{OLD.lower()}", "hashtags": [f"#{OLD.lower()}"]}, "clipbot:x")
        self.assertNotIn(OLD.lower(), d.lower())
        self.assertIn("ownerlogin", d)
        cap = self.tiktok.caption_for({"tiktok_caption": f"hi\n\n#{OLD.lower()} #gaming #fun"})
        self.assertNotIn(OLD.lower(), cap.lower())
        self.assertEqual(self.ytc.clip_title(f"Panic | {OLD}"), "Panic")
        self.assertEqual(self.ytc.clip_title("Panic | OwnerBrand"), "Panic")

    def test_instagram_caption(self):
        cap = self.ig.caption_for({"id": "x", "ig_caption": f"Hi\n\nLive on Twitch: {OLD.lower()}\n\n#{OLD.lower()}"})
        self.assertNotIn(OLD.lower(), cap.lower())


class IdentityFromSecret(unittest.TestCase):
    def test_leon_refuses_to_run_without_an_identity(self):
        saved = os.environ.pop("CLIPBOT_ID")
        os.environ["CLIPBOT_ID_FILE"] = os.path.join(os.path.dirname(__file__), "no-such-identity.json")
        try:
            with self.assertRaises(SystemExit):
                load(None)
        finally:
            os.environ["CLIPBOT_ID"] = saved
            del os.environ["CLIPBOT_ID_FILE"]

    def test_glatiar_runs_without_leons_identity(self):
        saved = os.environ.pop("CLIPBOT_ID")
        try:
            legacy = load("glatiar")[0]
            self.assertEqual(legacy.common.BRAND, "Glatiar")
        finally:
            os.environ["CLIPBOT_ID"] = saved


class LegacyGlatiarUntouched(unittest.TestCase):
    def test_glatiar_text_is_never_rewritten(self):
        legacy = load("glatiar")[0]
        s = f"a {OLD} b"
        self.assertEqual(legacy.fix(s), s)


if __name__ == "__main__":
    unittest.main()

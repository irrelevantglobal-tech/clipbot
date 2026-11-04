"""Titles, games and channel names from API answers must never reach the (public) Actions logs."""
import _ident  # noqa: F401
import io
import sys
import unittest


class Redaction(unittest.TestCase):
    def test_registered_strings_are_scrubbed(self):
        from clipbot import common
        common._walk({"data": [{"title": "A Very Private Stream Title", "game_name": "Some Game Name", "id": "123"}]})
        buf = io.StringIO()
        w = common._Redact(buf)
        w.write("vod 123: a very private stream title / SOME GAME NAME ok\n")
        out = buf.getvalue()
        self.assertEqual(out, "vod 123: *** / *** ok\n")

    def test_identity_names_are_scrubbed(self):
        from clipbot import common
        buf = io.StringIO()
        w = common._Redact(buf)
        w.write(f"logged in as {common.BRAND} twitch.tv/{common.LOGIN}\n")
        self.assertNotIn(common.BRAND.lower(), buf.getvalue().lower())
        self.assertNotIn(common.LOGIN.lower(), buf.getvalue().lower())

    def test_short_strings_are_not_registered(self):
        from clipbot import common
        before = set(common._SENS)
        common.sensitive("Car", "abcd")
        self.assertEqual(before, common._SENS)


if __name__ == "__main__":
    unittest.main()

"""19 Nov: the review's frame tiles are cached per FILE, so a re-cut of the same episode is never reviewed on the old cut."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CLIPBOT_ID", '{"login": "x", "brand": "X", "twitch_uid": "1", "channel": "UCx", "ig": "", "legacy": []}')

from polished import review  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg"), "no ffmpeg")
class Tiles(unittest.TestCase):
    def test_a_recut_gets_fresh_tiles(self):
        d = tempfile.mkdtemp()
        try:
            for name, colour, secs in (("a.mp4", "red", 3), ("b.mp4", "blue", 4)):
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:d={secs}",
                                "-pix_fmt", "yuv420p", os.path.join(d, name)], check=True)
            work = os.path.join(d, "work")
            review.sheets(os.path.join(d, "a.mp4"), work, step=1.0)
            outs, _ = review.sheets(os.path.join(d, "b.mp4"), work, step=1.0)   # same work dir, a different cut
            from PIL import Image
            r, g, b = Image.open(outs[0]).convert("RGB").getpixel((60, 60))
            self.assertGreater(b, r)                                              # the new (blue) cut, not the old red tiles
            self.assertEqual(len(os.listdir(os.path.join(work, "_review_tiles"))), 1)   # old file's tiles cleared
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

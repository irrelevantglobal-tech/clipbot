"""Owner 2026-11-12: everything 1080p. Every VOD download is checked on the file itself.

    py -m unittest tests.test_1080 -v      (needs ffmpeg on PATH)
"""
import _ident  # noqa: F401
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from clipbot import render

ROOT = pathlib.Path(__file__).resolve().parents[1] / "clipbot"


def make(path, size, fps=60):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration=1",
                    "-c:v", "libx264", "-preset", "ultrafast", path], check=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class Require1080(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_1080p60_passes(self):
        p = os.path.join(self.d, "a.mp4")
        make(p, "1920x1080")
        self.assertEqual(render.require_1080(p)[:2], (1920, 1080))

    def test_720p_refused(self):
        p = os.path.join(self.d, "b.mp4")
        make(p, "1280x720")
        with self.assertRaisesRegex(RuntimeError, "1280x720, not 1920x1080"):
            render.require_1080(p, "test")

    def test_not_a_video_refused(self):
        p = os.path.join(self.d, "c.mp4")
        pathlib.Path(p).write_bytes(b"nope")
        with self.assertRaises(RuntimeError):
            render.require_1080(p)


class NoSilentFallback(unittest.TestCase):
    def test_vod_downloads_ask_1080_only(self):
        for f in ("episodes.py", "find_vod_clips.py", "thumbs.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            self.assertNotIn("best[height<=1080]", src, f)
            self.assertIn("require_1080", src, f)
        self.assertEqual(render.VOD_FORMAT, "1080p60/1080p")


if __name__ == "__main__":
    unittest.main()

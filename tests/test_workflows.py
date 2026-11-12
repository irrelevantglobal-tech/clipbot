"""Workflow hygiene: the runner's apt mirrors can fail to resolve (11 Nov: 3 runs died on it), so every ffmpeg install retries."""
import glob
import os
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AptRetry(unittest.TestCase):
    def test_every_ffmpeg_install_retries_and_then_checks_the_binary(self):
        files = glob.glob(os.path.join(HERE, ".github", "workflows", "*.yml"))
        seen = 0
        for p in files:
            s = open(p, encoding="utf-8").read()
            if "apt-get install" not in s:
                continue
            seen += 1
            self.assertIn("for i in 1 2 3 4; do sudo apt-get update", s, p)
            self.assertIn("command -v ffmpeg", s, p)
        self.assertGreaterEqual(seen, 6)


if __name__ == "__main__":
    unittest.main()

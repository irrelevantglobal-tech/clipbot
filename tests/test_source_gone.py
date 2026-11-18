"""18 Nov: a clip whose video is gone everywhere (no release asset, the run's artifacts 404) is a permanent SourceGone,
so the X / Reels / TikTok lanes drop it once instead of failing every hour. No network."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ident  # noqa: F401,E402
from clipbot import common, publish  # noqa: E402


class SourceGone(unittest.TestCase):
    def setUp(self):
        for k, v in (("GITHUB_REPOSITORY", "x/clipbot"), ("GITHUB_TOKEN", "t")):
            p = mock.patch.dict(os.environ, {k: v})
            p.start()
            self.addCleanup(p.stop)

    def test_run_gone_is_source_gone(self):
        def http(url, *a, **k):
            raise RuntimeError(f"GET {url} -> 404: Not Found")
        with mock.patch.object(common, "http", side_effect=http), self.assertRaises(publish.SourceGone):
            publish.fetch_artifact("39348101123", "clips-1792446060", tempfile.mkdtemp())

    def test_artifact_expired_is_source_gone(self):
        def http(url, *a, **k):
            if "/releases/" in url:
                raise RuntimeError(f"GET {url} -> 404: Not Found")
            return {"artifacts": [{"name": "clips-1", "expired": True}]}
        with mock.patch.object(common, "http", side_effect=http), self.assertRaises(publish.SourceGone):
            publish.fetch_artifact("1", "clips-1", tempfile.mkdtemp())

    def test_other_errors_are_not_swallowed(self):
        def http(url, *a, **k):
            if "/releases/" in url:
                raise RuntimeError(f"GET {url} -> 404: Not Found")
            raise RuntimeError(f"GET {url} -> 500: boom")
        with mock.patch.object(common, "http", side_effect=http), self.assertRaises(RuntimeError) as cm:
            publish.fetch_artifact("1", "clips-1", tempfile.mkdtemp())
        self.assertNotIsInstance(cm.exception, publish.SourceGone)


if __name__ == "__main__":
    unittest.main()

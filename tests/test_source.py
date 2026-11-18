"""Kick pivot (17 Nov): polished/source.py. No network: a fake get() serves a channel list and a master playlist shaped
like the real ones read 17 Nov (chessbrah, live 5.3 h: 1080p30 source + 720p30..160p, EVENT playlist with ENDLIST)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polished import fetch, source  # noqa: E402

MASTER = "https://stream.kick.com/ivs/v1/1/abc/2026/11/17/14/46/XYZ/media/hls/master.m3u8"
MASTER_TEXT = """#EXTM3U
#EXT-X-SESSION-DATA:DATA-ID="net.live-video.content.id",VALUE="x"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subtitles",NAME="English",URI="subtitles/en/playlist.m3u8",LANGUAGE="en"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="1080p",NAME="1080p",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=8977752,CODECS="avc1.640028,mp4a.40.2",RESOLUTION=1920x1080,VIDEO="1080p",FRAME-RATE=30.000,SUBTITLES="subtitles"
1080p/playlist.m3u8
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=2439511,CODECS="avc1.4D401F,mp4a.40.2",RESOLUTION=1280x720,VIDEO="720p30",FRAME-RATE=30.000
720p30/playlist.m3u8
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=300000,CODECS="avc1.4D401F,mp4a.40.2",RESOLUTION=284x160,VIDEO="160p30",FRAME-RATE=30.000
160p30/playlist.m3u8
"""
VARIANT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:13
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PROGRAM-DATE-TIME:2026-11-17T14:46:53.173Z
#EXTINF:10.000,
0.ts
#EXTINF:10.000,
1.ts
#EXTINF:8.500,
2.ts
#EXT-X-ENDLIST
"""


def vids(live_first=True):
    return [
        {"id": 138370155, "is_live": live_first, "duration": 0 if live_first else 19000000, "session_title": "GTA 6 day 1",
         "start_time": "2026-11-17 14:46:55", "created_at": "2026-11-17 14:46:56", "source": MASTER,
         "video": {"live_stream_id": 138370155, "uuid": "779f3069-aaaa"}},
        {"id": 138324336, "is_live": False, "duration": 31899000, "session_title": "earlier",
         "start_time": "2026-11-17 05:54:40", "created_at": "2026-11-17 05:54:45", "source": MASTER.replace("XYZ", "OLD"),
         "video": {"live_stream_id": 138324336, "uuid": "1111-bbbb"}},
    ]


def fake_get(listing):
    calls = []

    def get(url, **kw):
        calls.append(url)
        if "/api/v2/channels/" in url:
            return json.dumps(listing).encode()
        if url.endswith("master.m3u8"):
            return MASTER_TEXT.encode()
        raise AssertionError("unexpected url " + url)
    get.calls = calls
    return get


class Ids(unittest.TestCase):
    def test_platform_and_number(self):
        self.assertTrue(source.is_kick("k138370155"))
        self.assertTrue(source.is_kick("kt1795452415"))
        self.assertFalse(source.is_kick("2925545033"))     # Twitch archive id
        self.assertTrue(source.is_kick("1794940195"))      # Kick stream start (what Forge sends)
        self.assertFalse(source.is_kick("555"))            # test ids stay Twitch
        self.assertEqual(source.num("k138370155"), 138370155)
        self.assertEqual(source.num("kt1795452415"), 1795452415)
        self.assertEqual(source.num("2925545033"), 2925545033)


class KickInfo(unittest.TestCase):
    def setUp(self):
        for k, v in (("KICK_SLUG", "chan"), ("KICK_CHANNEL_ID", "")):
            p = mock.patch.object(source, k, v)
            p.start()
            self.addCleanup(p.stop)

    def test_no_slug_refuses_before_any_request(self):
        g = fake_get(vids())
        with mock.patch.object(source, "KICK_SLUG", ""), self.assertRaises(RuntimeError):
            source.kick_info("k138370155", get=g)
        self.assertEqual(g.calls, [])

    def test_by_kick_id_live(self):
        g = fake_get(vids())
        inf = source.kick_info("k138370155", slug="chan", get=g)
        self.assertEqual(inf["created"], "2026-11-17T14:46:55Z")
        self.assertTrue(inf["recording"])
        self.assertEqual(inf["duration"], 0.0)
        self.assertEqual(inf["title"], "GTA 6 day 1")
        self.assertIn("/api/v2/channels/chan/videos", g.calls[0])

    def test_by_start_time_matches_the_nearest_stream_within_tolerance(self):
        started = source._unix("2026-11-17T14:47:30Z")      # official started_at, 35 s off the list's start_time
        inf = source.kick_info("kt%d" % started, get=fake_get(vids()))
        self.assertEqual(inf["kick_id"], "k138370155")
        self.assertEqual(inf["vod"], "kt%d" % started)       # the pipeline keeps ITS id
        self.assertEqual(source.kick_info(str(int(started)), get=fake_get(vids()))["kick_id"], "k138370155")   # plain digits

    def test_start_time_too_far_from_any_stream_refuses(self):
        far = source._unix("2026-11-17T12:00:00Z")
        with self.assertRaises(RuntimeError):
            source.kick_info("kt%d" % far, get=fake_get(vids()))

    def test_unknown_vod_refuses(self):
        with self.assertRaises(RuntimeError):
            source.kick_info("k999", get=fake_get(vids()))

    def test_ended_stream_has_its_length(self):
        inf = source.kick_info("k138324336", get=fake_get(vids()))
        self.assertFalse(inf["recording"])
        self.assertAlmostEqual(inf["duration"], 31899.0)

    def test_1080_rendition_and_audio_source(self):
        url, fps, inf = source.kick_rendition("k138370155", get=fake_get(vids()))
        self.assertTrue(url.endswith("/media/hls/1080p/playlist.m3u8"))
        self.assertEqual(fps, 30.0)
        self.assertTrue(source.kick_audio(inf).endswith("/160p30/playlist.m3u8"))

    def test_no_1080_refuses(self):
        def g(url, **kw):
            if "/api/v2/" in url:
                return json.dumps(vids()).encode()
            return "\n".join(l for l in MASTER_TEXT.splitlines() if "1920x1080" not in l and l != "1080p/playlist.m3u8").encode()
        with self.assertRaises(RuntimeError):
            source.kick_rendition("k138370155", get=g)


class ChannelGuard(unittest.TestCase):
    """17 Nov: KICK_SLUG once named a stranger's channel. With KICK_CHANNEL_ID set the slug must BE that channel."""

    def g(self, chan_id, video_channel=90000001):
        calls = []

        def get(url, **kw):
            calls.append(url)
            if url.endswith("/videos"):
                return json.dumps([dict(v, channel_id=video_channel) for v in vids()]).encode()
            if "/api/v2/channels/" in url:
                return json.dumps({"id": chan_id, "slug": "x"}).encode()
            return MASTER_TEXT.encode()
        get.calls = calls
        return get

    def setUp(self):
        source._checked.clear()
        for k, v in (("KICK_SLUG", "chan"), ("KICK_CHANNEL_ID", "90000001")):
            p = mock.patch.object(source, k, v)
            p.start()
            self.addCleanup(p.stop)

    def test_our_channel_reads(self):
        g = self.g(90000001)
        self.assertEqual(source.kick_info("k138370155", get=g)["kick_id"], "k138370155")
        source.kick_info("k138370155", get=g)
        self.assertEqual(sum(1 for c in g.calls if c.endswith("/channels/chan")), 1)   # checked once per run

    def test_a_strangers_channel_refuses_before_any_vod_is_read(self):
        g = self.g(111569384)
        with self.assertRaisesRegex(RuntimeError, "WRONG CHANNEL"):
            source.kick_info("k138370155", get=g)
        self.assertFalse(any(c.endswith("/videos") for c in g.calls))

    def test_a_renamed_away_slug_refuses(self):
        def get(url, **kw):
            raise RuntimeError("HTTP Error 404")
        with self.assertRaisesRegex(RuntimeError, "slug may have changed"):
            source.kick_info("k138370155", get=get)

    def test_videos_of_another_channel_refuse(self):
        with self.assertRaises(RuntimeError):
            source.kick_info("k138370155", get=self.g(90000001, video_channel=5))


class KickLive(unittest.TestCase):
    def test_live_and_offline(self):
        with mock.patch.object(source, "KICK_SLUG", "chan"), mock.patch.object(source, "KICK_CHANNEL_ID", ""):
            on = lambda url, **kw: json.dumps({"data": {"session_title": "GTA 6 DAY 1", "created_at": "2026-11-18T23:00:12.000000Z",
                                                        "thumbnail": {"src": "https://images.kick.com/t/720.webp"}}}).encode()
            k = source.kick_live(get=on)
            self.assertEqual((k["title"], k["thumb"]), ("GTA 6 DAY 1", "https://images.kick.com/t/720.webp"))
            self.assertIsNone(source.kick_live(get=lambda url, **kw: b'{"data": null}'))


class FetchOnKick(unittest.TestCase):
    def test_recording_comes_from_the_api_not_the_endlist(self):
        segs, rec = None, None
        with mock.patch.object(fetch, "_get", return_value=VARIANT.encode()):
            segs, rec = fetch.playlist("https://stream.kick.com/x/1080p/playlist.m3u8")
        self.assertEqual(len(segs), 3)
        self.assertAlmostEqual(segs[-1][0] + segs[-1][1], 28.5)
        self.assertFalse(rec)          # the playlist ALONE says "ended" while live: why fetch() asks the API on Kick
        self.assertTrue(segs[0][2].endswith("/1080p/0.ts"))

    def test_rendition_dispatches_on_the_id(self):
        with mock.patch.object(source, "kick_rendition", return_value=("u", 30.0, {"duration": 0.0})) as kr:
            self.assertEqual(fetch.rendition("k1"), ("u", 30.0, 0.0))
        kr.assert_called_once()


if __name__ == "__main__":
    unittest.main()

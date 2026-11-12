"""Fetch an exact stretch of a Twitch VOD at 1920x1080 from its HLS playlist, on any machine.

    python -m polished.fetch <vod> <from_s> <to_s> <out.ts>

Why not yt-dlp --download-sections: its cut point snaps to a keyframe it does not report, which is why
the stream-copy lane has to find the offset again by matching audio. Here the file is whole Twitch
segments, and the VOD second at the file's first sample comes from the segments' own timestamps
(PTS of the first segment we fetched minus PTS of segment 0). Measured on a 10.7 h VOD (2026-11-12):
PTS agrees with the playlist's summed #EXTINF to a constant 0.032 s from segment 1 to 3797, so the
mapping is exact; when the two ever disagree by more than DRIFT_MAX the job stops instead of guessing.

ARCHIVE (lane 3, the laptop backlog): `python -m polished.backlog pull <vod>` saves the VOD's own
segments + playlist to POLISHED_ARCHIVE/<vod>/ before Twitch expires it. When that folder exists,
fetch() reads the SAME segments from disk instead of the network: identical bytes, identical timing.

Hard rules (owner 2026-11-12, "everything 1080p"): the playlist must offer a 1920x1080 rendition and
the downloaded file must probe as 1920x1080, or nothing is fetched/used. No tokens are needed:
yt-dlp gets the public playback token itself.
"""
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0"}
DRIFT_MAX = 0.25        # s, PTS vs summed EXTINF (constant 0.032 measured)
PARALLEL = 8


def _get(url, rng=None, tries=5, timeout=60):
    for k in range(tries):
        try:
            h = dict(UA, **({"Range": rng} if rng else {}))
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return r.read()
        except Exception as ex:   # noqa: BLE001 - network: retry, then raise
            if k == tries - 1:
                raise RuntimeError(f"fetch failed after {tries} tries: {url.split('?')[0][-60:]}: {ex}")
            time.sleep(2 * (k + 1))


def rendition(vod):
    """The 1080 rendition's playlist URL + fps. Raises unless the VOD has a 1920x1080 rendition."""
    out = subprocess.run(["yt-dlp", "-J", "--no-warnings", f"https://www.twitch.tv/videos/{vod}"],
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode:
        raise RuntimeError(f"yt-dlp could not read VOD {vod}: {out.stderr[-300:]}")
    info = json.loads(out.stdout)
    full = [f for f in info.get("formats", []) if f.get("width") == 1920 and f.get("height") == 1080
            and "m3u8" in (f.get("protocol") or "")]
    if not full:
        have = sorted({f"{f.get('width')}x{f.get('height')}" for f in info.get("formats", []) if f.get("height")})
        raise RuntimeError(f"VOD {vod} has no 1920x1080 rendition (has {have}); refusing (owner: everything 1080p)")
    best = max(full, key=lambda f: f.get("fps") or 0)
    return best["url"], float(best.get("fps") or 0), float(info.get("duration") or 0)


def playlist(url):
    """[(start_s, dur_s, abs_uri)] from the summed #EXTINF, plus whether the VOD is still recording."""
    text = _get(url).decode("utf-8")
    base = url.split("?")[0].rsplit("/", 1)[0] + "/"
    lines = text.splitlines()
    segs, cum = [], 0.0
    for i, l in enumerate(lines):
        if l.startswith("#EXTINF:"):
            d = float(l[8:].split(",")[0])
            uri = lines[i + 1].strip()
            segs.append((round(cum, 3), d, uri if uri.startswith("http") else base + uri))
            cum += d
    if not segs:
        raise RuntimeError("empty playlist")
    return segs, "#EXT-X-ENDLIST" not in text


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=start_time,duration:stream=codec_type,width,height,avg_frame_rate",
                          "-of", "json", path], capture_output=True, text=True).stdout
    return json.loads(out or "{}")


def _start(p):
    return float(p.get("format", {}).get("start_time") or "nan")


def archived(vod):
    root = os.environ.get("POLISHED_ARCHIVE")
    d = os.path.join(root, str(vod)) if root else None
    if d and os.path.exists(os.path.join(d, "playlist.json")):
        return d
    return None


def fetch(vod, a, b, out, pad=2.0, url=None):
    """Whole segments covering [a - pad, b + pad] (VOD seconds) -> out (MPEG-TS). Returns
    {"path", "origin": VOD second at the file's t=0, "duration", "fps", "segments", "recording"}."""
    arch = archived(vod)
    if arch:
        pl = json.load(open(os.path.join(arch, "playlist.json"), encoding="utf-8"))
        segs = [(s, d, os.path.join(arch, "seg_%06d.ts" % i)) for i, (s, d) in enumerate(pl["segments"])]
        recording = False
    else:
        url = url or rendition(vod)[0]
        segs, recording = playlist(url)
    total = segs[-1][0] + segs[-1][1]
    if a >= total:
        raise RuntimeError(f"VOD {vod} has {total:.0f} s, asked from {a:.0f} s")
    pick = [i for i, (s, d, _) in enumerate(segs) if s + d > a - pad and s < b + pad]
    tmpd = out + ".parts"
    os.makedirs(tmpd, exist_ok=True)
    head = os.path.join(tmpd, "seg0_head.ts")            # PTS of segment 0 = the VOD's time origin
    if arch:
        shutil.copy(os.path.join(arch, "seg_000000.ts"), head)
    else:
        open(head, "wb").write(_get(segs[0][2], rng="bytes=0-399999"))
    t0 = _start(probe(head))

    def one(i):
        if arch:
            if not os.path.getsize(segs[i][2]):
                raise RuntimeError(f"archive segment {i} of {vod} is empty")
            return segs[i][2]
        p = os.path.join(tmpd, f"{i:06d}.ts")
        if not (os.path.exists(p) and os.path.getsize(p) > 0):
            data = _get(segs[i][2])
            open(p + ".tmp", "wb").write(data)
            os.replace(p + ".tmp", p)
        return p

    with cf.ThreadPoolExecutor(PARALLEL) as ex:
        parts = list(ex.map(one, pick))
    tmp = out + ".part"
    with open(tmp, "wb") as fo:
        for p in parts:
            with open(p, "rb") as fi:
                while True:
                    blk = fi.read(1 << 22)
                    if not blk:
                        break
                    fo.write(blk)
    pr = probe(tmp)
    v = [s for s in pr.get("streams", []) if s.get("codec_type") == "video"]
    if not v or (v[0].get("width"), v[0].get("height")) != (1920, 1080):
        raise RuntimeError(f"VOD {vod} {a:.0f}-{b:.0f}: file is {v[0].get('width') if v else '?'}x"
                           f"{v[0].get('height') if v else '?'}, not 1920x1080; refusing")
    origin = _start(pr) - t0
    want = segs[pick[0]][0]
    if not abs(origin - want) <= DRIFT_MAX:   # also catches NaN
        raise RuntimeError(f"VOD {vod}: timestamps say {origin:.3f} s, playlist says {want:.3f} s; "
                           "a timestamp jump in the VOD, refusing to guess")
    os.replace(tmp, out)
    for p in ([] if arch else parts) + [head]:
        os.remove(p)
    os.rmdir(tmpd)
    num, den = (v[0].get("avg_frame_rate") or "0/1").split("/")
    return {"path": out, "origin": round(origin, 3), "duration": float(pr["format"].get("duration") or 0),
            "fps": round(float(num) / float(den or 1), 3) if float(den or 1) else 0.0,
            "segments": [pick[0], pick[-1]], "recording": recording, "vod_total": round(total, 3)}


if __name__ == "__main__":
    vod, a, b, out = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
    print(json.dumps(fetch(vod, a, b, out)))

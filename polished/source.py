"""Where a VOD lives: Twitch or Kick.

* Kick, as the pipeline names it: the stream's START TIME in unix seconds, plain digits (e.g. 1794940195). That is
  what the Forge worker can know (Kick's official API gives started_at but no VOD id, and Cloudflare blocks Workers
  from Kick's public JSON), and plain digits pass every place that keeps only digits of a VOD id (Forge KV keys,
  Discord button ids). Told apart from Twitch by range: 1.6e9 <= id < 2e9 (unix time reaches 2e9 in May 2033;
  Twitch archive ids passed 2.9e9 by Nov 2026 and only grow).
* Kick, for probes/tests: "k<Kick VOD id>" (= its live_stream_id) or "kt<unix start>".
* Twitch: any other plain-digit id.

Kick pivot (owner 2026-11-17: the GTA 6 marathon streams on Kick only). The platform travels INSIDE the VOD id, so
code path picks the right source. Only the RUNNER asks Kick: it resolves the 1080p playlist and hands that URL to the
GPU hosts (conductor.Run.src_url), so Modal/Vast never meet Kick's Cloudflare. The one Kick setting is the channel
slug: env KICK_SLUG, from the KICK_SLUG secret (masked in public logs; identity stays out of this public repo).

    python -m polished.source info  <vod>                 title, start, length, live?, 1080p playlist, fps
    python -m polished.source probe [--slug S] [--vod V] [--from S --to S] [--out F]   reachability + a real fetch

Kick facts this relies on (checked 17 Nov 2026 on live channels):
* kick.com/api/v2/channels/<slug>/videos lists the channel's VODs, newest first. A stream that is ON AIR is already
  there: is_live true, duration 0, id = live_stream_id, source = an IVS master.m3u8 on stream.kick.com.
* The 1080p rendition is an EVENT playlist from segment 0 to ~40 s behind real time, so a growing VOD can be cut.
* Kick's playlist carries #EXT-X-ENDLIST even while live: "still recording" comes from the API, never the playlist.
* No audio-only rendition: audio comes from the smallest rendition (same AAC, tiny video).
* No auth; the JSON sits behind Cloudflare, which answered the laptop's urllib.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

KICK_SLUG = os.environ.get("KICK_SLUG") or ""   # THE Kick channel: the KICK_SLUG secret (identity stays out of this public repo)
KICK_CHANNEL_ID = os.environ.get("KICK_CHANNEL_ID") or ""   # the KICK_CHANNEL_ID secret: the slug MUST be this channel
KICK_API = "https://kick.com/api/v2/channels/%s/videos"
KICK_CHANNEL = "https://kick.com/api/v2/channels/%s"
KICK_CLIPS = "https://kick.com/api/v2/channels/%s/clips?sort=date&time=week"
_checked = set()
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36", "Accept": "application/json"}


KICK_START_MIN, KICK_START_MAX = 1_600_000_000, 2_000_000_000


def is_kick(vod):
    s = str(vod)
    return s.startswith("k") or (s.isdigit() and KICK_START_MIN <= int(s) < KICK_START_MAX)


def _by_start(vod):
    s = str(vod)
    return s.startswith("kt") or s.isdigit()


def num(vod):
    """The numeric part, for ordering Parts (Kick and Twitch never mix inside one series; kt = a start time, which
    orders streams the same way)."""
    return int(re.sub(r"^kt?", "", str(vod)) or 0)


KICK_VOD_CAP_S = 48 * 3600 - 300   # Kick keeps the first 48 h of a stream as the VOD; 5 min margin
START_TOL = 180.0   # s: Kick's official started_at vs the public list's start_time (same stream: a few s apart)


def _unix(iso):
    """Kick times: "2026-11-17 18:45:46" or "2026-11-17T02:37:22.190246Z" (UTC) -> unix seconds; unreadable = 0."""
    try:
        return datetime.fromisoformat(_utc_iso(iso).replace("Z", "+00:00")).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def _get(url, tries=4, timeout=30, headers=None):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or UA), timeout=timeout) as r:
                return r.read()
        except Exception as ex:   # noqa: BLE001 - network: retry gently, then raise
            if k == tries - 1:
                raise RuntimeError(f"fetch failed after {tries} tries: {url.split('?')[0][-70:]}: {ex}")
            time.sleep(3 * (k + 1))


# ------------------------------------------------------------------ Kick
def check_channel(slug, get=_get):
    """17 Nov: the slug once pointed at a STRANGER's channel (a name Leon did not own). With KICK_CHANNEL_ID set
    (every production workflow), the slug must resolve to that channel id, or nothing is read: never a silent wrong
    channel. Channel ids survive a rename; a rename makes this fail loudly until KICK_SLUG is updated."""
    if not KICK_CHANNEL_ID or slug in _checked:
        return
    try:
        d = json.loads(get(KICK_CHANNEL % slug))
    except Exception as e:   # noqa: BLE001 - 404 after a rename, Cloudflare, anything: refuse
        raise RuntimeError(f"Kick channel {slug!r} could not be read ({str(e)[:120]}); the slug may have changed "
                           f"(KICK_SLUG secret). Nothing read.")
    if str(d.get("id")) != str(KICK_CHANNEL_ID):
        raise RuntimeError(f"Kick slug {slug!r} is channel {d.get('id')}, not ours ({KICK_CHANNEL_ID}): WRONG CHANNEL, "
                           "refusing. Fix the KICK_SLUG secret.")
    _checked.add(slug)


def kick_videos(slug=None, get=_get):
    slug = slug or KICK_SLUG
    if not slug:
        raise RuntimeError("no Kick channel: set the KICK_SLUG secret. Nothing read.")
    check_channel(slug, get)
    d = json.loads(get(KICK_API % slug))
    if not isinstance(d, list):
        raise RuntimeError(f"Kick videos for {slug}: not a list ({str(d)[:120]})")
    if KICK_CHANNEL_ID:
        other = {str(v.get("channel_id")) for v in d} - {str(KICK_CHANNEL_ID)}
        if other:
            raise RuntimeError(f"Kick videos for {slug} include channel(s) {sorted(other)}, not ours: refusing")
    return d


def kick_entry(vod, slug=None, get=_get):
    want = num(vod)
    vids = kick_videos(slug, get)
    if _by_start(vod):   # by start time: the nearest stream within START_TOL
        near = [(abs(_unix(v.get("start_time") or v.get("created_at")) - want), i) for i, v in enumerate(vids)]
        near = [x for x in near if x[0] <= START_TOL]
        if near:
            return vids[min(near)[1]]
    for v in vids:
        if int(v.get("id") or 0) == want or int((v.get("video") or {}).get("live_stream_id") or 0) == want:
            return v
    raise RuntimeError(f"Kick VOD {vod} is not in {slug or KICK_SLUG}'s video list (deleted, private, or another channel)")


def _utc_iso(s):
    """Kick's "2026-11-17 18:45:46" (UTC) -> "2026-11-17T18:45:46Z"."""
    s = str(s or "").strip().replace(" ", "T")
    s = re.sub(r"\.\d+", "", s).rstrip("Z")
    return (s + "Z") if s else ""


def master_variants(text, base):
    """[{"url", "width", "height", "fps", "bw"}] from a master playlist."""
    out, lines = [], text.splitlines()
    for i, l in enumerate(lines):
        if not l.startswith("#EXT-X-STREAM-INF:"):
            continue
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", l)
        fr = re.search(r"FRAME-RATE=([\d.]+)", l)
        bw = re.search(r"BANDWIDTH=(\d+)", l)
        uri = next((x.strip() for x in lines[i + 1:] if x.strip() and not x.startswith("#")), "")
        out.append({"url": uri if uri.startswith("http") else base + uri,
                    "width": int(res.group(1)) if res else 0, "height": int(res.group(2)) if res else 0,
                    "fps": float(fr.group(1)) if fr else 0.0, "bw": int(bw.group(1)) if bw else 0})
    return out


def kick_live(slug=None, get=_get):
    """OUR channel's live stream right now: {"title", "thumb" (image URL or ""), "created" (unix)} or None (offline)."""
    slug = slug or KICK_SLUG
    if not slug:
        raise RuntimeError("no Kick channel: set the KICK_SLUG secret. Nothing read.")
    check_channel(slug, get)
    d = json.loads(get(KICK_CHANNEL % slug + "/livestream")).get("data")
    if not d:
        return None
    th = d.get("thumbnail") or {}
    return {"title": d.get("session_title") or "", "thumb": (th.get("src") or th.get("url") or "") if isinstance(th, dict) else "",
            "created": _unix(d.get("created_at"))}


def kick_clips(since_unix, slug=None, get=_get, pages=5):
    """Viewer clips of OUR channel created at or after since_unix, oldest first: [{"id", "title", "created" (unix),
    "duration", "url" (HLS playlist on clips.kick.com), "creator", "livestream_id"}]. Public ones only."""
    slug = slug or KICK_SLUG
    if not slug:
        raise RuntimeError("no Kick channel: set the KICK_SLUG secret. Nothing read.")
    check_channel(slug, get)
    out, cursor = [], ""
    for _ in range(pages):
        d = json.loads(get(KICK_CLIPS % slug + ("&cursor=" + cursor if cursor else "")))
        page = d.get("clips") or []
        for c in page:
            if KICK_CHANNEL_ID and str(c.get("channel_id")) != str(KICK_CHANNEL_ID):
                raise RuntimeError(f"Kick clips for {slug} include channel {c.get('channel_id')}, not ours: refusing")
            t = _unix(c.get("created_at"))
            if t >= since_unix and (c.get("privacy") or "public") == "public" and (c.get("clip_url") or c.get("video_url")):
                out.append({"id": str(c["id"]), "title": c.get("title") or "", "created": t, "duration": float(c.get("duration") or 0),
                            "url": c.get("clip_url") or c.get("video_url"), "creator": (c.get("creator") or {}).get("username") or "",
                            "livestream_id": str(c.get("livestream_id") or "")})
        cursor = str(d.get("nextCursor") or "")
        if not page or not cursor or min(_unix(c.get("created_at")) for c in page) < since_unix:
            break
    return sorted(out, key=lambda c: c["created"])


def kick_info(vod, slug=None, get=_get):
    v = kick_entry(vod, slug, get)
    master = v.get("source") or ""
    if not master:
        raise RuntimeError(f"Kick VOD {vod} has no source playlist yet")
    variants = master_variants(get(master).decode("utf-8"), master.split("?")[0].rsplit("/", 1)[0] + "/")
    return {"vod": str(vod), "kick_id": "k%s" % v.get("id"), "platform": "kick", "title": v.get("session_title") or "",
            "created": _utc_iso(v.get("start_time") or v.get("created_at")),
            "duration": float(v.get("duration") or 0) / 1000.0,      # ms; 0 while live
            # Kick saves only the FIRST 48 h of one stream as its VOD (boss 18 Nov): past that the VOD no longer grows,
            # so it counts as finished (its true length then reaches Forge) even while the stream itself goes on
            "recording": bool(v.get("is_live")) and time.time() - _unix(v.get("start_time") or v.get("created_at")) < KICK_VOD_CAP_S,
            "master": master, "variants": variants,
            "uuid": (v.get("video") or {}).get("uuid") or ""}


# ------------------------------------------------------------------ Twitch
def twitch_info(vod):
    from clipbot import common, episodes
    v = common.helix(f"videos?id={vod}")["data"][0]
    return {"vod": str(vod), "platform": "twitch", "title": v["title"], "created": v["created_at"],
            "duration": float(episodes._secs(v["duration"])), "recording": None}


# ------------------------------------------------------------------ both
def info(vod, get=_get):
    """{"vod", "platform", "title", "created" (ISO Z), "duration" (s; Kick: 0 while live), "recording" (Kick: bool,
    Twitch: None = read it from the playlist)}. Kick also gives "master" and "variants"."""
    return kick_info(vod, get=get) if is_kick(vod) else twitch_info(vod)


def pick_1080(variants):
    full = [f for f in variants if (f.get("width"), f.get("height")) == (1920, 1080)]
    return max(full, key=lambda f: f.get("fps") or 0) if full else None


def kick_rendition(vod, get=_get):
    """(1080p playlist url, fps, info) or raise (owner: everything 1080p)."""
    inf = kick_info(vod, get=get)
    best = pick_1080(inf["variants"])
    if not best:
        have = sorted({f"{f['width']}x{f['height']}" for f in inf["variants"]})
        raise RuntimeError(f"Kick VOD {vod} has no 1920x1080 rendition (has {have}); refusing (owner: everything 1080p)")
    return best["url"], best["fps"], inf


def kick_audio(inf):
    """The smallest rendition's playlist: the audio source (Kick has no audio-only rendition)."""
    vs = [f for f in inf["variants"] if f.get("height")]
    return min(vs, key=lambda f: (f["height"], f["bw"]))["url"] if vs else None


def audio_window(vod, lo, hi, out, max_s=0.0, ff="ffmpeg"):
    """Kick: the audio of [lo, hi] (VOD seconds; hi 0 = to the end) -> 16 kHz mono wav whose t=0 IS lo. Whole segments
    of the smallest rendition (Kick has no audio-only one), then an exact trim. Returns the wav's length in seconds."""
    import concurrent.futures as cf
    import subprocess
    from polished import fetch
    inf = kick_info(vod)
    segs = fetch.playlist(kick_audio(inf))[0]
    total = segs[-1][0] + segs[-1][1]
    hi = min(hi or total, total)
    if hi - lo < 1:
        raise RuntimeError(f"Kick VOD {vod} has {total:.0f} s, asked {lo:.0f}-{hi:.0f}")
    pick = [k for k, (s0, d, _) in enumerate(segs) if s0 + d > lo and s0 < hi]
    ts = out + ".src.ts"
    with cf.ThreadPoolExecutor(8) as ex:
        parts = list(ex.map(lambda k: fetch._get(segs[k][2]), pick))
    with open(ts, "wb") as f:
        for x in parts:
            f.write(x)
    want = (hi - lo) if not max_s else min(hi - lo, max_s)
    try:
        subprocess.run([ff, "-v", "error", "-y", "-i", ts, "-ss", "%.3f" % (lo - segs[pick[0]][0]), "-t", "%.3f" % want,
                        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out], check=True)
    finally:
        os.remove(ts)
    return want


def video_window(vod, a, b, out, ff="ffmpeg"):
    """Kick: exactly [a, b] (VOD seconds) of the 1080p rendition -> out (.mp4, re-encoded at the cut like yt-dlp's
    --force-keyframes-at-cuts). polished.fetch takes whole segments and knows the VOD second at the file's start."""
    import subprocess
    from polished import fetch
    ts = out + ".src.ts"
    f = fetch.fetch(vod, a, b, ts)
    try:
        # -ss AFTER -i (decode, then drop): an input seek into IVS segments can lose the video stream on short cuts
        # (17 Nov: a 1.5 s thumbnail cut came out audio-only); decoding <= one segment first costs a second at most
        subprocess.run([ff, "-v", "error", "-y", "-i", ts, "-ss", "%.3f" % (a - f["origin"]), "-t", "%.3f" % (b - a),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out], check=True)
    finally:
        os.remove(ts)
    return out


def page_url(vod, slug=None):
    """A URL yt-dlp understands, for the lanes that still cut with yt-dlp (rough episodes, Shorts, thumbnails)."""
    if not is_kick(vod):
        return f"https://www.twitch.tv/videos/{vod}"
    inf = kick_info(vod, slug)
    return f"https://kick.com/{slug or KICK_SLUG}/videos/{inf['uuid']}"


# ------------------------------------------------------------------ CLI
def _probe(a):
    rep = {"slug": a.slug, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "steps": []}

    def step(name, fn):
        t = time.time()
        try:
            r = fn()
            rep["steps"].append({"step": name, "ok": True, "s": round(time.time() - t, 2), "result": r})
            return r
        except Exception as e:   # noqa: BLE001 - a probe reports, never hides
            rep["steps"].append({"step": name, "ok": False, "s": round(time.time() - t, 2), "error": str(e)[:300]})
            return None

    vids = step("api: channel videos", lambda: [{"vod": "k%s" % v["id"], "live": v.get("is_live"),
                                                 "created": _utc_iso(v.get("start_time") or v.get("created_at")),
                                                 "h": round((v.get("duration") or 0) / 3.6e6, 2)} for v in kick_videos(a.slug)][:5])
    vod = a.vod or (vids[0]["vod"] if vids else "")
    if vod:
        r = step("api+master: 1080p rendition", lambda: (lambda u, f, i: {"url_host": u.split("/")[2], "fps": f, "live": i["recording"],
                                                                        "renditions": ["%dx%d@%g" % (x["width"], x["height"], x["fps"]) for x in i["variants"]]})(*kick_rendition(vod)))
        if r:
            def segs():
                from polished import fetch
                url, fps, inf = kick_rendition(vod)
                s, rec = fetch.playlist(url)
                return {"segments": len(s), "hours": round((s[-1][0] + s[-1][1]) / 3600, 3), "api_live": inf["recording"],
                        "playlist_says_recording": rec, "seg0_head_bytes": len(fetch._get(s[0][2], rng="bytes=0-399999"))}
            step("variant playlist + segment 0 head", segs)
            if a.hi > a.lo:
                def win():
                    from polished import fetch
                    f = fetch.fetch(vod, a.lo, a.hi, os.path.join(a.work, "probe.ts"))
                    os.remove(f["path"])
                    return {k: f[k] for k in ("origin", "duration", "fps", "segments", "recording", "vod_total")}
                step("polished.fetch window %g-%g" % (a.lo, a.hi), win)
    print(json.dumps(rep, indent=1))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1)
    return 0 if all(s["ok"] for s in rep["steps"]) and rep["steps"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("info")
    i.add_argument("vod")
    p = sub.add_parser("probe")
    p.add_argument("--slug", default="")
    p.add_argument("--vod", default="")
    p.add_argument("--from", dest="lo", type=float, default=0.0)
    p.add_argument("--to", dest="hi", type=float, default=0.0)
    p.add_argument("--work", default=".")
    p.add_argument("--out", default="")
    a = ap.parse_args(argv)
    if a.cmd == "info":
        print(json.dumps(info(a.vod), indent=1))
        return 0
    global KICK_SLUG
    a.slug = a.slug or KICK_SLUG
    KICK_SLUG = a.slug
    import polished.source as imported   # `python -m` runs a SECOND copy of this module: fetch.py uses the imported one
    imported.KICK_SLUG = a.slug
    imported.KICK_CHANNEL_ID = KICK_CHANNEL_ID
    return _probe(a)


if __name__ == "__main__":
    sys.exit(main())

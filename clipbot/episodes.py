"""Cut long-form episodes out of a Twitch VOD (GTA 6 marathon lane, plan: Documents\\_gta6-content\\PLAN.md).

    python -m clipbot.episodes --vod 2904099410 --out out [--from 0] [--to 14400] [--final]
                               [--series "GTA 6"] [--max-episodes 1] [--upload]

Runs in GitHub Actions. Nothing heavy ever runs on the owner's laptop.

What it does, for the window [--from, --to] of the VOD (the VOD can still be recording):
  1. audio of the window only (Audio_Only rendition) -> whisper, 30-min chunks
  2. a 160p copy of the window -> picture motion per second (cheap, ~100 MB an hour)
  3. HOLES = no speech for HOLE_MIN s AND a near-still picture (BRB / AFK / pause menu / starting
     screen). Only those are cut. Shorter pauses stay in, because he talks through nearly all of
     them (WDL rule, measured 2026-08-05: never delete speech to smooth an edit).
  4. episodes of TARGET kept minutes (MIN..MAX), each ending in a measured pause of >= PAUSE_MIN s
     (WDL: 1.5 s was not enough) or at a hole. The leftover tail is NOT emitted unless --final;
     the next window starts where the last emitted episode ended (printed as next_from).
  5. each episode: download its range at 1080p60, find the file's exact offset by matching its
     audio against the window audio, cut the kept spans WITHOUT re-encoding (stream copy; each cut
     is placed just before speech resumes so the keyframe snap lands inside the pause), join,
     check duration + decode of both ends.
  6. --upload: YouTube PRIVATE, NO publishAt, marker `clipbot:ep:<vod>:<start>` in the description.
     Approval (Discord) later sets the Part number, title, date and playlist. If the marker is
     already on the channel the episode is not uploaded again. If YouTube cannot be read, nothing
     is uploaded.

Writes out/episodes.json (the manifest the offer step sends to Discord).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

from . import common, legacy, render

HOLE_MIN = 100.0         # s of no speech before a stretch can be cut at all
MOTION_STILL = 1.6       # mean abs luma change per second (0-255 scale, 64x36) below which a frame pair is "still"
STILL_SHARE = 0.85       # share of still seconds a gap needs to count as a hole
HOLE_PAD = 1.5           # s of room kept either side of a cut hole
PAUSE_MIN = 3.0          # s, an episode boundary must sit in a pause at least this long
TARGET, MIN_LEN, MAX_LEN = 55 * 60, 40 * 60, 75 * 60
TAIL_MERGE = 15 * 60     # --final: a leftover shorter than this joins the previous episode
SLACK = 20.0             # s of extra video fetched either side of an episode range
LEAD = 0.3               # s: cut-in this long before speech resumes, cut-out this long after it stops
# A stream that never goes offline (the owner sleeps on a resting screen):
AWAY_MIN = 20 * 60       # s without one word = nobody is there; cut whatever the picture does (an animated screen is not "still")
SESSION_BREAK = 30 * 60  # s: a cut hole this long ends a session; what came before it is closed like the end of a stream
STUCK_WINDOW = 3 * 3600  # s: a window this long that yields nothing and would start again at the same second is closed too
QUIET_WORDS_PER_H = 100  # fewer words than this per hour = away, or a broken download (audio_problem tells which)

MARK = "clipbot:ep:"


def sh(cmd, **kw):
    return render.run(cmd, **kw)


def ytdlp(url, fmt, section, out):
    tmp = out + ".part.mp4"
    subprocess.run(["yt-dlp", "-q", "--no-progress", "-N", "8", "-f", fmt, "--download-sections", section,
                    "-o", tmp, url], check=True)
    shutil.move(tmp, out)


# ---------------- signals ----------------

def motion(path):
    """Mean absolute luma change between consecutive 1 fps frames, per second of the file."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", "fps=1,scale=64:36,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    f = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 36 * 64).astype(np.float32)
    if len(f) < 2:
        return []
    d = np.abs(np.diff(f, axis=0)).mean(axis=1)
    return [0.0] + [round(float(x), 2) for x in d]


def gaps(words, lo, hi, min_len):
    """Silences between words inside [lo, hi] (window edges count as speech)."""
    out, prev = [], lo
    for w in words:
        if w["s"] - prev >= min_len:
            out.append((prev, w["s"]))
        prev = max(prev, w["e"])
    if hi - prev >= min_len:
        out.append((prev, hi))
    return out


def holes(words, mot, lo, hi):
    """Stretches safe to cut: no speech for HOLE_MIN AND mostly still picture. mot is per second
    from lo. Returns [(a, b, stats)] in source seconds, padded inwards."""
    out = []
    for a, b in gaps(words, lo, hi, HOLE_MIN):
        seg = mot[int(a - lo):int(b - lo)]
        still = sum(1 for x in seg if x < MOTION_STILL) / max(1, len(seg))
        med = sorted(seg)[len(seg) // 2] if seg else 99
        keep_a = a + (0 if a <= lo + 0.01 else HOLE_PAD)
        keep_b = b - (0 if b >= hi - 0.01 else HOLE_PAD)
        # An animated starting/BRB scene is "still" only ~78% of seconds (2904919965: median 1.44, gameplay ~12),
        # so a near-still MEDIAN counts too.
        away = b - a >= AWAY_MIN
        cut = away or still >= STILL_SHARE or (med < MOTION_STILL and still >= 0.6)
        print(f"  gap {fmt(a)}-{fmt(b)} ({b - a:.0f}s) still {still:.0%} median motion {med}"
              f" -> {('CUT (nobody there)' if away and still < 0.6 else 'CUT') if cut else 'kept (picture moves)'}")
        if cut and keep_b - keep_a > 10:
            out.append((keep_a, keep_b, {"still": round(still, 2), "median_motion": med}))
    return out


def audio_problem(wav_seconds, lo, hi):
    """Why the window audio cannot be trusted, or ''. A short file is a broken download; silence is not."""
    if wav_seconds < (hi - lo) - 90:
        return f"audio covers {wav_seconds / 60:.0f} of {(hi - lo) / 60:.0f} min"
    return ""


def fmt(s):
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ---------------- planning ----------------

def kept_spans(lo, hi, hs):
    spans, cur = [], lo
    for a, b, _ in hs:
        if a > cur:
            spans.append((cur, a))
        cur = max(cur, b)
    if hi > cur:
        spans.append((cur, hi))
    return spans


def kept_between(spans, a, b):
    return sum(max(0.0, min(e, b) - max(s, a)) for s, e in spans)


def plan(words, spans, hs, lo, hi, final):
    """Episode ranges [(start, end)] in source seconds, and where the next window starts.

    A hole of SESSION_BREAK or more (asleep, away) ends a session: what was played before it is closed
    the way the end of a stream is. Without that, a short stretch before a long sleep was never long
    enough for an episode, so every later window started at the same second and nothing moved again."""
    eps, seg_lo = [], lo
    for a, b, _ in hs:
        if b - a < SESSION_BREAK:
            continue
        eps += _plan_part(words, spans, hs, seg_lo, a, True)[0]
        seg_lo = b
    got, start = _plan_part(words, spans, hs, seg_lo, hi, final)
    return eps + got, start


def _plan_part(words, spans, hs, lo, hi, final):
    """One session [lo, hi]. Boundaries: holes (best) or pauses >= PAUSE_MIN."""
    spans = [(max(a, lo), min(b, hi)) for a, b in spans if b > lo and a < hi]
    hs = [h for h in hs if h[0] >= lo and h[1] <= hi]
    words = [w for w in words if lo <= w["s"] < hi]
    # (this episode ends at, the next one starts at, quality). A pause boundary is ONE point,
    # 2.5 s after the last word (or LEAD before the next, if sooner): nothing between episodes is
    # dropped (a long silent drive stays in the next episode), and the next episode's keyframe snap
    # (<= 2 s back on Twitch) still lands after the last word of a PAUSE_MIN pause.
    cands = []
    for a, b, _ in hs:
        cands.append((a, b, 20.0))                   # a break is the best place to end an episode
    for a, b in gaps(words, lo, hi, PAUSE_MIN):
        if any(ha - 2 <= a and b <= hb + 2 for ha, hb, _ in hs):
            continue
        t = min(b - LEAD, a + 2.5)
        cands.append((t, t, min(b - a, 10.0)))
    cands.sort()
    eps, start = [], spans[0][0] if spans else lo
    while True:
        best = None
        for t, t_next, q in cands:
            if t <= start:
                continue
            k = kept_between(spans, start, t)
            if k < MIN_LEN:
                continue
            if k > MAX_LEN:
                break
            sc = q - abs(k - TARGET) / 180.0
            if best is None or sc > best[0]:
                best = (sc, t, t_next)
        if best is None:
            break
        eps.append((start, best[1]))
        start = best[2]
    left = kept_between(spans, start, hi)
    if final and left > 60:
        if eps and left < TAIL_MERGE and kept_between(spans, eps[-1][0], hi) <= MAX_LEN + TAIL_MERGE:
            eps[-1] = (eps[-1][0], hi)
        else:
            eps.append((start, hi))
        start = hi
    return eps, start


# ---------------- cutting ----------------

PROBES = (30.0, 60.0, 90.0)   # s into the downloaded file: three 30 s stretches of its first 120 s
AGREE = 0.05                  # s: probes that land within this of each other found the same spot
AGREE_MIN = 0.3               # a probe must match at least this well to count as agreeing


def offset_of(file_wav, win_wav, guess):
    """Exact position (window seconds) of the file's start, by cross-correlating its audio envelope
    against the window audio around the guess. Same VOD, same audio track, so the match is sharp
    wherever there is sound to match.

    Three stretches are tried, not one. Episode boundaries sit in pauses, so the first stretch is often
    near silence, where only the two encodes' noise is left to compare and the score drops though the
    offset is right (a real 11 Nov run: offset right to 0.02 s, score 0.49, run refused).
    Returns the best-scoring offset. When two or more probes land on the same offset, that agreement is
    the proof, and the score returned is the best of the agreeing ones."""
    import numpy as np
    sr = 1000                                         # 16 kHz / 16 = a 1 ms envelope
    base = max(0.0, guess)

    def env(x):
        n = len(x) // 16 * 16
        return np.abs(x[:n]).reshape(-1, 16).mean(axis=1)

    hits = []
    for p in PROBES:
        a = render.load_audio(file_wav, p, 30)        # file t = p .. p+30 ...
        b = render.load_audio(win_wav, base, p + 60)  # ... must sit in window [guess, guess+p+60] if the snap is < 30 s
        ea, eb = env(a), env(b)
        if len(ea) < sr or len(eb) < len(ea):
            continue
        ea = ea - ea.mean()
        eb = eb - eb.mean()
        n = len(eb) + len(ea)
        c = np.fft.irfft(np.fft.rfft(eb, n) * np.conj(np.fft.rfft(ea, n)), n)[:len(eb) - len(ea) + 1]
        k = int(np.argmax(c))
        peak = float(c[k] / (np.linalg.norm(ea) * np.linalg.norm(eb[k:k + len(ea)]) + 1e-9))
        hits.append((base + k / sr - p, peak))
    if not hits:
        raise RuntimeError("not enough audio to align")
    best = max(hits, key=lambda h: h[1])
    agree = [h for h in hits if h[1] >= AGREE_MIN and abs(h[0] - best[0]) <= AGREE]
    if best[1] < 0.5 and len(agree) < 2:
        for o, pk in hits:                            # two weaker probes that agree with each other beat one strong miss
            pair = [h for h in hits if h[1] >= AGREE_MIN and abs(h[0] - o) <= AGREE]
            if len(pair) >= 2:
                return o, max(0.5, max(h[1] for h in pair))
    if len(agree) >= 2:
        return best[0], max(0.5, best[1])
    return best


def keyframes(path, upto=None):
    out = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries",
              "frame=pts_time", "-of", "csv=p=0", *(["-read_intervals", f"%+{upto}"] if upto else []), path])
    return [float(x) for x in out.split() if x.strip() and x.strip() != "N/A"]


def cut_episode(url, win_wav, lo, spans, workdir, name):
    """Stream-copy the kept spans of one episode into workdir/name.mp4. Returns info dict."""
    a0, b0 = spans[0][0], spans[-1][1]
    src = os.path.join(workdir, f"{name}_src.mp4")
    s0 = max(0.0, a0 - SLACK)
    ytdlp(url, render.VOD_FORMAT, f"*{s0:.2f}-{b0 + SLACK:.2f}", src)
    w, h, fps = render.require_1080(src, f"{name} source")
    fwav = os.path.join(workdir, f"{name}_src.wav")
    sh(["ffmpeg", "-v", "error", "-y", "-i", src, "-t", "120", "-vn", "-ac", "1", "-ar", "16000", fwav])
    start, peak = offset_of(fwav, win_wav, s0 - lo)
    os.remove(fwav)
    if peak < 0.5:
        raise RuntimeError(f"{name}: could not align the video to the transcript (match {peak:.2f}); refusing to cut blind")
    start += lo                                       # source second at file t=0
    kf = keyframes(src, upto=60)
    gop = max((y - x for x, y in zip(kf, kf[1:])), default=0)
    print(f"  {name}: file starts at source {start:.2f}s (match {peak:.2f}), keyframe gap {gop:.2f}s, {w}x{h}@{fps}")
    pieces = []
    for i, (a, b) in enumerate(spans):
        p = os.path.join(workdir, f"{name}_{i:03d}.ts")
        sh(["ffmpeg", "-v", "error", "-y", "-ss", f"{a - start:.3f}", "-i", src, "-t", f"{b - a:.3f}",
            "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-avoid_negative_ts", "make_zero", "-f", "mpegts", p])
        pieces.append(p)
    lst = os.path.join(workdir, f"{name}_list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.writelines(f"file '{os.path.basename(p)}'\n" for p in pieces)
    out = os.path.join(workdir, f"{name}.mp4")
    tmp = out + ".tmp.mp4"
    sh(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
        "-movflags", "+faststart", tmp], cwd=workdir)
    for p in pieces + [lst, src]:
        os.remove(p)
    want = sum(b - a for a, b in spans)
    got = render.duration(tmp)
    if abs(got - want) > 2.5 * len(spans) + 3:
        raise RuntimeError(f"{name}: duration {got:.0f}s, expected ~{want:.0f}s; not shipping it")
    for ss in (["-t", "5"], ["-sseof", "-5"]):
        err = subprocess.run(["ffmpeg", "-v", "error", *(ss if ss[0] == "-sseof" else []), "-i", tmp,
                              *(ss if ss[0] == "-t" else []), "-f", "null", "-"], capture_output=True, text=True).stderr
        if err.strip():
            raise RuntimeError(f"{name}: decode errors at {'start' if ss[0] == '-t' else 'end'}: {err[:300]}")
    shutil.move(tmp, out)
    return {"file": out, "duration": round(got, 1), "expected": round(want, 1), "gop": round(gop, 2),
            "align": round(peak, 3), "pieces": len(spans)}


def join_preview(path, spans, out, around=6.0):
    """Small 480p reel of the first/last 8 s and +-around s of every join, for checking cuts by eye."""
    dur = render.duration(path)
    marks, acc = [], 0.0
    for a, b in spans[:-1]:
        acc += b - a
        marks.append(acc)
    pts = [(0.0, min(8.0, dur))] + [(max(0.0, m - around), min(dur, m + around)) for m in marks] + [(max(0.0, dur - 8), dur)]
    parts = []
    for k, (x, y) in enumerate(pts):
        p = f"{out}.{k}.mp4"
        sh(["ffmpeg", "-v", "error", "-y", "-ss", f"{x:.2f}", "-i", path, "-t", f"{y - x:.2f}", "-vf", "scale=-2:480,fps=30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "96k", p])
        parts.append(p)
    lst = out + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        f.writelines(f"file '{p}'\n" for p in parts)
    sh(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    for p in parts + [lst]:
        os.remove(p)
    return [round(m, 1) for m in marks]


# ---------------- metadata ----------------

BANNED = [
    (re.compile(r"(?i)kick\.com"), "kick.com link"),
    (re.compile(r"(?i)\b(language warning|contains swearing|strong language|swearing warning)\b"), "language warning"),
    (re.compile(r"(?i)\b\d+\s*(?:part|episode)s?\s+(?:in )?total\b|\ball\s+\d+\s+parts\b"), "total part count"),
    (re.compile("—"), "em dash"),
]
def footer():
    """Names come from the channel identity (common.IDENT, repo is public). A handle not in it is left out."""
    socials = [(n, common.IDENT.get(k)) for n, k in (("YouTube", "yt"), ("TikTok", "tiktok"), ("Instagram", "ig"))]
    lines = [f"{n}: @{h}" for n, h in socials if h]
    return f"WATCH ME LIVE\nTwitch: twitch.tv/{common.LOGIN}" + ("\n\nSOCIALS\n" + "\n".join(lines) if lines else "")


def episode_notes(vod, s, e):
    """Notes the crew marked with /moment in Discord during [s, e) (VOD seconds), oldest first."""
    try:
        ms = common.worker(f"/clipbot/moments?vod={vod}").get("moments", [])
    except Exception as ex:
        print(f"  moments unavailable ({str(ex)[:100]})")
        return []
    return [str(m.get("note", ""))[:120] for m in sorted(ms, key=lambda m: m.get("t", 0)) if s <= float(m.get("t", -1)) < e][:12]


def plain_dashes(text):
    """Writers love long dashes and the copy rules ban them. Swap them rather than lose the whole draft."""
    return re.sub(r" ?\u2014 ?", ", ", text).replace("\u2013", "-")


def episode_copy(series, text, stream_title, notes=()):
    """AI draft (worker /clipbot/epcopy): hook title + description. Fallback: plain text. Every
    word is shown on the Discord card and editable before anything is scheduled."""
    r = {}
    try:
        r = common.worker("/clipbot/epcopy", {"series": series, "stream_title": stream_title,
                                              "transcript": text[:6000], "notes": list(notes)})
    except Exception as e:
        print(f"  epcopy unavailable ({str(e)[:100]}), plain draft")
    hook = re.sub(r"\s+", " ", plain_dashes(str(r.get("hook", "")))).strip().strip('"')
    desc = plain_dashes(str(r.get("description", ""))).strip()
    hook = hook if 3 <= len(hook) <= 70 else ""
    desc = desc if 40 <= len(desc) <= 1500 else ""
    return {"hook": hook, "description": desc,
            "hashtags": [h for h in r.get("hashtags", []) if isinstance(h, str) and re.fullmatch(r"#[A-Za-z0-9_]{2,40}", h)][:5]
            if isinstance(r.get("hashtags"), list) else []}


def title_for(series, n, hook):
    """`GTA 6 Part 3 - Hook` + ' | <brand>' only if it all fits in 100. Never trimmed to fit."""
    base = f"{series} Part {n}" + (f" - {hook}" if hook else "")
    suffix = f" | {common.BRAND}"
    return base + suffix if len(base) + len(suffix) <= 100 else base


def description_for(desc, hashtags, marker):
    body = (desc.strip() + "\n\n" if desc.strip() else "") + footer()
    if hashtags:
        body += "\n\n" + " ".join(hashtags)
    body += f"\n\n{marker}"
    return body


def check_meta(title, description):
    probs = [why for rx, why in BANNED if rx.search(title) or rx.search(description)]
    try:
        legacy.check(title, description, where="episode")
    except RuntimeError as e:
        probs.append(str(e))
    if common.LOGIN.lower() not in description.lower():
        probs.append("no channel name in description")
    if len(title) > 100:
        probs.append(f"title {len(title)} chars")
    if len(description) > 5000:
        probs.append(f"description {len(description)} chars")
    return probs


def tags_for(series, n=None):
    s = series.lower()
    return [s, f"{s} playthrough", *( [f"{s} part {n}"] if n else []), f"{s} gameplay", f"{s} walkthrough", common.LOGIN]


# ---------------- upload ----------------

def upload_private(path, title, description, tags):
    """Private, no schedule. Approval sets the date later."""
    import urllib.request
    body = {"snippet": {"title": title[:100], "description": description[:5000], "tags": tags[:30], "categoryId": "20"},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
    size = os.path.getsize(path)
    init = urllib.request.Request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + common.yt_token(), "Content-Type": "application/json",
                 "User-Agent": "ClipBot/1.0",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
    with urllib.request.urlopen(init, timeout=60) as r:
        session = r.headers["Location"]
    return common.upload_session(session, path, size)


_ledger = None


def marker_on_channel(marker):
    """The video id already holding this episode, '' if none. Raises if either source cannot be read.

    Two sources, because each misses things: the uploads playlist shows only the newest 50 and can lag or
    omit a video, and the worker's list only knows episodes that got as far as a card. Uploading on one
    source alone is how a duplicate gets made, so a run that cannot read both stops instead."""
    global _ledger
    ups = common.recent_uploads(50)
    if not isinstance(ups, list):
        raise RuntimeError("could not read the channel")
    hit = common.find_marker(marker, ups)
    if hit:
        return hit
    if _ledger is None:
        got = common.worker("/clipbot/eplist")
        if not isinstance(got, dict) or not isinstance(got.get("items"), list):
            raise RuntimeError("could not read the worker's episode list")
        _ledger = got["items"]
    for it in _ledger:
        vid = it.get("video")
        if it.get("marker") != marker or not vid:
            continue
        live = common.yt("videos", part="snippet", id=vid).get("items", [])
        if live and live[0]["snippet"].get("channelId") == common.CHANNEL_ID:
            return vid                                # carded before and still on the channel
    return ""


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vod", required=True)
    ap.add_argument("--out", default="out")
    ap.add_argument("--from", dest="lo", type=float, default=0.0)
    ap.add_argument("--to", dest="hi", type=float, default=0.0, help="0 = end of the VOD")
    ap.add_argument("--final", action="store_true", help="the VOD has ended: emit the leftover tail too")
    ap.add_argument("--series", default="GTA 6")
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--max-episodes", type=int, default=0, help="test: only cut the first N episodes")
    ap.add_argument("--upload", action="store_true", help="upload each episode PRIVATE (no schedule)")
    ap.add_argument("--plan-only", action="store_true", help="stop after planning (no video download)")
    a = ap.parse_args()
    a.out = os.path.abspath(a.out)
    os.makedirs(a.out, exist_ok=True)
    print(f"runner: {runner_info()}")
    url = f"https://www.twitch.tv/videos/{a.vod}"
    v = common.helix(f"videos?id={a.vod}")["data"][0]
    total = _secs(v["duration"])
    lo, hi = a.lo, (a.hi or total)
    hi = min(hi, total)
    if hi - lo < MIN_LEN and not a.final:
        print(f"window {fmt(lo)}-{fmt(hi)} is shorter than one episode; nothing to do")
        json.dump({"vod": a.vod, "episodes": [], "next_from": lo}, open(os.path.join(a.out, "episodes.json"), "w"))
        return
    print(f"VOD {a.vod} '{v['title']}' {v['duration']}  window {fmt(lo)}-{fmt(hi)}{' FINAL' if a.final else ''}")

    win_wav = os.path.join(a.out, "window.wav")
    if not os.path.exists(win_wav):
        m4a = os.path.join(a.out, "window.m4a")
        ytdlp(url, "Audio_Only/bestaudio", f"*{lo:.2f}-{hi:.2f}", m4a)
        sh(["ffmpeg", "-v", "error", "-y", "-i", m4a, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", win_wav])
        os.remove(m4a)
    tpath = os.path.join(a.out, "window_words.json")
    if os.path.exists(tpath):
        words = json.load(open(tpath, encoding="utf-8"))
    else:
        words = render.transcribe(win_wav, model=a.model)
        json.dump(words, open(tpath, "w", encoding="utf-8"))
    words = [{**w, "s": w["s"] + lo, "e": w["e"] + lo} for w in words]
    print(f"{len(words)} words")
    bad = audio_problem(render.duration(win_wav), lo, hi)
    if bad:
        raise SystemExit(f"{bad}; the download looks broken, refusing to cut")
    quiet = len(words) < QUIET_WORDS_PER_H * (hi - lo) / 3600

    mpath = os.path.join(a.out, "window_motion.json")
    if os.path.exists(mpath):
        mot = json.load(open(mpath))
    else:
        low = os.path.join(a.out, "window_low.mp4")
        ytdlp(url, "160p/160p30/360p/worst", f"*{lo:.2f}-{hi:.2f}", low)
        mot = motion(low)
        os.remove(low)
        json.dump(mot, open(mpath, "w"))
    print(f"motion: {len(mot)} s, median {sorted(mot)[len(mot) // 2] if mot else '?'}")
    if abs(len(mot) - (hi - lo)) > 30:
        print(f"  ⚠ motion covers {len(mot)} s of a {hi - lo:.0f} s window")

    hs = holes(words, mot, lo, hi)
    spans_all = kept_spans(lo, hi, hs)
    eps, next_from = plan(words, spans_all, hs, lo, hi, a.final)
    if not eps and not a.final and next_from <= lo + 1 and hi - lo >= STUCK_WINDOW:
        print("  a full window with less than one episode in it: closing it so the next window can move on")
        eps, next_from = plan(words, spans_all, hs, lo, hi, True)
    if quiet:
        # Nearly no speech in the whole window. On a stream that never stops that is the owner asleep, so it is
        # cut as away and the lane moves on. He is told once per window, in case it was the audio instead.
        print(f"  only {len(words)} words in {(hi - lo) / 60:.0f} min: treated as away")
        try:
            common.worker("/clipbot/failed", {"id": f"episodes {a.vod} window {int(lo)}", "note": True, "error":
                          f"almost no speech between {fmt(lo)} and {fmt(hi)} of the stream ({len(words)} words). "
                          f"Treated as away: {len(eps)} episode(s) cut from it. If you were playing, tell Claude."})
        except Exception as ex:
            print(f"  could not tell the owner ({str(ex)[:80]})")
    cut_total = sum(b - a for a, b, _ in hs)
    print(f"{len(hs)} holes cut ({cut_total / 60:.1f} min), {len(eps)} episodes, next window from {fmt(next_from)}")
    for i, (s, e) in enumerate(eps, 1):
        sp = [(max(a, s), min(b, e)) for a, b in spans_all if b > s and a < e]
        print(f"  ep {i}: {fmt(s)}-{fmt(e)}  kept {sum(b - a for a, b in sp) / 60:.1f} min in {len(sp)} piece(s)")

    manifest = {"vod": a.vod, "vod_title": v["title"], "vod_created": v["created_at"], "series": a.series,
                "window": [lo, hi], "final": a.final, "next_from": next_from,
                "holes": [[round(x, 2), round(y, 2), st] for x, y, st in hs], "episodes": []}
    if a.plan_only:
        manifest["episodes"] = [{"start": s, "end": e} for s, e in eps]
        json.dump(manifest, open(os.path.join(a.out, "episodes.json"), "w", encoding="utf-8"), indent=1)
        return
    if a.upload:
        common.channel_gate()

    todo = eps[:a.max_episodes] if a.max_episodes else eps
    for i, (s, e) in enumerate(todo, 1):
        sp = [(max(x, s), min(y, e)) for x, y in spans_all if y > s and x < e]
        sp = [(x, y) for x, y in sp if y - x > 1]
        marker = f"{MARK}{a.vod}:{int(s)}"
        text = " ".join(w["w"] for w in words if s <= w["s"] < e)
        copy = episode_copy(a.series, text, v["title"], episode_notes(a.vod, s, e))
        title = title_for(a.series, "?", copy["hook"]).replace("Part ?", "(draft)")
        desc = description_for(copy["description"], copy["hashtags"], marker)
        probs = check_meta(title, desc)
        if probs:
            print(f"  ep {i}: draft copy broke rules {probs}; using the plain draft")
            copy = {"hook": "", "description": "", "hashtags": []}
            title = f"{a.series} (draft) {datetime.fromisoformat(v['created_at'][:19]).strftime('%d %b')} {fmt(s)}"
            desc = description_for("", [], marker)
        ep = {"i": i, "start": round(s, 2), "end": round(e, 2), "spans": [[round(x, 2), round(y, 2)] for x, y in sp],
              "kept": round(sum(y - x for x, y in sp), 1), "marker": marker, "hook": copy["hook"],
              "description": copy["description"], "hashtags": copy["hashtags"], "draft_title": title}
        if a.upload:
            have = marker_on_channel(marker)
            if have:
                print(f"  ep {i}: already on YouTube as {have}, not uploading again")
                ep["video"] = have
                manifest["episodes"].append(ep)
                continue
        info = cut_episode(url, win_wav, lo, sp, a.out, f"ep{i}")
        ep.update({k: info[k] for k in ("duration", "expected", "gop", "align", "pieces")})
        print(f"  ep {i}: {info}")
        ep["joins"] = join_preview(info["file"], sp, os.path.join(a.out, f"ep{i}_joins.mp4"))
        if a.upload:
            vid = upload_private(info["file"], title, desc, tags_for(a.series))
            ep["video"] = vid
            print(f"  ep {i}: uploaded PRIVATE https://youtu.be/{vid}")
            os.remove(info["file"])
        manifest["episodes"].append(ep)
        json.dump(manifest, open(os.path.join(a.out, "episodes.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    json.dump(manifest, open(os.path.join(a.out, "episodes.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("manifest written")


def runner_info():
    """CPU and transcriber versions. The same 4 h window gave 15,246 words on one runner and 15,933 on the
    next (same packages, same model), which moved an episode boundary; this line shows the cause next time."""
    cpu = ""
    try:
        cpu = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), "")
    except OSError:
        pass
    vers = []
    for mod in ("faster_whisper", "ctranslate2", "yt_dlp"):
        try:
            m = __import__(mod)
            vers.append(f"{mod} {getattr(m, '__version__', '') or getattr(getattr(m, 'version', None), '__version__', '?')}")
        except Exception:
            vers.append(f"{mod} ?")
    return f"{cpu or '?'} x{os.cpu_count()}; " + ", ".join(vers)


def _secs(d):
    m = re.match(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", d or "")
    return sum(int(x or 0) * k for x, k in zip(m.groups(), (3600, 60, 1)))


if __name__ == "__main__":
    sys.exit(main())

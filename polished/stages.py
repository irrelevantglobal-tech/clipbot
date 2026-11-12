"""The polished episode recipe, run on a GPU host (a Modal container or a rented Vast.ai box).

Same stages and settings as the recipe that made the published WD1/WD2/WDL episodes and passed the
owner's blind test on 2026-10-30 (Documents\\_gta6-content\\modal\\cloud_episode.py), split into two
STATELESS jobs so any GPU anywhere can run either one and nothing depends on a long-lived client:

  analyze_chunk(vod, lo, hi)   fetch [lo-OVERLAP, hi+OVERLAP] of the VOD at 1920x1080 (fetch.py),
                               audio map, 2 fps picture map, kit static screens, CLIP menu map,
                               whisper large-v3 (kit transcribe_chunked + clean_transcript).
                               Everything comes back in VOD SECONDS, owned range [lo, hi) only.
  render_episode(vod, plan)    fetch only the stretches the plan keeps, ONE encode (cold open ->
                               intro sting -> body -> outro), five data gates. Plan in VOD seconds.

Never runs whisper and NVENC at the same time (brain: that cost 3 episodes): whisper only runs in
analyze jobs, renders never transcribe.
"""
import json
import os
import shutil
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.environ.get("POLISHED_KIT") or os.path.join(HERE, "kit")
sys.path.insert(0, os.path.dirname(HERE))
from polished import fetch  # noqa: E402

FF, FP = "ffmpeg", "ffprobe"
HOP = 0.1            # audio map hop, seconds (same as the WD2 audio_map)
VIS_FPS = 2          # picture map rate
MENU_FPS = 1
OVERLAP = 20.0       # s fetched past each chunk edge so whisper has context there; words are owned by start time
CLUSTER_GAP = 300.0  # s: kept spans further apart than this are fetched as separate files (a long sleep is not downloaded)

# WD2 render contract (wd2_render_all.py): change nothing here without changing chapters.
INTRO_D, OUTRO_D, STING_TOL = 4.6, 20.0, 0.20
PRE_PAD, POST_PAD = 4.0, 1.0
NVENC = ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr",
         "-cq", "21", "-b:v", "12M", "-maxrate", "20M", "-bufsize", "24M",
         "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "120"]
# A host whose container cannot reach NVENC (some Vast hosts) still renders the same edit on the CPU.
X264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-maxrate", "20M", "-bufsize", "24M",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "120"]
AENC = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
LIMIT = "alimiter=level_in=1:level_out=1:limit=0.98:attack=5:release=50:level=disabled"
BATCH = 40


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def probe_dur(p, stream=None):
    cmd = [FP, "-v", "error"]
    cmd += (["-select_streams", stream, "-show_entries", "stream=duration"] if stream
            else ["-show_entries", "format=duration"])
    r = subprocess.run(cmd + ["-of", "csv=p=0", p], capture_output=True, text=True)
    try:
        return float(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return -1.0


def gpu_name():
    r = subprocess.run("nvidia-smi --query-gpu=name --format=csv,noheader", shell=True, capture_output=True, text=True)
    return r.stdout.strip() or "none"


_venc = None


def venc():
    """NVENC when this host can open it (one real 1080p frame), else x264. Printed, never silent."""
    global _venc
    if _venc is None:
        r = subprocess.run([FF, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=60:duration=0.5",
                            *NVENC, "-f", "null", "-"], capture_output=True, text=True)
        _venc = NVENC if r.returncode == 0 else X264
        print("encoder:", "h264_nvenc" if _venc is NVENC else "libx264 (NVENC unavailable: %s)" % r.stderr.strip()[-160:],
              flush=True)
    return _venc


# ------------------------------------------------------------------ analysis (one chunk)
def _pcm(src, ss, t):
    raw = subprocess.run([FF, "-v", "error", "-ss", "%.3f" % ss, "-i", src, "-t", "%.3f" % t, "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def audio_map(src, ss, t):
    """RMS dBFS, spectral flux and low-band energy per 0.1 s over file [ss, ss+t)."""
    x = _pcm(src, ss, t)
    reach = len(x) / 16000.0          # a decode that stops short is a failure, not a quiet ending
    n = int(16000 * HOP)
    ns = int(round(t / HOP))
    x = np.pad(x, (0, max(0, ns * n - len(x))))[:ns * n]
    rms, flux, low = [], [], []
    freqs = np.fft.rfftfreq(n, 1 / 16000)
    prev = None
    for k in range(0, ns, 6000):            # 10 min at a time: bounded memory on a 4 h window
        fr = x[k * n:min(ns, k + 6000) * n].reshape(-1, n)
        mag = np.abs(np.fft.rfft(fr * np.hanning(n), axis=1))
        rms.append(20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-9))
        d = np.diff(np.vstack([mag[:1] if prev is None else prev, mag]), axis=0)
        f = np.clip(d, 0, None).sum(1)
        if prev is None:
            f[0] = 0.0
        flux.append(f)
        low.append(10 * np.log10((mag[:, freqs < 200] ** 2).sum(1) + 1e-9))
        prev = mag[-1:]
    return ({"rms": np.round(np.concatenate(rms), 2).tolist(), "flux": np.round(np.concatenate(flux), 3).tolist(),
             "low": np.round(np.concatenate(low), 2).tolist()}, reach)


def picture_map(src, ss, t, t0):
    """Mean |diff| (HUD corners cropped), mean luma and p95 luma at 2 fps; rows in VOD seconds from t0.
    format=gray BEFORE the downscale, flags=area (brain: scale-then-gray read 6x too dark).
    SOFTWARE decode on purpose (benchmarked 10-30: 11.4 s vs 20.8 s per 300 s with -hwaccel cuda)."""
    w, h = 96, 54
    raw = subprocess.run([FF, "-v", "error", "-ss", "%.3f" % ss, "-i", src, "-t", "%.3f" % t, "-an",
                          "-vf", "fps=%d,format=gray,scale=%d:%d:flags=area" % (VIS_FPS, w, h),
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    f = np.frombuffer(raw, np.uint8).reshape(-1, h, w).astype(np.float32)
    core = f[:, 5:-5, 10:-10]
    diff = np.r_[99.0, np.abs(np.diff(core, axis=0)).mean((1, 2))]
    rows = [{"t": round(t0 + i / VIS_FPS, 2), "diff": round(float(diff[i]), 3), "luma": round(float(f[i].mean()), 2),
             "p95": round(float(np.percentile(f[i], 95)), 1)} for i in range(len(f))]
    return rows, len(f) / VIS_FPS


MENU_PROMPTS = [
    "a video game main menu screen with text buttons",
    "a video game inventory or loadout screen with item grids",
    "a video game character select lobby screen",
    "a video game loading screen",
    "a video game pause menu or settings screen",
    "a full screen video game map",
    "a matchmaking or results screen in a video game",
    "a black screen",
]
PLAY_PROMPTS = [
    "first-person shooter gameplay",
    "third-person gameplay of a character in a game world",
    "driving a car in a video game",
    "a cinematic cutscene in a video game",
    "a dark corridor in a video game",
    "a video game character fighting enemies",
]


def menu_map(src, ss, t, t0):
    """CLIP zero-shot P(menu) at 1 fps (every static/black detector misses ANIMATED menus)."""
    import open_clip
    import torch
    from PIL import Image
    w = h = 224
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model, _, pre = open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai", device=dev)
    tok = open_clip.get_tokenizer("ViT-B-32")
    rows = []
    with torch.no_grad():
        te = model.encode_text(tok(MENU_PROMPTS + PLAY_PROMPTS).to(dev))
        te = te / te.norm(dim=-1, keepdim=True)
        for k0 in range(0, int(np.ceil(t)), 600):          # 10 min of frames at a time (bounded memory)
            seg = min(600.0, t - k0)
            raw = subprocess.run([FF, "-v", "error", "-ss", "%.3f" % (ss + k0), "-i", src, "-t", "%.3f" % seg, "-an",
                                  "-vf", "fps=%d,scale=%d:%d:flags=area" % (MENU_FPS, w, h),
                                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
            fr = np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)
            for k in range(0, len(fr), 256):
                x = torch.stack([pre(Image.fromarray(f)) for f in fr[k:k + 256]]).to(dev)
                ie = model.encode_image(x)
                ie = ie / ie.norm(dim=-1, keepdim=True)
                p = (100.0 * ie @ te.T).softmax(-1)[:, :len(MENU_PROMPTS)].sum(-1).cpu().numpy()
                base = len(rows)
                rows += [{"t": round(t0 + (base + i) / MENU_FPS, 2), "menu": round(float(v), 3)} for i, v in enumerate(p)]
    del model
    if dev == "cuda":
        torch.cuda.empty_cache()
    return rows


def analyze_chunk(vod, lo, hi, work, url=None):
    """One chunk of a window. lo/hi in VOD seconds. Returns VOD-second maps for [lo, hi_eff)."""
    os.makedirs(work, exist_ok=True)
    t, t_all = {}, time.time()
    t0 = time.time()
    f = fetch.fetch(vod, max(0.0, lo - OVERLAP), hi + OVERLAP, os.path.join(work, "chunk.ts"), url=url)
    src, org = f["path"], f["origin"]
    t["fetch_s"] = round(time.time() - t0, 1)
    hi_eff = min(hi, org + f["duration"] - 0.5)
    if hi_eff - lo < 1.0:
        raise RuntimeError("chunk %.0f-%.0f: the VOD has nothing there yet" % (lo, hi))
    span = round(hi_eff - lo, 3)
    ss = lo - org
    t0 = time.time(); am, a_reach = audio_map(src, ss, span); t["audio_map_s"] = round(time.time() - t0, 1)
    t0 = time.time(); vis, v_reach = picture_map(src, ss, span, lo); t["picture_map_s"] = round(time.time() - t0, 1)
    for name, reach in (("audio_map", a_reach), ("visual", v_reach)):
        if span - reach > 6.0:
            raise RuntimeError("%s covers only %.1f of %.1f s - failed decode" % (name, reach, span))
    t0 = time.time()
    sj = os.path.join(work, "static.json")
    sh([sys.executable, os.path.join(KIT, "find_static_screens.py"), src, sj], stdout=subprocess.DEVNULL)
    st = json.load(open(sj, encoding="utf-8"))
    runs = [[max(lo, r[0] + org), min(hi_eff, r[1] + org)] + list(r[2:]) for r in st.get("runs", [])
            if r[1] + org > lo and r[0] + org < hi_eff]
    t["static_s"] = round(time.time() - t0, 1)
    t0 = time.time(); menu = menu_map(src, ss, span, lo); t["menu_map_s"] = round(time.time() - t0, 1)
    t0 = time.time()
    sh([sys.executable, os.path.join(KIT, "transcribe_chunked.py"), src, work, "ch", "600"])
    t["transcribe_s"] = round(time.time() - t0, 1)
    tj = os.path.join(work, "ch_transcript.json")
    tr = json.load(open(tj, encoding="utf-8"))
    if not tr.get("complete"):
        # A missing chunk reads as a 600 s pause: the best-scoring boundary in the VOD.
        raise RuntimeError("transcript incomplete, failed chunks %s" % tr.get("failed_chunks"))
    t0 = time.time()
    sh([sys.executable, os.path.join(KIT, "clean_transcript.py"), tj, src])
    t["clean_s"] = round(time.time() - t0, 1)
    tc = json.load(open(os.path.join(work, "ch_transcript_clean.json"), encoding="utf-8"))
    segs = []
    for sg in tc.get("segments", []):
        ws = [dict(w, s=round(w["s"] + org, 3), e=round(w["e"] + org, 3)) for w in (sg.get("words") or [])]
        ws = [w for w in ws if lo <= w["s"] < hi_eff]           # a word belongs to the chunk it STARTS in
        if not ws:
            continue
        segs.append({"s": ws[0]["s"], "e": ws[-1]["e"], "text": " ".join(w["w"].strip() for w in ws), "words": ws})
    t["total_s"] = round(time.time() - t_all, 1)
    os.remove(src)
    return {"vod": str(vod), "lo": lo, "hi": round(hi_eff, 3), "origin": org, "fetch": {k: f[k] for k in
            ("segments", "recording", "vod_total", "fps")}, "hop": HOP, "audio_map": am, "visual": vis,
            "static_runs": runs, "menu": menu, "segments": segs, "timings": t, "gpu": gpu_name()}


def merge_chunks(chunks):
    """Chunk results (VOD seconds) -> ONE window analysis in WINDOW seconds (t=0 at the first chunk's lo),
    the format plan_cut reads. Refuses a hole between chunks rather than inventing a pause there."""
    chunks = sorted(chunks, key=lambda c: c["lo"])
    base = chunks[0]["lo"]
    for x, y in zip(chunks, chunks[1:]):
        if abs(y["lo"] - x["hi"]) > 0.5:
            raise RuntimeError("chunks do not meet: %.1f -> %.1f" % (x["hi"], y["lo"]))
    am = {"hop": HOP, "rms": [], "flux": [], "low": []}
    vis, menu, static, segs = [], [], [], []
    for c in chunks:
        n = int(round((c["hi"] - c["lo"]) / HOP))
        for k in ("rms", "flux", "low"):
            arr = c["audio_map"][k][:n]
            am[k] += arr + [arr[-1] if arr else -90.0] * (n - len(arr))
        vis += [dict(v, t=round(v["t"] - base, 2)) for v in c["visual"]]
        menu += [dict(m, t=round(m["t"] - base, 2)) for m in c["menu"]]
        static += [[r[0] - base, r[1] - base] + list(r[2:]) for r in c["static_runs"]]
        for sg in c["segments"]:
            ws = [dict(w, s=round(w["s"] - base, 3), e=round(w["e"] - base, 3)) for w in sg["words"]]
            segs.append({"s": ws[0]["s"], "e": ws[-1]["e"], "text": sg["text"], "words": ws})
    merged = []   # a static run cut in two by a chunk edge is one run
    for r in sorted(static):
        if merged and r[0] - merged[-1][1] <= 1.0:
            merged[-1][1] = max(merged[-1][1], r[1])
        else:
            merged.append(list(r))
    return {"vod": chunks[0]["vod"], "base": base, "duration": round(chunks[-1]["hi"] - base, 3),
            "audio_map": am, "visual": vis, "menu": menu, "static": {"runs": merged},
            "transcript": {"segments": segs, "complete": True},
            "recording": chunks[-1]["fetch"]["recording"], "vod_total": chunks[-1]["fetch"]["vod_total"],
            "timings": [c["timings"] for c in chunks], "gpus": sorted({c["gpu"] for c in chunks})}


# --------------------------------------------------------------- render (one episode)
def _vf(i, a0=None, a1=None):
    f = "[%d:v]" % i
    if a0 is not None:
        f += "trim=start=%.4f:end=%.4f,setpts=PTS-STARTPTS," % (a0, a1)
    return (f + "fps=60,scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:-1:-1,setsar=1,format=yuv420p[v%d]" % i)


STING_I = -34.0   # set per episode by render_episode from the measured body (WD2 stream level = -34)


def lufs(path, s, e):
    """Integrated loudness of file [s, e) (ebur128), or None."""
    r = subprocess.run([FF, "-hide_banner", "-nostats", "-ss", "%.3f" % s, "-i", path, "-t", "%.3f" % (e - s), "-vn",
                        "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True)
    import re
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    return float(m[-1]) if m else None


def _af(i, a0=None, a1=None, norm=False, dur=None):
    f = "[%d:a]" % i
    if a0 is not None:
        f += "atrim=start=%.4f:end=%.4f,asetpts=PTS-STARTPTS," % (a0, a1)
    f += "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    if norm:   # stings matched DOWN to the stream level, never the body up (wd2 rule)
        f += ",loudnorm=I=%.1f:TP=-3:LRA=11" % STING_I
    if dur:    # 20 ms de-click at every seam
        f += ",afade=t=in:st=0:d=0.02,afade=t=out:st=%.3f:d=0.02" % max(dur - 0.02, 0)
    return f + "[a%d]" % i


def _batch_cmd(clips, out, container):
    """clips: (path, s, e, kind) in FILE seconds. kind 'seg' = seeked span, 'sting' = whole file."""
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-y"]
    fc, lab = [], []
    for i, (p, s, e, kind) in enumerate(clips):
        if kind == "seg":
            seek = max(0.0, s - PRE_PAD)
            cmd += ["-ss", "%.3f" % seek, "-t", "%.3f" % ((e - seek) + POST_PAD), "-i", p]
            fc.append(_vf(i, s - seek, e - seek))
            fc.append(_af(i, s - seek, e - seek, dur=e - s))
        else:
            cmd += ["-i", p]
            fc.append(_vf(i))
            fc.append(_af(i, norm=True))
        lab.append("[v%d][a%d]" % (i, i))
    fc.append("%sconcat=n=%d:v=1:a=1[vout][a0x]" % ("".join(lab), len(clips)))
    fc.append("[a0x]%s[aout]" % LIMIT)
    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]"]
    cmd += venc() + AENC + ["-fps_mode", "cfr", "-r", "60"]
    cmd += ["-f", "mpegts"] if container == "ts" else ["-movflags", "+faststart"]
    return cmd + [out]


def clusters(spans):
    """Group VOD spans into stretches worth one download each."""
    out = []
    for s, e in sorted(spans):
        if out and s - out[-1][1] < CLUSTER_GAP:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def render_episode(vod, plan, intro, outro, work, url=None):
    """plan: {"cold":[s,e], "keep":[[s,e],...]} in VOD seconds. -> (mp4, report)."""
    os.makedirs(work, exist_ok=True)
    t_all = time.time()
    for p, want, lab in ((intro, INTRO_D, "intro"), (outro, OUTRO_D, "outro")):
        d = probe_dur(p)
        if abs(d - want) > STING_TOL:   # every chapter would drift by the difference
            raise RuntimeError("%s is %.3f s, contract says %.2f" % (lab, d, want))
    spans = [tuple(map(float, x)) for x in plan["keep"]]
    cold = tuple(map(float, plan["cold"]))
    files = []
    t0 = time.time()
    url = url or fetch.rendition(vod)[0]
    for k, (a, b) in enumerate(clusters(spans + [cold])):
        f = fetch.fetch(vod, a - PRE_PAD - 1, b + POST_PAD + 1, os.path.join(work, "src%02d.ts" % k), url=url)
        files.append((a, b, f))
    t_fetch = time.time() - t0

    def where(s, e):
        for a, b, f in files:
            if a - 0.01 <= s and e <= b + 0.01:
                if s - f["origin"] < 0 or e - f["origin"] > f["duration"] + 0.05:
                    raise RuntimeError("span %.2f-%.2f outside its download" % (s, e))
                return f["path"], s - f["origin"], e - f["origin"]
        raise RuntimeError("span %.2f-%.2f in no download" % (s, e))

    # WD2 matched the stings to its raw VOD level (-34 LUFS). A quieter stream (Cyberpunk 11 Nov:
    # -38.6) made them jump out and failed gate 4 by +2 LU: match them DOWN to THIS body, never louder
    # than -34, never quieter than -42 (a near-silent body is not a level to copy).
    global STING_I
    big0 = max(files, key=lambda x: x[1] - x[0])
    body0 = [(s, e) for s, e in spans if big0[0] - 0.01 <= s and e <= big0[1] + 0.01]
    li = lufs(big0[2]["path"], body0[0][0] - big0[2]["origin"], body0[-1][1] - big0[2]["origin"]) if body0 else None
    STING_I = max(-42.0, min(-34.0, li)) if li is not None else -34.0
    clips = [where(*cold) + ("seg",), (intro, None, None, "sting")]
    clips += [where(s, e) + ("seg",) for s, e in spans] + [(outro, None, None, "sting")]
    expected = (cold[1] - cold[0]) + sum(e - s for s, e in spans) + INTRO_D + OUTRO_D
    out = os.path.join(work, "episode.mp4")
    tmp = out + ".part.mp4"
    t0 = time.time()
    if len(clips) <= BATCH:
        sh(_batch_cmd(clips, tmp, "mp4"))
    else:
        bdir = out + "_batches"
        shutil.rmtree(bdir, ignore_errors=True)
        os.makedirs(bdir)
        parts = []
        for k in range(0, len(clips), BATCH):
            p = os.path.join(bdir, "b%03d.ts" % (k // BATCH))
            sh(_batch_cmd(clips[k:k + BATCH], p, "ts"))
            parts.append(p)
        lst = os.path.join(bdir, "list.txt")
        open(lst, "w").write("".join("file '%s'\n" % p for p in parts))
        sh([FF, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", lst, "-c", "copy", "-movflags", "+faststart", tmp])
        shutil.rmtree(bdir, ignore_errors=True)
    t_render = time.time() - t0
    d = probe_dur(tmp)
    if d < 0 or abs(d - expected) > 1.0:
        raise RuntimeError("render is %.2f s, plan says %.2f - refusing to publish" % (d, expected))
    os.replace(tmp, out)
    # five data gates, loudness compared against the biggest download's kept body
    big = max(files, key=lambda x: x[1] - x[0])
    body = [(s, e) for s, e in spans if big[0] - 0.01 <= s and e <= big[1] + 0.01]
    t0 = time.time()
    g = gates(out, expected, big[2]["path"], (body[0][0] - big[2]["origin"], body[-1][1] - big[2]["origin"]))
    t_gates = time.time() - t0
    for _, _, f in files:
        os.remove(f["path"])
    return out, {"duration": d, "expected": expected, "gates": g, "gpu": gpu_name(),
                 "encoder": "h264_nvenc" if venc() is NVENC else "libx264", "downloads": len(files),
                 "body_lufs": li, "sting_lufs": STING_I,
                 "timings": {"fetch_s": round(t_fetch, 1), "render_s": round(t_render, 1), "gates_s": round(t_gates, 1),
                             "total_s": round(time.time() - t_all, 1)}}


def gates(out, expected, src, body_span):
    r = subprocess.run([sys.executable, os.path.join(KIT, "verify_render.py"), out,
                        "--expect", "%.3f" % expected, "--fps", "60", "--source", src,
                        "--src-span", "%.2f,%.2f" % body_span, "--lu-tolerance", "1.5",
                        "--jobs", "1"], capture_output=True, text=True)
    va = probe_dur(out, "v:0"), probe_dur(out, "a:0")
    # Stream-LENGTH difference, not drift: ~0.1 s accrues at the tail (published WD2 Part 1: 0.097 s).
    av_ok = va[0] > 0 and va[1] > 0 and abs(va[0] - va[1]) <= 0.15
    return {"verify_rc": r.returncode, "verify_log": (r.stdout + r.stderr)[-4000:],
            "av_sync": {"video_s": va[0], "audio_s": va[1], "ok": av_ok},
            "pass": r.returncode == 0 and av_ok}

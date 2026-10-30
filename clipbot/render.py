"""Vertical Short renderer.

Layout = the approved full-bleed 9:16 (centre crop of the 16:9 stream).

His stream burns its own karaoke captions into the picture (source y972-1037, x686-1233 on
1080p). Full-bleed puts those at output y1728-1844, UNDER the Shorts title/handle overlay,
and adding our own captions on top would print every word twice. So: detect them, mask the
band (the zone the Shorts UI covers anyway) and burn fresh karaoke higher up at MarginV 300.
That is the fix that worked on the OMEA clip Shorts (brain: sponsored-shorts TRAP 16).
"""
import math
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.normpath(os.path.join(HERE, "..", "fonts"))
PURPLE = "&H00FF4691&"            # #9146FF in ASS BGR
_model = {}

MASK = {"fuck": "f**k", "fucking": "f**king", "fucked": "f**ked", "fucker": "f**ker",
        "motherfucker": "m*therf**ker", "shit": "sh*t", "shitting": "sh*tting", "bullshit": "bullsh*t",
        "bitch": "b*tch", "cunt": "c**t", "dick": "d*ck", "twat": "tw*t", "wanker": "w*nker",
        "bastard": "b*stard", "prick": "pr*ck", "pussy": "p*ssy", "cock": "c*ck"}


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        raise RuntimeError(f"{cmd[0]} failed ({r.returncode}): {r.stderr[-800:]}")
    return r.stdout


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).strip())


def whisper(name, threads=0):
    if name not in _model:
        from faster_whisper import WhisperModel
        _model[name] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads or int(os.environ.get("CLIPBOT_THREADS", 0)) or os.cpu_count() or 2)
    return _model[name]


def load_audio(path, start=0.0, length=None):
    """16 kHz mono float32 via ffmpeg. faster-whisper's own decoder (PyAV) broke on the cloud
    runner when PyAV changed its API, so it is never used."""
    import numpy as np
    cut = (["-ss", str(start)] if start else []) + (["-t", str(length)] if length else [])
    raw = subprocess.run(["ffmpeg", "-v", "error", *cut, "-i", path, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


CHUNK = 1800   # 7 h of audio in one array (+ whisper/VAD copies) killed the 7 GB runner on 10-19; 30 min chunks keep RAM flat


def transcribe(path, model="small.en", vad=True):
    """Word-level transcript [{w,s,e}] of a media file, in CHUNK-second pieces."""
    total = duration(path)
    out, start = [], 0.0
    while start < total:
        segs, _ = whisper(model).transcribe(load_audio(path, start, CHUNK), language="en", word_timestamps=True,
                                            vad_filter=vad, condition_on_previous_text=False, beam_size=1)
        for s in segs:
            for w in (s.words or []):
                t = w.word.strip()
                if t:
                    out.append({"w": t, "s": round(float(w.start) + start, 2), "e": round(float(w.end) + start, 2)})
        start += CHUNK
        if total > CHUNK:
            print(f"  transcribed {min(start, total) / 60:.0f}/{total / 60:.0f} min, {len(out)} words", flush=True)
    return out


def burned_captions(src):
    """True if the stream's own captions are in this clip: near-white pixels in the
    caption band on a good share of frames."""
    w, h, n = 547, 65, 0
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-vf",
                          f"fps=2,scale=1920:1080,crop={w}:{h}:686:972,format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    frames = [raw[i:i + w * h] for i in range(0, len(raw) - w * h + 1, w * h)]
    for f in frames:
        if sum(1 for b in f if b >= 235) >= 250:
            n += 1
    return bool(frames) and n / len(frames) >= 0.15


def mask_word(w):
    core = re.sub(r"[^a-z']", "", w.lower())
    if core in MASK:
        return w.lower().replace(core, MASK[core])
    return w


def esc(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def ts(t):
    t = max(0.0, t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def build_ass(words, hook, dur, path):
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        "Style: Hook,Nunito Black,86,&H00FFFFFF,&H00FFFFFF,&H00101010,&H88000000,0,0,0,0,100,100,0,0,1,7,3,8,70,70,230,1",
        "Style: Cap,Nunito Black,80,&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,0,0,0,0,100,100,0,0,1,6,3,2,70,70,300,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook:
        lines.append(f"Dialogue: 1,{ts(0)},{ts(min(2.8, dur))},Hook,,0,0,0,,{{\\fad(120,200)}}{esc(hook.upper())}")
    # chunks of <=3 words, broken on pauses
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) == 3 or w["s"] - cur[-1]["e"] > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else dur
        end_chunk = min(ch[-1]["e"] + 0.35, nxt, dur)
        texts = [esc(mask_word(w["w"])).upper().rstrip(".,;:") or "…" for w in ch]
        for wi, w in enumerate(ch):
            st = w["s"]
            en = ch[wi + 1]["s"] if wi + 1 < len(ch) else end_chunk
            if en - st < 0.04:
                continue
            body = " ".join(f"{{\\c{PURPLE}}}{t}{{\\c&H00FFFFFF&}}" if j == wi else t for j, t in enumerate(texts))
            lines.append(f"Dialogue: 0,{ts(st)},{ts(en)},Cap,,0,0,0,,{body}")
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def render(src, out_final, out_preview, hook="", words=None, workdir=None):
    """src: a 16:9 clip. Writes the 1080x1920 final and a <=9 MB 540x960 Discord preview.
    Returns info dict."""
    workdir = workdir or os.path.dirname(os.path.abspath(out_final))
    dur = duration(src)
    if words is None:
        words = transcribe(src)
    masked = burned_captions(src)
    ass = os.path.join(workdir, os.path.basename(out_final) + ".ass")
    build_ass(words, hook, dur, ass)
    fonts = os.path.relpath(FONTS, workdir).replace("\\", "/")
    vf = "scale=-2:1920:flags=lanczos,crop=1080:1920"
    if masked:
        vf += ",drawbox=x=0:y=1688:w=iw:h=232:color=black:t=fill"
    vf += f",subtitles={os.path.basename(ass)}:fontsdir={fonts}"
    tmp = out_final + ".part.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-i", os.path.abspath(src), "-vf", vf, "-r", "60",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", os.path.basename(tmp)], cwd=workdir)
    if abs(duration(tmp) - dur) > 1.0:
        raise RuntimeError(f"render length {duration(tmp):.1f}s != source {dur:.1f}s")
    shutil.move(tmp, out_final)
    # Discord bot uploads cap at 10 MB; aim for ~8.5
    vbit = max(300_000, min(2_500_000, int(8.5 * 8 * 1024 * 1024 / max(dur, 1) - 96_000)))
    tmp = out_preview + ".part.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-i", out_final, "-vf", "scale=540:960", "-r", "30",
         "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(vbit), "-maxrate", str(vbit), "-bufsize", str(vbit * 2),
         "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", tmp])
    shutil.move(tmp, out_preview)
    return {"duration": round(dur, 2), "masked_burned_captions": masked, "words": len(words),
            "preview_mb": round(os.path.getsize(out_preview) / 1048576, 2)}


if __name__ == "__main__":
    # local test: py render.py <clip.mp4> <out_dir> [hook]
    src, od = sys.argv[1], sys.argv[2]
    os.makedirs(od, exist_ok=True)
    print(render(src, os.path.join(od, "final.mp4"), os.path.join(od, "preview.mp4"),
                 hook=sys.argv[3] if len(sys.argv) > 3 else ""))

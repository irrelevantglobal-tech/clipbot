"""Sleep skip: find the stretches of a window where the owner is asleep on the Resting screen and skip their GPU
analysis (24/7 marathon). CPU only: runs in the GitHub Actions job BEFORE any GPU is rented.

Owner 13 Nov: "is there a way it can pick up on it? like if it see's we're on the resting screen? and maybe
a manual way in the discord to see if its triggered and a button to hit it if it hasnt?"

    python -m polished.sleepskip probe --vod <id> --from <s> --to <s> [--presses presses.json] [--out report.json]
    python -m polished.sleepskip probe --video <file> [--audio <file>] ...        offline test on a local file
    python -m polished.sleepskip refs <out.npy> <image or video>...               build the Resting reference library

WHY IT IS SAFE (equivalence with the full GPU analysis, the rule that decides every threshold below):
the planner cuts any stretch of AWAY_MIN (20 min) with no word at all (plan_cut "away"), whatever the picture.
A skipped stretch is (a) at least MIN_SKIP long, (b) inside a run where EVERY picture sample is the Resting screen and
the CPU transcriber heard NO word, and (c) shrunk by MARGIN at both ends, so his leaving and coming back are always
analysed for real (an end that is the WINDOW's edge, with the Resting screen already there, needs no margin). The GPU would have found no words there either, so the planner cuts it the same way: the edit is
unchanged and only the GPU minutes are saved. Every doubt keeps the stretch (fail OPEN = scan): a sample that cannot be
fetched or decoded, a picture that is not clearly Resting, any word, playlists that disagree, any exception.

SIGNALS
* picture: the first keyframe of the 1080p segment at every STEP s (a Range request for the segment's head: the VOD
  has no low renditions), squashed to 224x224 exactly as stages.menu_map does, through the SAME CLIP ViT-B-32:
    - "scene": the stream-scene softmax of stages.SCENE_PROMPTS (starting soon / brb / ending / campfire / waiting);
    - "ref": cosine similarity to the Resting reference library (brand/resting_refs.npy: frames of every camp set);
    - manual stretch only: similarity to the picture at the moment someone pressed "asleep".
* speech: the Audio_Only rendition of each picture-candidate run through faster-whisper tiny.en (int8, VAD) on CPU.
  Any word with p >= WORD_P ends the run there (+-SPEECH_PAD). The Resting screen plays music, so loudness says nothing.
* manual (Discord "💤 He's asleep, skip" / "☀️ He's back, scan again", via the Forge): from an "asleep" press on, a
  sample also counts while the picture is the SAME as at the press (covers a Resting screen the library has never
  seen); the press-time picture is used only if it looks like a stream screen, never gameplay. It ENDS by itself at
  the first sample that differs or the first word, so a forgotten press can never lose gameplay. An "awake" press
  ends it too. Presses only ever widen the PICTURE rule: the no-word rule and the margins hold as for the automatic
  skip.
"""
import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REFS = os.path.join(HERE, "brand", "resting_refs.npy")
FF = os.environ.get("FFMPEG") or "ffmpeg"

STEP = 60.0              # s between picture samples
HEAD_BYTES = 1_500_000   # first keyframe of a 1080p60 Twitch segment (I-frame ~0.3-0.7 MB at 6.4 Mbps)
AWAY_MIN = 20 * 60.0     # plan_cut.AWAY_MIN: the planner's own "nobody there" rule
MARGIN = 180.0           # s analysed for real at both ends of every skip (him leaving / coming back)
MIN_SKIP = AWAY_MIN + 60.0   # a skip shorter than this is not worth the risk (and could fall outside an away cut)
# Calibrated 13 Nov on the laptop CPU (logic: tests/test_sleepskip.py): 993 real frames of 5 of the owner's VODs (gameplay,
# menus, BRB/starting/ending cards) score at most 0.80 against the 273-frame Resting library; the camp's Resting sets
# score 0.91-0.96 against it. A camp set NOT in the library scored 0.78-0.83 (too close to gameplay to call
# automatically): that case is what the 💤 button is for. The stream-scene score alone is NOT used for the automatic
# rule: the camp's Resting renders score 0.08-0.97 on it, and BRB/starting cards are not "clearly Resting".
REF_AUTO = 0.88          # similarity to a Resting reference frame that counts as Resting
PRESS_SIM = 0.95         # inside a 💤 stretch: same picture as at the press (one camp set vs itself: >= 0.974)
PRESS_SCENE_OK = 0.40    # the press-time picture is used only if it looks like a stream scene (gameplay: ~2.5% of
                         # real frames reach this) or matches the library at REF_MANUAL
REF_MANUAL = 0.83
WORD_P = 0.50            # a word this sure ends a run
SPEECH_PAD = 60.0        # s kept analysed either side of a word
PAR = 8


def log(*a):
    print("[sleepskip]", *a, flush=True)


# ------------------------------------------------------------------ sources
class TwitchSource:
    """VOD seconds -> frames (segment heads of the 1080p rendition) and audio (Audio_Only rendition)."""

    def __init__(self, vod):
        from polished import fetch
        self.fetch = fetch
        self.vod = str(vod)
        info = json.loads(subprocess.run(["yt-dlp", "-J", "--no-warnings", "https://www.twitch.tv/videos/%s" % vod],
                                         capture_output=True, text=True, encoding="utf-8", check=True).stdout)
        fm = info.get("formats", [])
        v = [f for f in fm if f.get("width") == 1920 and f.get("height") == 1080 and "m3u8" in (f.get("protocol") or "")]
        au = [f for f in fm if (f.get("format_id") or "").lower().startswith("audio") and "m3u8" in (f.get("protocol") or "")]
        if not v:
            raise RuntimeError("no 1080p rendition")
        self.vsegs, self.recording = fetch.playlist(max(v, key=lambda f: f.get("fps") or 0)["url"])
        self.asegs = fetch.playlist(au[0]["url"])[0] if au else None
        self.total = self.vsegs[-1][0] + self.vsegs[-1][1]
        if self.asegs:   # the audio playlist must keep the same clock, or audio times are a guess: no audio = no skip
            n = min(len(self.asegs), len(self.vsegs)) - 1
            if n < 0 or abs(self.asegs[n][0] - self.vsegs[n][0]) > 1.0:
                log("audio playlist clock disagrees with the video one: no audio, so nothing is skipped")
                self.asegs = None

    def frame(self, t, work):
        i = max(k for k, (s, d, _) in enumerate(self.vsegs) if s <= t) if t >= self.vsegs[0][0] else 0
        p = os.path.join(work, "h%06d.ts" % i)
        open(p, "wb").write(self.fetch._get(self.vsegs[i][2], rng="bytes=0-%d" % (HEAD_BYTES - 1)))
        try:
            return self.vsegs[i][0], decode_first(p)
        finally:
            os.remove(p)

    def audio(self, a, b, out):
        """Audio_Only segments covering [a, b] -> out; returns the VOD second at the file's t=0."""
        if not self.asegs:
            raise RuntimeError("no audio rendition")
        pick = [k for k, (s, d, _) in enumerate(self.asegs) if s + d > a and s < b]
        with cf.ThreadPoolExecutor(PAR) as ex:
            parts = list(ex.map(lambda k: self.fetch._get(self.asegs[k][2]), pick))
        with open(out, "wb") as f:
            for x in parts:
                f.write(x)
        return self.asegs[pick[0]][0]


class FileSource:
    """Offline tests: one local video (VOD second = file second + offset) and optionally a separate audio file."""

    def __init__(self, video, audio=None, offset=0.0):
        self.video, self.audio_path, self.offset = video, audio or video, offset
        self.total = offset + float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                                    "csv=p=0", video], capture_output=True, text=True).stdout.strip())
        self.recording = False

    def frame(self, t, work):
        raw = subprocess.run([FF, "-v", "error", "-ss", "%.3f" % (t - self.offset), "-i", self.video, "-frames:v", "1",
                              "-vf", "scale=224:224:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        if len(raw) != 224 * 224 * 3:
            raise RuntimeError("no frame at %.1f" % t)
        return t, np.frombuffer(raw, np.uint8).reshape(224, 224, 3)

    def audio(self, a, b, out):
        subprocess.run([FF, "-v", "error", "-y", "-ss", "%.3f" % (a - self.offset), "-i", self.audio_path, "-t",
                        "%.3f" % (b - a), "-vn", "-ac", "1", "-ar", "16000", out], check=True)
        return a


def decode_first(path):
    raw = subprocess.run([FF, "-v", "error", "-i", path, "-frames:v", "1", "-vf", "scale=224:224:flags=area",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    if len(raw) != 224 * 224 * 3:
        raise RuntimeError("segment head did not decode")
    return np.frombuffer(raw, np.uint8).reshape(224, 224, 3)


# ------------------------------------------------------------------ picture (CLIP, CPU)
class Clip:
    def __init__(self):
        import open_clip
        import torch
        from polished import stages
        torch.set_num_threads(max(1, os.cpu_count() or 1))
        self.torch = torch
        self.model, _, self.pre = open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai", device="cpu")
        self.model.eval()
        tok = open_clip.get_tokenizer("ViT-B-32")
        prompts = stages.MENU_PROMPTS + stages.PLAY_PROMPTS + stages.SCENE_PROMPTS
        self.n_mp = len(stages.MENU_PROMPTS) + len(stages.PLAY_PROMPTS)
        with torch.no_grad():
            te = self.model.encode_text(tok(prompts))
        self.te = te / te.norm(dim=-1, keepdim=True)

    def embed(self, frames):
        """RGB uint8 224x224 frames -> (unit image embeddings, stream-scene scores) as in stages.menu_map."""
        from PIL import Image
        out_e, out_s = [], []
        with self.torch.no_grad():
            for k in range(0, len(frames), 64):
                x = self.torch.stack([self.pre(Image.fromarray(f)) for f in frames[k:k + 64]])
                ie = self.model.encode_image(x)
                ie = ie / ie.norm(dim=-1, keepdim=True)
                lg = 100.0 * ie @ self.te.T
                out_s.append(lg.softmax(-1)[:, self.n_mp:].sum(-1).numpy())
                out_e.append(ie.numpy())
        if not out_e:
            return np.zeros((0, 512), np.float32), np.zeros(0, np.float32)
        return np.concatenate(out_e).astype(np.float32), np.concatenate(out_s).astype(np.float32)


def load_refs(path=REFS):
    if not os.path.exists(path):
        return np.zeros((0, 512), np.float32)
    r = np.load(path).astype(np.float32)
    return r / np.linalg.norm(r, axis=1, keepdims=True)


# ------------------------------------------------------------------ speech (faster-whisper, CPU)
def words_in(audio_path, origin):
    """-> sorted VOD-second times of every word with p >= WORD_P (tiny.en, int8, VAD on)."""
    from faster_whisper import WhisperModel
    raw = subprocess.run([FF, "-v", "error", "-i", audio_path, "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout     # ffmpeg, not PyAV (13 Nov: av API clash)
    x = np.frombuffer(raw, np.float32)
    if len(x) < 16000:
        raise RuntimeError("audio decoded to %.1f s" % (len(x) / 16000))
    m = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=max(1, os.cpu_count() or 1))
    segs, _ = m.transcribe(x, language="en", vad_filter=True, word_timestamps=True, beam_size=1,
                           condition_on_previous_text=False)
    out = []
    for sg in segs:
        for w in (sg.words or []):
            if (w.probability or 0.0) >= WORD_P and w.word.strip(" .,!?-'\""):
                out.append(origin + float(w.start))
    return sorted(out)


# ------------------------------------------------------------------ the decision
def press_mode(presses, t):
    """The latest press at or before t: ("asleep", t_press) / ("awake", t_press) / (None, None)."""
    last = None
    for p in presses:
        if p.get("mode") in ("asleep", "awake") and p["t"] <= t:
            last = p
    return (last["mode"], last["t"]) if last else (None, None)


def classify(ts, scene, ref, press_sim, presses, talked=None):
    """Per sample: (resting?, why). Auto rule, or the relaxed manual rule while an asleep press is live.
    A manual stretch ends for good at its first non-Resting-like sample or at talked[press] (the first word heard
    after the press), until the next press."""
    talked = talked or {}
    out, dead = [], set()        # dead: press times whose stretch already ended
    for i, t in enumerate(ts):
        mode, tp = press_mode(presses, t)
        if scene[i] is None:     # fetch/decode failed: fail open
            out.append((False, "no picture"))
            if mode == "asleep":
                dead.add(tp)
            continue
        if ref[i] >= REF_AUTO:
            out.append((True, "resting screen (ref %.2f)" % ref[i]))
            continue
        if mode == "asleep" and tp not in dead:
            if talked.get(tp) is not None and t >= talked[tp] - SPEECH_PAD:
                dead.add(tp)
                out.append((False, "💤 press ended: he talked"))
                continue
            if press_sim[i] is not None and press_sim[i] >= PRESS_SIM:
                out.append((True, "💤 pressed + same picture as at the press (%.3f)" % press_sim[i]))
                continue
            dead.add(tp)
            out.append((False, "💤 press ended: %s" % ("picture changed (%.3f vs the press)" % press_sim[i]
                                                       if press_sim[i] is not None else
                                                       "the picture at the press was not a Resting/stream screen")))
            continue
        out.append((False, "not resting (scene %.2f, ref %.2f)" % (scene[i], ref[i])))
    return out


def runs_of(ts, flags):
    """Consecutive Resting samples -> [[first_t, last_t]] (a run never reaches past its own samples)."""
    out, cur = [], None
    for t, f in zip(ts, flags):
        if f and cur is None:
            cur = [t, t]
        elif f:
            cur[1] = t
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def split_at_words(run, words):
    a, b = run
    out, cur = [], a
    for w in words:
        if a <= w <= b:
            if w - SPEECH_PAD > cur:
                out.append([cur, w - SPEECH_PAD])
            cur = max(cur, w + SPEECH_PAD)
    if b > cur:
        out.append([cur, b])
    return out


def decide(src, lo, hi, presses=(), refs=None, clip=None, work=None, transcribe=words_in):
    """-> report {"skips": [[a, b]], "samples": [...], "words": n, "notes": [...]} in VOD seconds."""
    t_all = time.time()
    work = work or tempfile.mkdtemp(prefix="sleepskip_")
    refs = load_refs() if refs is None else refs
    clip = clip or Clip()
    presses = sorted((dict(p, t=float(p["t"])) for p in presses if "t" in p), key=lambda p: p["t"])
    hi = min(hi, src.total)
    ts = [float(x) for x in np.arange(lo, hi - 1.0, STEP)]
    frames = [None] * len(ts)

    def grab(i):
        try:
            return i, src.frame(ts[i], work)[1]
        except Exception as e:   # noqa: BLE001 - fail open: this sample is "not resting"
            return i, ("err", str(e)[:80])
    with cf.ThreadPoolExecutor(PAR) as ex:
        for i, f in ex.map(grab, range(len(ts))):
            frames[i] = f
    ok = [i for i, f in enumerate(frames) if isinstance(f, np.ndarray)]
    emb, sc = clip.embed([frames[i] for i in ok])
    scene, ref, emb_all = [None] * len(ts), [0.0] * len(ts), [None] * len(ts)
    for j, i in enumerate(ok):
        scene[i] = float(sc[j])
        ref[i] = float((refs @ emb[j]).max()) if len(refs) else 0.0
        emb_all[i] = emb[j]
    # the picture at each "asleep" press (first sample at or after it, within 5 min), if it is not gameplay
    press_ref = {}
    for p in presses:
        if p.get("mode") != "asleep":
            continue
        k = next((i for i, t in enumerate(ts) if p["t"] <= t <= p["t"] + 300 and emb_all[i] is not None), None)
        if k is not None and (scene[k] >= PRESS_SCENE_OK or ref[k] >= REF_MANUAL):
            press_ref[p["t"]] = emb_all[k]
    press_sim = []
    for i, t in enumerate(ts):
        mode, tp = press_mode(presses, t)
        e = press_ref.get(tp) if mode == "asleep" else None
        press_sim.append(float(e @ emb_all[i]) if (e is not None and emb_all[i] is not None) else None)
    def candidates(cls):
        # A run that reaches a WINDOW edge is not him leaving or coming back (the picture is Resting at the edge
        # sample), so it is taken to the edge without a margin: a whole sleeping window then rents no GPU at all.
        # Its audio is still checked right up to the edge.
        out = []
        for r in runs_of(ts, [c[0] for c in cls]):
            e0, e1 = r[0] <= ts[0], r[1] >= ts[-1] and hi - r[1] <= STEP
            a, b = (lo if e0 else r[0]), (hi if e1 else r[1])
            if b - a >= MIN_SKIP + (0 if e0 else MARGIN) + (0 if e1 else MARGIN):
                out.append([a, b, e0, e1])
        return out

    cls = classify(ts, scene, ref, press_sim, presses)
    cand = candidates(cls)
    notes, n_words, all_words, heard = [], 0, [], []      # heard: [(a, b, words)] for the runs whose audio was read
    for a0, b0, e0, e1 in cand:
        try:
            ap = os.path.join(work, "a_%d.ts" % int(a0))
            org = src.audio(a0, b0, ap)
            ws = transcribe(ap, org)
            os.remove(ap)
        except Exception as e:   # noqa: BLE001 - no audio = no proof he is silent = keep it
            notes.append("run %.0f-%.0f kept: audio check failed (%s)" % (a0, b0, str(e)[:80]))
            continue
        heard.append((a0, b0, ws))
        n_words += len(ws)
        all_words += ws
        if ws:
            notes.append("run %.0f-%.0f: %d word(s) heard, first at %.0f" % (a0, b0, len(ws), ws[0]))
    # a 💤 press ends at the first word after it (owner: "auto-ends when the picture or audio says he's back"):
    # classify again with that, so a manual stretch never carries on past his voice (the automatic rule does, as
    # the planner would: a word is an island, the Resting screen around it is still away)
    talked = {}
    for p in presses:
        if p.get("mode") == "asleep":
            nxt = next((q["t"] for q in presses if q["t"] > p["t"]), None)
            w = next((x for x in sorted(all_words) if x >= p["t"] and (nxt is None or x < nxt)), None)
            if w is not None:
                talked[p["t"]] = w
    if talked:
        cls = classify(ts, scene, ref, press_sim, presses, talked)
        cand = candidates(cls)          # only ever shorter: every run lies inside a run whose audio was read
    skips = []
    for a0, b0, e0, e1 in cand:
        src_run = next((h for h in heard if h[0] <= a0 and b0 <= h[1]), None)
        if src_run is None:             # its audio was not read (failed above): keep it
            continue
        ws = [w for w in src_run[2] if a0 <= w <= b0]
        for x in split_at_words([a0, b0], ws):
            a = x[0] + (0.0 if (e0 and x[0] == a0) else MARGIN)
            b = x[1] - (0.0 if (e1 and x[1] == b0) else MARGIN)
            if b - a >= MIN_SKIP:
                skips.append([round(a, 3), round(b, 3)])
    told = []   # what happened to each press, for the status line
    for p in presses:
        if p.get("mode") == "asleep" and lo <= p["t"] < hi:
            nxt = next((q for q in presses if q["t"] > p["t"]), None)
            stop = next(((t, c[1]) for t, c in zip(ts, cls) if t >= p["t"] and "press ended" in c[1]
                         and (nxt is None or t < nxt["t"])), None)
            w = next((x for x in all_words if x >= p["t"] and (nxt is None or x < nxt["t"])), None)
            ends = [(stop[0], stop[1].split(": ", 1)[-1])] if stop else []
            ends += [(w, "he talked")] if w is not None else []
            ends += [(nxt["t"], "%s pressed %s" % (nxt.get("by") or "someone", nxt["mode"]))] if nxt and nxt["t"] < hi else []
            e = min(ends) if ends else None
            told.append({"t": p["t"], "by": p.get("by", ""), "end": None if e is None else round(e[0], 1),
                         "why": "" if e is None else e[1]})
    return {"window": [lo, hi], "skips": skips, "skipped_s": round(sum(b - a for a, b in skips), 1),
            "samples": [{"t": round(t, 1), "scene": None if s is None else round(s, 3), "ref": round(r, 3),
                         "press": None if q is None else round(q, 3), "resting": c[0], "why": c[1]}
                        for t, s, r, q, c in zip(ts, scene, ref, press_sim, cls)],
            "candidates": [c[:2] for c in cand], "words": n_words, "presses": told, "notes": notes, "refs": int(len(refs)),
            "cpu_s": round(time.time() - t_all, 1)}


# ------------------------------------------------------------------ synthetic analysis for a skipped stretch
def skipped_chunk(vod, a, b, fetch_info, hop=0.1, vis_fps=2, menu_fps=1):
    """A chunk result in stages.analyze_chunk's format for [a, b] that was NOT analysed: no words (the CPU check heard
    none), a still picture, the stream-scene score at 1.0 (it is the Resting screen). Marked "skipped" so nobody
    mistakes it for a measurement. The planner's away rule cuts all of it (see the module doc)."""
    n = int(round((b - a) / hop))
    nv = int((b - a) * vis_fps)
    nm = int((b - a) * menu_fps)
    return {"vod": str(vod), "lo": a, "hi": b, "origin": a, "fetch": dict(fetch_info), "hop": hop, "skipped": True,
            "audio_map": {"rms": [-60.0] * n, "flux": [0.0] * n, "low": [-60.0] * n},
            "visual": [{"t": round(a + i / vis_fps, 2), "diff": 1.0, "luma": 40.0, "p95": 120.0} for i in range(nv)],
            "static_runs": [], "menu": [{"t": round(a + i / menu_fps, 2), "menu": 0.0, "scene": 1.0} for i in range(nm)],
            "segments": [], "timings": {"total_s": 0.0, "skipped": True}, "gpu": "none (sleep skip)"}


def chunk_plan(lo, hi, skips, chunk):
    """Window [lo, hi] -> [(a, b, skipped)] covering it exactly: skips as single pieces, the rest in <= chunk pieces."""
    out, t = [], lo
    for a, b in sorted(skips) + [(hi, hi)]:
        a, b = max(lo, a), min(hi, b)
        while t < a - 1.0:
            out.append((round(t, 3), round(min(a, t + chunk), 3), False))
            t = min(a, t + chunk)
        if b > a:
            out.append((round(a, 3), round(b, 3), True))
            t = b
    return out


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("probe")
    p.add_argument("--vod")
    p.add_argument("--video")
    p.add_argument("--audio")
    p.add_argument("--from", dest="lo", type=float, default=0.0)
    p.add_argument("--to", dest="hi", type=float, default=0.0)
    p.add_argument("--presses", default="")
    p.add_argument("--refs", default=REFS)
    p.add_argument("--out", default="")
    r = sp.add_parser("refs")
    r.add_argument("out")
    r.add_argument("inputs", nargs="+")
    r.add_argument("--every", type=float, default=5.0, help="s between frames taken from a video")
    a = ap.parse_args()
    if a.cmd == "refs":
        clip, frames = Clip(), []
        for f in a.inputs:
            if f.lower().endswith((".png", ".jpg", ".jpeg")):
                from PIL import Image
                frames.append(np.asarray(Image.open(f).convert("RGB").resize((224, 224), Image.BOX)))
            else:
                fs = FileSource(f)
                for t in np.arange(0.5, fs.total - 0.5, a.every):
                    frames.append(fs.frame(t, None)[1])
        emb, sc = clip.embed(frames)
        np.save(a.out, emb.astype(np.float16))
        print("refs: %d frames -> %s (stream-scene score median %.2f, min %.2f)" % (len(frames), a.out,
              float(np.median(sc)), float(sc.min())))
        return 0
    src = TwitchSource(a.vod) if a.vod else FileSource(a.video, a.audio)
    presses = json.load(open(a.presses, encoding="utf-8")) if a.presses else []
    rep = decide(src, a.lo, a.hi or src.total, presses, refs=load_refs(a.refs))
    js = json.dumps(rep, indent=1, ensure_ascii=False)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(js)
    print(json.dumps({k: rep[k] for k in ("window", "skips", "skipped_s", "candidates", "words", "presses", "notes", "refs", "cpu_s")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

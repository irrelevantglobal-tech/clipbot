"""Find the 5 funniest moments in a Twitch VOD and render them as vertical Shorts.

    python -m clipbot.find_vod_clips --vod 2904099410 --out out
    python -m clipbot.find_vod_clips --vod 2904099410 --from 7200 --to 14400 --picks 3 --out out

--from/--to scan one window of the VOD (works on a VOD that is still recording), so a marathon gets
picks every couple of hours instead of 5 for the whole stream (Leon 2026-10-28: "3 picks every 2 h").
A window run's batch id is "<vod>-<from>"; clip start/end stay in whole-VOD seconds.

Signals (ported from the AC4 / Last Pirates miners, which were checked against Leon's picks):
  * reaction phrases, IMMEDIATE repetition, swearing, delivery rate (what he SAYS)
  * peak loudness in 0.5 s windows as a small bonus only. Energy never vetoes on its own:
    the quietest shot in Last Pirates was the funniest one.
  * whisper repetition loops and "thank you" card hallucinations are rejected
  * moments already clipped on Twitch are skipped (the clips lane uploads those)
Then an AI re-rank picks 5 of the top 15 and writes titles. If that is unavailable the
heuristic order is used. Leon approves every clip in Discord, so this is a shortlist.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from . import common, render

GAP = 1.2
MAX_WIN = 40.0
MERGE_GAP = 3.0
N_CAND = 15
N_PICK = 5
SPACING = 90.0
MIN_LEN, MAX_LEN = 12.0, 58.0

REACT = {
    "what the fuck": 6, "what the hell": 4, "oh my god": 5, "oh my days": 4, "no way": 4,
    "are you kidding": 5, "you're joking": 4, "jesus christ": 4, "what is happening": 5,
    "what just happened": 5, "i can't": 3, "why would": 3, "hold on": 3, "hold up": 3,
    "excuse me": 4, "you what": 3, "shut up": 3, "that's mad": 3, "i'm dead": 3,
    "did you see": 3, "oh dear": 3, "bro": 1.5, "dude": 1.5, "wait wait": 3, "oh no": 3,
    "are you serious": 4, "how did": 2, "what was that": 4, "help": 1.5, "run": 1,
}
SWEAR = re.compile(r"\b(fuck\w*|shit\w*|bastard|bollocks|twat|prick|dick\w*|arse\w*|wank\w*|cunt|bloody|piss\w*)\b", re.I)
WORD = re.compile(r"[a-z']+", re.I)
JUNK = re.compile(r"^(thank you\.?|thanks for watching!?|you|bye\.?)$", re.I)


def utterances(words):
    out, cur = [], []
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > GAP:
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def repetition(toks):
    best = 0
    for n in (1, 2):
        i = 0
        while i + n < len(toks):
            run = 1
            while toks[i:i + n] == toks[i + run * n:i + (run + 1) * n] and toks[i:i + n]:
                run += 1
            best = max(best, run if run > 1 else 0)
            i += 1
    return best


def score(u):
    text = " ".join(x["w"] for x in u)
    low = text.lower()
    toks = WORD.findall(low)
    if len(toks) < 4 or JUNK.match(low.strip()):
        return 0.0
    dur = max(u[-1]["e"] - u[0]["s"], 0.1)
    s = sum(low.count(p) * w for p, w in REACT.items())
    s += len(SWEAR.findall(low)) * 1.5
    rep = repetition(toks)
    if rep >= 3:
        s += (rep - 2) * 4
    rate = len(toks) / dur
    if rate > 3.2:
        s += (rate - 3.2) * 4
    if dur < 1.5:
        s *= 0.4
    return s


def is_loop(words):
    """Whisper repetition loop: one token dominating a LONG window. Real repetition
    ("what the fuck what the fuck") is short, loops run 30-60 s."""
    toks = [t for w in words for t in WORD.findall(w["w"].lower())]
    if len(toks) < 12:
        return False
    top = max(toks.count(t) for t in set(toks))
    return top / len(toks) > 0.45 and words[-1]["e"] - words[0]["s"] > 12


def peaks(wav, step=0.5):
    """Peak dBFS per 0.5 s block of a 16 kHz mono s16 wav."""
    import array
    import math
    with open(wav, "rb") as f:
        f.seek(44)
        a = array.array("h", f.read())
    n = int(16000 * step)
    out = []
    for i in range(0, len(a), n):
        blk = a[i:i + n]
        m = max(1, max(blk, default=0), -min(blk, default=0))
        out.append(20 * math.log10(m / 32768))
    return out


def windows(words, pk, dur, exclude):
    scored = []
    for u in utterances(words):
        sc = score(u)
        if sc > 0:
            scored.append({"s": u[0]["s"], "e": u[-1]["e"], "score": sc})
    merged = []
    for x in scored:
        if merged and x["s"] - merged[-1]["e"] <= MERGE_GAP and x["e"] - merged[-1]["s"] <= MAX_WIN:
            merged[-1]["e"] = x["e"]
            merged[-1]["score"] += x["score"]
        else:
            merged.append(dict(x))
    out = []
    for m in merged:
        if m["s"] < 480 or m["e"] > dur - 60:
            continue                                    # starting / ending screens
        if any(not (m["e"] < a - 5 or m["s"] > b + 5) for a, b in exclude):
            continue                                    # already a Twitch clip
        ws = [w for w in words if m["s"] <= w["s"] <= m["e"]]
        if is_loop(ws):
            continue
        blk = pk[int(m["s"] / 0.5):int(m["e"] / 0.5) + 1] or [-90]
        m["peak"] = round(max(blk), 1)
        m["score"] += max(0.0, (m["peak"] + 12) * 0.3)   # small bonus only, never a veto
        out.append(m)
    return out


def expand(m, words, dur):
    """Give the bit its setup and its reaction: start at the previous utterance if it is
    close (setup), end a beat after the last word, snap to pauses, clamp 12-58 s."""
    us = utterances(words)
    s, e = m["s"], m["e"]
    prev = [u for u in us if u[-1]["e"] <= s and s - u[0]["s"] <= 10]
    if prev:
        s = prev[-1][0]["s"]
    s = max(0.0, s - 1.2)
    e = min(dur, e + 2.5)
    if e - s < MIN_LEN:
        pad = (MIN_LEN - (e - s)) / 2
        s, e = max(0.0, s - pad), min(dur, e + pad)
    if e - s > MAX_LEN:
        # keep the densest-scoring 58 s
        best, bs = -1, s
        t = s
        while t + MAX_LEN <= e + 0.01:
            sc = sum(score(u) for u in us if u[0]["s"] >= t and u[-1]["e"] <= t + MAX_LEN)
            if sc > best:
                best, bs = sc, t
            t += 2
        s, e = bs, bs + MAX_LEN
    return round(s, 2), round(e, 2)


def text_between(words, s, e):
    return " ".join(w["w"] for w in words if s <= w["s"] <= e)


def rerank(cands, vod_title, n_pick=N_PICK):
    """Ask the worker's AI to pick n_pick and title them. Validated hard; any problem ->
    None and the heuristic order stands."""
    try:
        r = common.worker("/clipbot/rank", {"vod_title": vod_title, "n": n_pick,
                                            "candidates": [{"i": i, "text": c["text"][:900]} for i, c in enumerate(cands)],
                                            **common.who_fields()})
    except Exception as e:
        print(f"rerank unavailable ({str(e)[:120]}), using heuristic order")
        return None
    picks, seen = [], set()
    for p in r.get("picks", []):
        i = p.get("i")
        if isinstance(i, int) and 0 <= i < len(cands) and i not in seen:
            t = re.sub(r"\s+", " ", str(p.get("title", ""))).strip().strip('"')
            if not (3 <= len(t) <= 70):
                t = ""
            seen.add(i)
            picks.append((i, t))
    return picks[:n_pick] if len(picks) >= min(3, n_pick) else None


MO_MAX = 8   # helper moments cut per run, on top of the picks


def moments(vod, lo, dur):
    """Moments the crew marked with /moment in Discord (Leon 2026-10-29), window-relative.
    Leon's lane only; the worker never failing a run: no moments -> the run is exactly as before."""
    if common.WHO != "leon":
        return []
    try:
        r = common.worker(f"/clipbot/moments?vod={vod}")
    except Exception as e:
        print(f"moments unavailable ({str(e)[:100]})")
        return []
    out = []
    for m in r.get("moments", []):
        try:
            t = float(m.get("t", -1)) - lo
        except (TypeError, ValueError):
            continue
        if 0 <= t <= dur + 5:
            out.append({"t": min(t, dur), "note": re.sub(r"\s+", " ", str(m.get("note", ""))).strip()[:80]})
    return out


def moment_cands(marks, wins, words, dur, exclude):
    """One clip per marked moment. A helper types /moment just AFTER it happens, so look back 75 s:
    the best-scoring window there, else the 30 s before the mark. Skips moments a Twitch clip covers."""
    out = []
    for m in marks:
        t = m["t"]
        if any(x - 10 <= t <= y + 10 for x, y in exclude):
            continue
        near = [w for w in wins if w["e"] >= t - 75 and w["s"] <= t + 10]
        w = max(near, key=lambda w: w["score"]) if near else {"s": max(0.0, t - 30), "e": t, "score": 0.0, "peak": -90}
        s, e = expand(w, words, dur)
        if any(abs(s - c["s"]) < 20 for c in out):
            continue
        note = " ".join(render.mask_word(x) for x in m["note"].split())
        out.append({"s": s, "e": e, "score": round(w["score"], 1), "peak": w["peak"], "text": text_between(words, s, e),
                    "note": (note[:1].upper() + note[1:]) if 3 <= len(note) <= 70 else ""})
        if len(out) == MO_MAX:
            break
    return out


def quote_title(text):
    t = re.sub(r"\s+", " ", text).strip()
    words = t.split()
    t = " ".join(words[:9]) + ("..." if len(words) > 9 else "")
    return " ".join(render.mask_word(w) for w in t.split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vod", required=True)
    ap.add_argument("--out", default="out")
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--max-seconds", type=float, default=0, help="test: only use the first N seconds")
    ap.add_argument("--from", dest="lo", type=float, default=0.0, help="window start, VOD seconds")
    ap.add_argument("--to", dest="hi", type=float, default=0.0, help="window end, VOD seconds (0 = end of VOD)")
    ap.add_argument("--picks", type=int, default=N_PICK)
    a = ap.parse_args()
    windowed = a.lo > 0 or a.hi > 0
    batch = f"{a.vod}-{int(a.lo)}" if windowed else a.vod
    os.makedirs(a.out, exist_ok=True)
    url = f"https://www.twitch.tv/videos/{a.vod}"

    v = common.helix(f"videos?id={a.vod}")["data"][0]
    print(f"VOD {a.vod}  {v['duration']}  {v['created_at']}")
    if common.WHO != "leon" and v.get("user_id") != common.TWITCH_UID:
        raise SystemExit(f"VOD {a.vod} belongs to {v.get('user_login')}, not {common.LOGIN}. Nothing picked.")

    audio = os.path.join(a.out, "audio.m4a")
    wav = os.path.join(a.out, "audio.wav")
    if not os.path.exists(wav):
        section = ["--download-sections", f"*{a.lo:.2f}-{a.hi:.2f}" if a.hi else f"*{a.lo:.2f}-inf"] if windowed else []
        subprocess.run(["yt-dlp", "-q", "--no-progress", "-N", "8", "-f", "Audio_Only/bestaudio", *section, "-o", audio, url], check=True)
        cut = ["-t", str(a.max_seconds)] if a.max_seconds else []
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", audio, *cut, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], check=True)
        os.remove(audio)
    dur = render.duration(wav)                       # window-relative from here on; a.lo turns it back into VOD time
    print(f"audio {dur/60:.1f} min" + (f" (VOD {a.lo/3600:.2f} h to {(a.lo + dur)/3600:.2f} h)" if windowed else ""))

    tpath = os.path.join(a.out, "transcript.json")
    if os.path.exists(tpath):
        words = json.load(open(tpath, encoding="utf-8"))
    else:
        words = render.transcribe(wav, model=a.model)
        json.dump(words, open(tpath, "w", encoding="utf-8"))
    print(f"{len(words)} words transcribed")
    if len(words) < 200:
        raise SystemExit("transcript is nearly empty; refusing to pick clips from nothing")

    exclude = [(x - a.lo, y - a.lo) for x, y in common.vod_clips(a.vod) if y > a.lo and x < a.lo + dur]
    print(f"{len(exclude)} existing Twitch clips excluded")
    wins = windows(words, peaks(wav), dur, exclude)
    wins.sort(key=lambda w: -w["score"])
    cands = []
    for w in wins:
        s, e = expand(w, words, dur)
        if all(abs(s - c["s"]) > SPACING for c in cands):
            cands.append({"s": s, "e": e, "score": round(w["score"], 1), "peak": w["peak"],
                          "text": text_between(words, s, e)})
        if len(cands) == N_CAND:
            break
    marked = moment_cands(moments(a.vod, a.lo, dur), wins, words, dur, exclude)
    cands = [c for c in cands if all(abs(c["s"] - m["s"]) > SPACING for m in marked)]
    print(f"{len(wins)} scored windows -> {len(cands)} candidates, {len(marked)} marked moments")
    if not cands and not marked:
        raise SystemExit("no candidates")

    picks = (rerank(cands, v["title"], a.picks) or [(i, "") for i in range(min(a.picks, len(cands)))]) if cands else []
    chosen = [(cands[i], title) for i, title in picks] + [(m, m["note"]) for m in marked]
    chosen.sort(key=lambda p: p[0]["s"])

    clips = []
    for n, (c, title) in enumerate(chosen, 1):
        raw = os.path.join(a.out, f"raw{n}.mp4")
        tmp = raw + ".part.mp4"
        subprocess.run(["yt-dlp", "-q", "--no-progress", "-N", "8", "-f", "1080p60/1080p/best[height<=1080]",
                        "--download-sections", f"*{c['s'] + a.lo:.2f}-{c['e'] + a.lo:.2f}", "--force-keyframes-at-cuts",
                        "-o", tmp, url], check=True)
        shutil.move(tmp, raw)
        title = title or quote_title(c["text"])
        info = render.render(raw, os.path.join(a.out, f"clip{n}.mp4"), os.path.join(a.out, f"preview{n}.mp4"), hook=title)
        # raw{n}.mp4 stays in the artifact so a title edited in Discord can be re-rendered
        copy = common.clip_copy(title, stream_title=v["title"], transcript=c["text"])
        clips.append({"n": n, "start": round(c["s"] + a.lo, 2), "end": round(c["e"] + a.lo, 2), "title": title, "score": c["score"],
                      "quote": c["text"][:300], **({"marked": True} if "note" in c else {}), **copy, **info})
        print(f"clip {n}: {c['s'] + a.lo:.0f}-{c['e'] + a.lo:.0f}s score {c['score']} -> {info}")

    json.dump({"vod": a.vod, "batch": batch, "window": [a.lo, a.lo + dur] if windowed else None, "kind": "vod",
               "vod_title": v["title"], "vod_created": v["created_at"], "clips": clips},
              open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.remove(wav)
    print("manifest written")


if __name__ == "__main__":
    sys.exit(main())

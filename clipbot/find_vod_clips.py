"""Find the 5 funniest moments in a Twitch VOD and render them as vertical Shorts.

    python -m clipbot.find_vod_clips --vod 2904099410 --out out
    python -m clipbot.find_vod_clips --vod 2904099410 --from 7200 --to 14400 --picks 3 --out out

--from/--to scan one window of the VOD (works on a VOD that is still recording), so a marathon gets
picks every couple of hours instead of 5 for the whole stream (Owner 2026-10-28: "3 picks every 2 h").
A window run's batch id is "<vod>-<from>"; clip start/end stay in whole-VOD seconds.

Signals (ported from the AC4 / Last Pirates miners, which were checked against the owner's picks):
  * reaction phrases, IMMEDIATE repetition, swearing, delivery rate (what he SAYS)
  * peak loudness in 0.5 s windows as a small bonus only. Energy never vetoes on its own:
    the quietest shot in Last Pirates was the funniest one.
  * whisper repetition loops and "thank you" card hallucinations are rejected
  * moments already clipped on Twitch are skipped (the clips lane uploads those)
Then (picker v2, 2026-11-12, after the owner rejected 0 of 24 cards) the top 12 are re-heard with a better model and
an AI judge (judge.py) scores each one; only those that clear the bar are offered, so a window gives 0 to --picks
clips, cut on phrase boundaries from setup to payoff (snap). If the judge is down, the old AI re-rank runs over the
candidates above a heuristic floor instead. Every cut is checked against its own audio before it is offered
(aligned): footage that does not say what the transcript said is never sent. The owner approves every clip in Discord.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from . import chat, common, judge, render

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
    # GTA 6 (marathon from 18 Nov): deaths, arrests, cops and carnage are the moments
    "wasted": 4, "busted": 4, "mission failed": 3, "five star": 3, "5 star": 3, "wanted level": 2,
    "the cops": 2, "police": 1.5, "ran me over": 4, "run him over": 3, "ran him over": 3, "blew up": 3,
    "exploded": 3, "crashed": 2, "i'm dying": 3, "i'm crying": 3,
}
LAUGH = re.compile(r"\b(ha(ha)+|(ha ){2,}ha|lol|lmao)\b", re.I)
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
    s += min(3, len(LAUGH.findall(low))) * 2
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


def windows(words, pk, dur, exclude, base=0.0, tail=True):
    """Scored, merged stretches. base = VOD second of window second 0; tail = this window runs to the stream's end.
    The starting/ending-screen guards apply to the STREAM's first 8 min and last minute only: on 2 h windows they
    used to throw away the first 8 min and last minute of every window (2026-11-12)."""
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
        if m["s"] + base < 480 or (tail and m["e"] > dur - 60):
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


PHRASE_GAP = 0.35     # a pause this long between words is a phrase boundary
CUT_MIN, CUT_FLOOR = 14.0, 8.0


def _phrase_start(ws, i, limit=8.0):
    """Index of the first word of the phrase holding ws[i]; at most `limit` s back (then the widest pause in reach)."""
    k, best, best_gap = i, i, -1.0
    while k > 0 and ws[i]["s"] - ws[k - 1]["s"] <= limit:
        gap = ws[k]["s"] - ws[k - 1]["e"]
        if gap >= PHRASE_GAP:
            return k
        if gap > best_gap:
            best, best_gap = k, gap
        k -= 1
    return k if k == 0 else best


def snap(words, s, e, dur):
    """Cut on speech boundaries (Owner's rejects 11/12 Nov: lines cut mid-sentence, single lines padded to 12 s with
    silence). Start at the beginning of the phrase holding `s`; end once the phrase or laugh holding `e` is over, never
    inside the next word; 14-58 s, but a moment that is naturally 8-14 s long stays short rather than padded."""
    ws = sorted((w for w in words if w["e"] > w["s"] >= 0), key=lambda w: w["s"])
    if not ws:
        return round(max(0.0, s), 2), round(min(dur, e), 2)
    i = next((k for k, w in enumerate(ws) if w["s"] >= s - 0.5), len(ws) - 1)
    i = _phrase_start(ws, i)
    j = max([k for k, w in enumerate(ws) if w["e"] <= e + 0.5] or [i])
    j = max(i, j)
    while j + 1 < len(ws) and ws[j + 1]["s"] - ws[j]["e"] < 0.6 and ws[j + 1]["e"] - e < 6:
        j += 1                                       # finish the sentence / laugh
    # too short: add whole phrases before (setup), then after
    while ws[j]["e"] - ws[i]["s"] < CUT_MIN and i > 0 and ws[j]["e"] - ws[i - 1]["s"] <= MAX_LEN - 2 \
            and ws[i]["s"] - ws[i - 1]["e"] < 4.0:
        i = _phrase_start(ws, i - 1)
    while ws[j]["e"] - ws[i]["s"] < CUT_MIN and j + 1 < len(ws) and ws[j + 1]["s"] - ws[j]["e"] < 2.0:
        j += 1
    start = ws[i]["s"] - 0.4
    if i > 0:
        start = max(start, ws[i - 1]["e"] + 0.05)
    end = ws[j]["e"] + 0.8
    if j + 1 < len(ws):
        end = min(end, ws[j + 1]["s"] - 0.05)
    start, end = max(0.0, start), min(dur, end)
    if end - start > MAX_LEN:                        # keep the payoff, drop the oldest setup, start on a phrase
        ks = [k for k in range(i + 1, j + 1) if end - (ws[k]["s"] - 0.4) <= MAX_LEN]
        heads = [k for k in ks if ws[k]["s"] - ws[k - 1]["e"] >= PHRASE_GAP]
        if ks:
            k = (heads or ks)[0]
            start = max(ws[k]["s"] - 0.4, ws[k - 1]["e"] + 0.05)
        else:
            start = end - MAX_LEN
    if end - start < CUT_FLOOR:                      # pad only up to the floor, mostly after (the reaction)
        pad = CUT_FLOOR - (end - start)
        start, end = max(0.0, start - pad * 0.3), min(dur, end + pad * 0.7)
    return round(start, 2), round(end, 2)


def tokens(words):
    return {t for w in words for t in WORD.findall(w["w"].lower()) if len(t) >= 3}


def aligned(expected, got, need=0.3):
    """Does the downloaded clip say what the transcript said at that time? On 4-12 Nov every first window of a live
    stream cut footage from hours 0-2 under a transcript of hours 2-4 (the download recorded from the live edge).
    Share of the transcript's words heard again in the clip; measured 0.0-0.1 for those, 0.5-0.9 for good cuts.
    Too few words to tell = not verified = not offered."""
    exp = tokens(expected)
    if len(exp) < 5:
        return False, 0.0
    r = len(exp & tokens(got)) / len(exp)
    return r >= need, round(r, 2)


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
    """Moments the crew marked with /moment in Discord (Owner 2026-10-29), window-relative.
    the owner's lane only; the worker never failing a run: no moments -> the run is exactly as before."""
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


def audio_base(lo, hi):
    """VOD second of the window audio's first sample. A section starting at 0 is "no start time" to yt-dlp, and on a
    stream that is still live it then RECORDS FROM THE LIVE EDGE (4-12 Nov: every hour-0 window transcribed hours 2-4,
    took 2 h instead of 12 min, and cut its clips from hours 0-2: wrong footage on every card). Such a window starts
    1 s in, so it seeks like every other window. Whole-VOD runs (no section) are unaffected."""
    return max(lo, 1.0) if (lo > 0 or hi > 0) else 0.0


def shortlist(wins, words, dur):
    """The best-scoring stretches, at least SPACING apart, padded with expand() (the judge gets more around them)."""
    cands = []
    for w in sorted(wins, key=lambda w: -w["score"]):
        s, e = expand(w, words, dur)
        if all(abs(s - c["s"]) > SPACING for c in cands):
            cands.append({"s": s, "e": e, "score": round(w["score"], 1), "peak": w["peak"], "text": text_between(words, s, e)})
        if len(cands) == N_CAND:
            break
    return cands


def choose(cands, wav, dur, stream_title, n_pick, words, ask=None):
    """[(candidate, title)] worth offering, best first: everything that clears the floor and no veto, so the caller can
    fall through to the next one when a cut fails its footage checks. The caller offers at most n_pick of them, and
    none at all if nothing in the window is good enough (owner rejected 0/24 forced picks on 11-12 Nov).
    Each candidate's s/e/text are its final cut."""
    judged = False
    if cands:
        try:
            judged = judge.judge(wav, cands, dur, stream_title, **({"ask": ask} if ask else {}))
        except Exception as e:                       # never lose a window to the judge: fall back, loudly
            print(f"judge failed ({type(e).__name__}: {str(e)[:160]}), falling back to the ranker + floor")
    out = []

    def take(c, s, e, ws, title):
        if any(s < o["e"] + 5 and e > o["s"] - 5 for o, _ in out):
            return False
        c.update(s=s, e=e, text=text_between(ws, s, e))
        out.append((c, title))
        return True

    if judged:
        pool = [c for c in cands if c.get("judge") and not judge.vetoed(c["judge"]) and judge.rank(c) >= judge.FLOOR]
        pool.sort(key=lambda c: -judge.rank(c))
        for c in pool:
            b = c["judge"]["bounds"] or (c["s"], c["e"])
            ws = c.get("words") or words
            take(c, *snap(ws, b[0], b[1], dur), ws, c["judge"]["title"])
        print(f"{len(out)} candidates clear the floor {judge.FLOOR} with no veto: "
              + ", ".join(f"{c['s']:.0f}s rank {judge.rank(c):.2f}" for c, _ in out))
        # Titles: the live ranker's prompt (first-person hooks, tuned 10-29) over the re-heard text, which no longer
        # titles mishearings. The judge's own title is the fallback, a quote the last resort (main()).
        head = list(range(min(5, len(out))))
        for i, t in (rerank([out[k][0] for k in head], stream_title, len(head)) or []) if head else []:
            if t:
                out[i] = (out[i][0], t)
        return out
    # judge down: the old AI ranker, only over stretches above the heuristic floor
    strong = [c for c in cands if c["score"] >= judge.FALLBACK_FLOOR or chat.boost(*c.get("chat", (0, 0))) >= 0.8]
    print(f"no judge: {len(strong)} of {len(cands)} candidates clear the heuristic floor {judge.FALLBACK_FLOOR}")
    if not strong:
        return out
    order = rerank(strong, stream_title, min(5, len(strong))) or []
    order += [(i, "") for i in range(len(strong)) if i not in {k for k, _ in order}]
    for i, title in order:
        c = strong[i]
        ws = c.get("words") or words
        take(c, *snap(ws, c["s"], c["e"], dur), ws, title)
    return out


STILL_MAX = 0.85   # share of frozen 0.5 s steps above which a cut is a static screen (the 12 Nov wardrobe menu: 0.91)


def stillness(path):
    """Share of 0.5 s steps in which the picture barely changes (mean |diff| < 2 of 255 at 160x90 grey).
    Menus, inventories, maps and paused screens sit near 1; his approved clips mostly below 0.45."""
    import numpy as np
    w, h = 160, 90
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps=2,scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    fr = np.frombuffer(raw, np.uint8)[: len(raw) // (w * h) * w * h].reshape(-1, h, w).astype(np.float32)
    if len(fr) < 3:
        return 1.0                                   # cannot tell = treat as static (never offer blind)
    d = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))
    return round(float((d < 2.0).mean()), 2)


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
    base = audio_base(a.lo, a.hi)
    os.makedirs(a.out, exist_ok=True)
    url = f"https://www.twitch.tv/videos/{a.vod}"

    v = common.helix(f"videos?id={a.vod}")["data"][0]
    print(f"VOD {a.vod}  {v['duration']}  {v['created_at']}")
    if common.WHO != "leon" and v.get("user_id") != common.TWITCH_UID:
        raise SystemExit(f"VOD {a.vod} belongs to {v.get('user_login')}, not {common.LOGIN}. Nothing picked.")

    audio = os.path.join(a.out, "audio.m4a")
    wav = os.path.join(a.out, "audio.wav")
    if not os.path.exists(wav):
        section = ["--download-sections", f"*{base:.2f}-{a.hi:.2f}" if a.hi else f"*{base:.2f}-inf"] if windowed else []
        subprocess.run(["yt-dlp", "-q", "--no-progress", "-N", "8", "-f", "Audio_Only/bestaudio", *section, "-o", audio, url], check=True)
        cut = ["-t", str(a.max_seconds)] if a.max_seconds else []
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", audio, *cut, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], check=True)
        os.remove(audio)
    dur = render.duration(wav)                       # window-relative from here on; base turns it back into VOD time
    print(f"audio {dur/60:.1f} min" + (f" (VOD {base/3600:.2f} h to {(base + dur)/3600:.2f} h)" if windowed else ""))

    tpath = os.path.join(a.out, "transcript.json")
    if os.path.exists(tpath):
        words = json.load(open(tpath, encoding="utf-8"))
    else:
        words = render.transcribe(wav, model=a.model)
        json.dump(words, open(tpath, "w", encoding="utf-8"))
    print(f"{len(words)} words transcribed")
    if len(words) < 200:
        raise SystemExit("transcript is nearly empty; refusing to pick clips from nothing")

    exclude = [(x - base, y - base) for x, y in common.vod_clips(a.vod) if y > base and x < base + dur]
    print(f"{len(exclude)} existing Twitch clips excluded")
    wins = windows(words, peaks(wav), dur, exclude, base=base, tail=not a.hi)
    cands = shortlist(wins, words, dur)
    marked = moment_cands(moments(a.vod, base, dur), wins, words, dur, exclude)
    cands = [c for c in cands if all(abs(c["s"] - m["s"]) > SPACING for m in marked)]
    tl = chat.timeline(a.vod, base, base + dur + chat.AFTER) if cands else []
    for c in cands:
        c["chat"] = chat.around(tl, c["s"] + base, c["e"] + base)
    print(f"{len(wins)} scored windows -> {len(cands)} candidates, {len(marked)} marked moments; "
          f"{len(tl)} chat messages, {sum(1 for c in cands if c['chat'][0])} candidates with chat after them")

    ranked = choose(cands, wav, dur, v["title"], a.picks, words)
    queue = [(m, m["note"]) for m in marked] + ranked   # crew-marked moments are always offered, as before
    tries = len(marked) + min(len(ranked), a.picks + 4)  # a few spares for cuts that fail the footage checks

    clips, refused, static, picked, tried = [], [], [], 0, 0
    for c, title in queue[:tries]:
        marked_one = "note" in c
        if not marked_one and picked >= a.picks:
            break
        tried += 1
        n = len(clips) + 1
        raw = os.path.join(a.out, f"raw{n}.mp4")
        tmp = raw + ".part.mp4"
        subprocess.run(["yt-dlp", "-q", "--no-progress", "-N", "8", "-f", render.VOD_FORMAT,
                        "--download-sections", f"*{c['s'] + base:.2f}-{c['e'] + base:.2f}", "--force-keyframes-at-cuts",
                        "-o", tmp, url], check=True)
        render.require_1080(tmp, f"clip {n} source")
        heard = render.transcribe(tmp)               # the captions' words; also proves the footage is the moment
        ok, r = aligned([w for w in c.get("words") or words if c["s"] <= w["s"] <= c["e"]], heard)
        if not ok and not marked_one:                # a /moment mark has no transcript claim to check
            print(f"REFUSED {c['s'] + base:.0f}-{c['e'] + base:.0f}s: the footage does not say what the transcript said "
                  f"there (match {r}). Not offered.")
            refused.append(round(c["s"] + base))
            os.remove(tmp)
            continue
        still = stillness(tmp)
        jd = c.get("judge") or {}
        if still >= STILL_MAX and not marked_one and jd.get("kind") != "scare":
            print(f"SKIPPED {c['s'] + base:.0f}-{c['e'] + base:.0f}s: the picture is a static screen ({still:.0%} frozen: "
                  "menu, map, inventory or pause). Trying the next one.")
            static.append(round(c["s"] + base))
            os.remove(tmp)
            continue
        shutil.move(tmp, raw)
        picked += 0 if marked_one else 1
        title = title or quote_title(c["text"])
        info = render.render(raw, os.path.join(a.out, f"clip{n}.mp4"), os.path.join(a.out, f"preview{n}.mp4"), hook=title,
                             words=heard)
        # raw{n}.mp4 stays in the artifact so a title edited in Discord can be re-rendered
        copy = common.clip_copy(title, stream_title=v["title"], transcript=c["text"])
        # the signals ride along in the manifest, so later approvals can re-calibrate the floor (judge.py)
        clips.append({"n": n, "start": round(c["s"] + base, 2), "end": round(c["e"] + base, 2), "title": title, "score": c["score"],
                      **({"judge": jd["score"], "kind": jd["kind"], "who": jd["who"], "rank": round(judge.rank(c), 2)} if jd else {}),
                      "match": r, "still": still, **({"chat": list(c["chat"])} if c.get("chat") else {}),
                      "quote": c["text"][:300], **({"marked": True} if marked_one else {}), **copy, **info})
        print(f"clip {n}: {c['s'] + base:.0f}-{c['e'] + base:.0f}s score {c['score']} judge {jd.get('score')} "
              f"match {r} still {still} -> {info}")
    if len(refused) >= 2 and len(refused) * 2 >= tried:
        # most cuts landing somewhere else = the window's timing is wrong, not one bad moment: fail loudly
        raise SystemExit(f"{len(refused)} of {tried} clips did not match their transcript (at {refused}); "
                         "the audio and the video disagree on time. Nothing offered.")

    json.dump({"vod": a.vod, "batch": batch, "window": [base, base + dur] if windowed else None, "kind": "vod",
               "vod_title": v["title"], "vod_created": v["created_at"], "clips": clips,
               **({"refused": refused} if refused else {}), **({"static": static} if static else {})},
              open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.remove(wav)
    print(f"manifest written: {len(clips)} clip(s)" + ("" if clips else ", nothing in this stretch cleared the bar"))


if __name__ == "__main__":
    sys.exit(main())

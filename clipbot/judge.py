"""Second pass over the picker's shortlist: hear it properly, then let an AI judge it.

Why (diagnosis 2026-11-12, clipbot-quality-1112.md): the fast scan (whisper base.en over the whole window)
mishears enough that titles were made up from mistakes ("My Character Turns Into Christ"), it cannot tell the
owner's lines from a game's scripted dialogue (a 50 s Cyberpunk cutscene was offered because the script
swears), and a forced 3 picks per window offered whatever scored best even when nothing happened.

So for each shortlisted stretch:
  1. re-transcribe just that stretch with a better model (small.en) from the window audio already on disk;
  2. one AI call judges every stretch at once: a 0-10 score, whose words the funny part is (streamer or game),
     what kind of moment it is, where the setup starts and the payoff ends, and a title built only from what
     the transcript clearly shows;
  3. only stretches that clear the bar are kept, so a slow window yields fewer picks, or none.

The AI runs in the worker (/clipbot/judge, Workers AI, free plan). If that route is missing or fails, the caller
falls back to the old ranker behind a stricter heuristic floor: never a silent "0 picks" from a judge outage.
"""
import json
import re

from . import common, render

PAD_BEFORE, PAD_AFTER = 20.0, 15.0   # stretch handed to the judge around the scored window
MAX_JUDGE = 12                       # stretches judged per run (one AI call)
MAX_CHARS = 1400                     # transcript per stretch in the prompt
# Calibrated 2026-11-12 on 54 picks the owner already decided (scratch eval, report clipbot-quality-1112.md):
# as a GATE (score >= 7) the text judge would have dropped 14 of his 16 approvals: from words alone it cannot see
# what makes him laugh (the picture). As a VETO it is precise: these verdicts removed 7 skips and 1 approval.
# So it vetoes and it helps rank (rank()); it never decides alone.
VETO_KINDS = {"menu", "loading", "logistics", "sponsor"}
KINDS = {"reaction", "fail", "chaos", "scare", "banter", "clutch", "glitch", "story", "chat", "nothing"} | VETO_KINDS
# rank() needed to be offered. On the 54 decided picks: floor 2.5 offers 10, he approved 6 (60%; the old picker 30%);
# 1.0 offered 42 (33%). He approves ~1-2 per batch and can post 2 a day (4 in a marathon), so fewer, better cards.
# On the 5 test windows it sends picks from the 6 Nov stretches he liked and ~none from 11-12 Nov (0/24 approved).
FLOOR = 2.5
FALLBACK_FLOOR = 20.0                # heuristic score needed when the judge is down (45% approved on the same 54)
SCAN_CAP = 30.0                      # scan score counted at most this much in rank()


def retranscribe(wav, cands, dur, model="small.en"):
    """Fills c["region"] = (lo, hi) and c["words"] = accurate words in window seconds, per candidate."""
    for c in cands:
        lo, hi = max(0.0, c["s"] - PAD_BEFORE), min(dur, c["e"] + PAD_AFTER)
        segs, _ = render.whisper(model).transcribe(render.load_audio(wav, lo, hi - lo), language="en",
                                                   word_timestamps=True, vad_filter=True,
                                                   condition_on_previous_text=False, beam_size=5)
        ws = []
        for s in segs:
            for w in (s.words or []):
                t = w.word.strip()
                if t:
                    ws.append({"w": t, "s": round(float(w.start) + lo, 2), "e": round(float(w.end) + lo, 2)})
        c["region"], c["words"] = (round(lo, 2), round(hi, 2)), ws
    return cands


def lines(words, t0, gap=0.8):
    """'[12.4] text' per phrase, times relative to t0 (the stretch start)."""
    out, cur = [], []
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > gap:
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return [f"[{p[0]['s'] - t0:.1f}] " + " ".join(x["w"] for x in p) for p in out]


def prompt(cands, stream_title):
    blocks = []
    for i, c in enumerate(cands):
        lo, hi = c["region"]
        body = "\n".join(lines(c["words"], lo))
        if len(body) > MAX_CHARS:
            body = body[:MAX_CHARS] + " ..."
        blocks.append(f"#{i} (stretch is {hi - lo:.0f} s long)\n{body or '(no speech)'}")
    return "\n".join([
        f'You pick moments from one Twitch livestream to post as YouTube Shorts and TikToks. Stream title: "{stream_title[:150]}".',
        "Each candidate is a stretch of the stream's auto-transcript (it mishears some words). The audio mixes the streamer's "
        "microphone with the game, so some lines are the GAME's scripted dialogue (NPCs, cutscenes, radio, phone calls, mission "
        "voice-over), not the streamer. Times in [brackets] are seconds from the start of that stretch.",
        "",
        'Score each candidate 0-10 for "a stranger scrolling Shorts watches to the end and laughs or reacts":',
        "- 8-10: a clear payoff that lands on its own: a genuine big reaction, a fail, death, crash or wipe-out, chaos, a scare, "
        "a funny line or banter with friends, a clutch play, a ridiculous glitch. Makes sense without earlier context.",
        "- 5-7: mildly funny, or needs some context.",
        "- 0-4: nothing really happens; or the funny-sounding part is the game's scripted dialogue or a cutscene; menu, "
        "settings, inventory, map or shop talk; loading or waiting; reading or thanking chat, subs, raids; stream logistics "
        "(breaks, schedule, lag, tech problems); a sponsor read or promo; or it only makes sense with earlier context.",
        "Be strict: most of a stream is not Short-worthy, and a 9 is rare. Swearing alone is not a payoff.",
        "",
        "For each candidate give:",
        '- "who": "streamer" if the payoff is the streamer\'s own words or reaction, "game" if it is mostly scripted game '
        'dialogue, "mixed" otherwise.',
        '- "kind": one of reaction, fail, chaos, scare, banter, clutch, glitch, story, menu, loading, chat, logistics, sponsor, nothing.',
        '- "start": seconds where the clip begins: the start of the sentence that sets up the payoff (usually 3-15 s before it). '
        "The clip must make sense to someone who did not watch the stream.",
        '- "end": seconds where the clip ends: right after the payoff and the reaction or laugh finish, at the end of a '
        "sentence, never mid-line. Aim for 15-45 s between start and end.",
        '- "title" (for EVERY candidate): the on-screen hook, max 50 characters, Title Case. Say what HAPPENS, using only '
        "what the transcript clearly shows (the specific thing: the car, the cops, the boss, the teammate). First person "
        "when it happens to the streamer. If the transcript is too unclear to say what happens, use the streamer's clearest "
        "line as a quote instead. Never invent events, never give away the punchline. No emojis, hashtags, swearing or em dashes.",
        "",
        'Reply with JSON only, one entry per candidate: {"judged":[{"i":0,"score":7,"who":"streamer","kind":"fail",'
        '"start":4.0,"end":31.5,"title":"..."}]}',
        "",
        "\n\n".join(blocks),
    ])


def ask_worker(text, max_tokens=2000):
    """The judge AI. Returns the parsed JSON dict, or None when the route is missing or broken."""
    try:
        return common.worker("/clipbot/judge", {"prompt": text, "max_tokens": max_tokens, **common.who_fields()})
    except Exception as e:
        print(f"judge unavailable ({str(e)[:120]})")
        return None


def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if x == x else None   # NaN -> None


def parse(resp, cands):
    """Validated verdicts {i: {...}}, times turned back into window seconds. Anything malformed is dropped."""
    out = {}
    if not isinstance(resp, dict):
        return out
    for j in resp.get("judged", []) or []:
        if not isinstance(j, dict):
            continue
        i = j.get("i")
        if not isinstance(i, int) or not 0 <= i < len(cands) or i in out:
            continue
        score = _num(j.get("score"))
        if score is None or not 0 <= score <= 10:
            continue
        lo, hi = cands[i]["region"]
        st, en = _num(j.get("start")), _num(j.get("end"))
        bounds = None
        if st is not None and en is not None and 0 <= st < en <= hi - lo + 1:
            bounds = (lo + st, min(hi, lo + en))
        title = re.sub(r"\s+", " ", str(j.get("title") or "")).strip().strip('"').replace("—", ",").replace("–", "-")
        kind = str(j.get("kind") or "").lower().strip()
        out[i] = {"score": score, "who": str(j.get("who") or "").lower().strip(),
                  "kind": kind if kind in KINDS else "", "bounds": bounds,
                  "title": title if 3 <= len(title) <= 70 else ""}
    return out


def vetoed(v):
    """Clearly not a clip: the payoff is the game's script (a cutscene), or it is a menu, loading, logistics or a promo."""
    return v["who"] == "game" or v["kind"] in VETO_KINDS


def rank(c):
    """Offer order and floor: the scan's heuristic score (what he says, how fast, how loud) + the judge's score
    + how much chat reacted (chat.boost)."""
    from .chat import boost
    # the scan score is capped: fast talking alone scored 63.5 and outranked everything (6 Nov test, judge 2/10)
    return min(c["score"], SCAN_CAP) / 20 + (c["judge"]["score"] / 4 if c.get("judge") else 0) + boost(*c.get("chat", (0, 0)))


def judge(wav, cands, dur, stream_title, ask=ask_worker):
    """Adds c["judge"] to each candidate it judged. Returns True if the judge answered for at least half."""
    cands = cands[:MAX_JUDGE]
    if not cands:
        return False
    retranscribe(wav, cands, dur)
    verdicts = parse(ask(prompt(cands, stream_title)), cands)
    for i, c in enumerate(cands):
        if i in verdicts:
            c["judge"] = verdicts[i]
    print(f"judge: {len(verdicts)}/{len(cands)} judged, {sum(1 for v in verdicts.values() if vetoed(v))} vetoed "
          + json.dumps([[i, v['score'], v['who'], v['kind']] for i, v in sorted(verdicts.items())]))
    return len(verdicts) * 2 >= len(cands)

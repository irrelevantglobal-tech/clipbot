"""Stage 3: plan ONE episode from the cloud analysis of a marathon window. Code decides every cut; ONE capped
Claude call only CHOOSES among options code has already proven legal; code then validates
every choice and refuses rather than guessing.

    python -m polished.plan_cut <analysis.json> <workdir> --start <s> [--final] [--vod-start]
                                [--game "GTA 6"] [--brand <name>] [--budget-limit $ --budget-spent $] [--no-llm]

MARATHON (2026-11-12): episodes of one window are planned one after another, each starting EXACTLY at
the previous one's next_start (`--start`), so nothing falls between two episodes. Exit codes:
  0 = plan.json written;  2 = TAIL: what is left is under one episode and the window is not final
  (tail.json says where the next window must start);  3 = HOLD: the cloud API budget cannot pay for
  the call, or its answer is unusable (the prompt is saved as plan_prompt.txt so a Claude session can
  answer it; re-run with --reuse-llm <answer>).
A stream that never goes offline (24/7 marathon, the owner asleep on a resting screen): AWAY_MIN
without one word is cut whatever the picture and audio do (the resting screen plays music and moves,
so no quiet/static rule sees it), and a cut of SESSION_BREAK or more ends the episode before it.
Part numbers are given at approval (ep_publish), so the title reads "Part N" here and the description
starts with the literal token "Part {PART}." that ep_publish fills in.

Writes <workdir>/plan.json:
  {"keep": [[s,e]...], "cold": [s,e], "title", "sting_name", "description", "chapters":
   [[render_s, label]...], "bounds": [s,e], "cuts": [[s,e,kind]...], "stats": {...}}

Rules ported from the recipe that shipped WD1/WD2/WDL (wd2_cuts.py, wd2_plan.py,
wd2_emit_metadata.py), every one paid for:
* DEAD AIR = no speech (word times +-0.35 s, p >= 0.25) AND low action AND ABSOLUTELY quiet
  (mean < -62 dBFS, max < -46). Game audio protects itself: a loud cutscene is never cut.
  >= 2.5 s gaps keep 0.55 s head / 0.45 s tail; >= 12 s gaps keep 0.9 s in total.
* NEVER DELETE SPEECH. Dead PICTURE (black luma < 8 for >= 12 s, or a static screen >= 15 s
  with <= 15% speech) is cut with a head kept (10 s black / 4 s static) so the reaction survives.
* BOUNDARIES sit in a word gap >= 3.0 s, at the gap's middle, >= 1.5 s from any word
  ("1.5 s is not enough" - wdl-episodes.md), never within 4 s of a cut edge.
* LENGTH in CONTENT seconds: 900 s floor, 1500 s ceiling (15-25 min).
* CHAPTERS: authored in source seconds, mapped render_t = cold + INTRO_D + kept-before(src),
  snapped FORWARD out of cuts; first at 0:00, >= 3, >= 10 s apart, last before the outro
  (YouTube drops ALL chapters silently otherwise).
* MENUS: CLIP P(menu) smoothed > 0.6 for >= 30 s AND median motion < 5/255 is a CANDIDATE;
  the planner call rules cut/keep from the words inside it; no ruling = keep (4 s head kept), speech or not.
* COLD OPEN wholly inside ONE kept span, on word gaps, 5-15 s, not on a black/static screen.
* TITLE "<Game> Part N - <title>" + " | <brand>" <= 100 chars (brand from the channel identity); description
  ends where ep_publish adds the handles, no kick.com, no language warning, no total part count, no em dashes.
"""
import argparse
import bisect
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polished import llm  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
INTRO_D, OUTRO_D = 4.6, 20.0
SPEECH_PAD, P_MIN = 0.35, 0.25
ACTION_TH, ABS_QUIET, ABS_MAX = 0.50, -62.0, -46.0
MIN_GAP, HEAD, TAIL = 2.5, 0.55, 0.45
DEAD_MAX_DIFF = 5.0      # short dead-air cuts only where the picture is (nearly) still
SENT_GAP, SENT_REACH = 0.6, 6.0   # menu-cut edges move inward to a >= 0.6 s word gap within 6 s
LONG_GAP, LONG_KEEP = 12.0, 0.9
BLACK_Y, BLACK_MIN, BLACK_HEAD = 8.0, 12.0, 10.0
STATIC_DIFF, STATIC_MIN, STATIC_HEAD, VIS_MAX_SPEECH = 1.2, 15.0, 4.0, 0.15
MENU_TH, MENU_SMOOTH, MENU_MIN, MENU_BRIDGE, MENU_MAX_DIFF = 0.6, 5, 30.0, 10.0, 5.0
MENU_MAX_SPEECH = 0.40   # WD2 T2: a 45 s controls screen cut at 51% speech (his riff); story screen kept at 37%
BOUND_GAP, BOUND_CLEAR = 3.0, 1.5
MIN_EP, MAX_EP = 900.0, 1500.0
AWAY_MIN, AWAY_KEEP = 20 * 60.0, 2.0     # episodes.py AWAY_MIN: 20 min without a word = nobody there
SESSION_BREAK = 30 * 60.0                # episodes.py: a cut this long ends a session
SHORT_MIN = 5 * 60.0                     # reported: the last episode before a break / VOD end may be this short
TAIL_MERGE = 6 * 60.0                    # closing: a leftover under this joins the last episode


def pct(x):
    return np.argsort(np.argsort(x)).astype(np.float32) / max(len(x) - 1, 1)


def merge(spans):
    out = []
    for a, b, *k in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b] + (k or [""]))
    return out


def subtract(lo, hi, cuts):
    keep, cur = [], lo
    for a, b, *_ in cuts:
        if b <= lo or a >= hi:
            continue
        if a > cur:
            keep.append([round(cur, 3), round(a, 3)])
        cur = max(cur, b)
    if cur < hi:
        keep.append([round(cur, 3), round(hi, 3)])
    return [k for k in keep if k[1] - k[0] >= 0.25]   # sub-frame slivers are seams, not content


def mmss(t):
    t = int(t)
    return "%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60) if t >= 3600 else "%d:%02d" % (t // 60, t % 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("analysis")
    ap.add_argument("work")
    ap.add_argument("--start", type=float, default=None, help="episode start (analysis seconds); chained from next_start")
    ap.add_argument("--final", action="store_true", help="the VOD has ended: emit the tail too")
    ap.add_argument("--vod-start", action="store_true", help="the window opens the VOD: start just before the first word")
    ap.add_argument("--game", default="GTA 6")
    ap.add_argument("--brand", default="")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--reuse-llm", default=None, help="saved plan_llm.json to re-validate (no call)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--budget-limit", type=float, default=0.0)
    ap.add_argument("--budget-spent", type=float, default=0.0)
    a = ap.parse_args()
    a.part = "{PART}"
    SUFFIX = " | " + a.brand if a.brand else ""
    an = json.load(open(a.analysis, encoding="utf-8"))
    dur = float(an["duration"])
    am = an["audio_map"]
    hop = am["hop"]
    rms, flux, low = (np.array(am[k], np.float32) for k in ("rms", "flux", "low"))
    NS = len(rms)
    segs = an["transcript"]["segments"]
    words = sorted((w["s"], w["e"], w["w"], w.get("p", 1.0)) for s in segs for w in (s.get("words") or []))
    if len(words) < 50:
        sys.exit("ABORT: %d words in %.0f s - a failed transcript, not a quiet stream" % (len(words), dur))

    # ---- speech + action masks (wd2_cuts.py)
    speech = np.zeros(NS, bool)
    for s, e, _, p in words:
        if p >= P_MIN:
            speech[max(0, int((s - SPEECH_PAD) / hop)):min(NS, int((e + SPEECH_PAD) / hop))] = True
    vis = an["visual"]
    vt = np.array([v["t"] for v in vis], np.float32)
    vdiff = np.interp(np.arange(NS) * hop, vt, np.array([v["diff"] for v in vis], np.float32))
    k = int(60 / hop)
    padr = np.pad(rms, (k // 2, k // 2), mode="edge")
    floor = np.array([np.percentile(padr[i:i + k], 20) for i in range(0, NS, 20)])
    floor = np.interp(np.arange(NS), np.arange(0, NS, 20)[:len(floor)], floor)
    action = 0.45 * pct(vdiff) + 0.30 * pct(flux) + 0.15 * pct(np.clip(rms - floor, 0, None)) + 0.10 * pct(low)
    dead = (~speech) & (action < ACTION_TH)

    cuts, i = [], 0
    while i < NS:
        if dead[i]:
            j = i
            while j < NS and dead[j]:
                j += 1
            s0, s1 = i * hop, j * hop
            win = rms[i:j]
            quiet = win.mean() < ABS_QUIET and win.max() < ABS_MAX
            if quiet and s1 - s0 >= LONG_GAP:
                cuts.append([s0 + LONG_KEEP / 2, s1 - LONG_KEEP / 2, "dead-long"])
            elif quiet and s1 - s0 >= MIN_GAP and float(np.median(vdiff[i:j])) < DEAD_MAX_DIFF:
                # a short silent gap over a MOVING picture is a jump cut, not dead air: WD2 T1
                # cut 6.3 s + 2.1 s out of the closing ctOS cinematic (quiet music, diff 5.5-7.3)
                cuts.append([s0 + HEAD, s1 - TAIL, "dead"])
            i = j
        else:
            i += 1
    n_audio = len(cuts)

    # ---- dead picture: black runs and static runs, speech-protected
    def runs(mask_rows, min_len):
        out, cur = [], None
        for v, on in mask_rows:
            if on and cur is None:
                cur = [v["t"], v["t"]]
            elif on:
                cur[1] = v["t"]
            elif cur:
                out.append(cur); cur = None
        if cur:
            out.append(cur)
        return [(x, y + 1.0 / 2) for x, y in out if y - x >= min_len]

    def speech_frac(x, y):
        i0, i1 = int(x / hop), int(y / hop)
        return float(speech[i0:i1].mean()) if i1 > i0 else 0.0

    vcuts = []
    for x, y in runs([(v, v["luma"] < BLACK_Y) for v in vis], BLACK_MIN):
        if y - TAIL - (x + BLACK_HEAD) >= 2.0:
            vcuts.append([x + BLACK_HEAD, y - TAIL, "black"])
    st = [(v, v["diff"] < STATIC_DIFF) for v in vis]
    st_runs = runs(st, STATIC_MIN) + [tuple(r) for r in an.get("static", {}).get("runs", [])
                                      if r[1] - r[0] >= STATIC_MIN]
    for x, y, *_ in merge([list(r)[:2] for r in st_runs]):
        if speech_frac(x, y) <= VIS_MAX_SPEECH and y - TAIL - (x + STATIC_HEAD) >= 2.0:
            vcuts.append([x + STATIC_HEAD, y - TAIL, "static"])
    # MENUS (CLIP zero-shot, cloud_episode.menu_map): "a menu is never content" (kit README),
    # even with commentary over it. Short menus (inventory flashes, a quick map check) stay:
    # only a run >= MENU_MIN of smoothed P(menu) > MENU_TH is cut, keeping a 4 s head so the
    # moment he opens it survives. Proven on run 1b: flagged 423-708 s vs eyes 440-712 s.
    menu = an.get("menu") or []
    menu_cands = []
    if menu:
        mt = np.array([r["t"] for r in menu]); mp = np.array([r["menu"] for r in menu])
        sm = np.convolve(mp, np.ones(MENU_SMOOTH) / MENU_SMOOTH, "same")
        mruns, cur = [], None
        for t, v in zip(mt, sm):
            if v > MENU_TH and cur is None:
                cur = [t, t]
            elif v > MENU_TH:
                cur[1] = t
            elif cur is not None:
                mruns.append(cur); cur = None
        if cur:
            mruns.append(cur)
        merged = []
        for r in mruns:   # bridge brief dips (a frame of gameplay between two menu screens)
            if merged and r[0] - merged[-1][1] <= MENU_BRIDGE:
                merged[-1][1] = r[1]
            else:
                merged.append(list(r))
        vtt = np.array([v["t"] for v in vis]); vdd = np.array([v["diff"] for v in vis])
        for x, y in merged:
            # a real menu barely moves (lobby median diff 2.1, inventory 1.0); gameplay ~15 and a
            # death/glitch effect 21 also read as "menu" to CLIP, so motion vetoes those
            sel = (vtt >= x) & (vtt <= y)
            if y - x >= MENU_MIN and sel.any() and float(np.median(vdd[sel])) < MENU_MAX_DIFF:
                menu_cands.append([x + STATIC_HEAD, y + 1.0 - TAIL, "menu"])
    else:
        print("NOTE: no menu map in the analysis - animated menus will NOT be cut")
    # ⚠️ MENU RUNS ARE CANDIDATES, NOT CUTS. WD2 test: CLIP + low motion flagged 104 s of the
    # in-game ctOS profile screen - the STORY beat the published Part 1 is titled after - as a
    # "menu". So the planner call rules on each run from the words spoken during it (lobby
    # chatter vs story dialogue); with no ruling, nothing is cut (fail toward keeping story).
    # AWAY (24/7 stream): AWAY_MIN with no word at all = cut, keeping AWAY_KEEP either side
    acuts, prev = [], 0.0
    for s0, e0, *_ in words + [(dur, dur, "", 1.0)]:
        if s0 - prev >= AWAY_MIN:
            acuts.append([prev + AWAY_KEEP, s0 - AWAY_KEEP, "away"])
        prev = max(prev, e0)
    vcuts += acuts
    cuts = merge(cuts + vcuts)
    # NO CUT EDGE INSIDE A WORD (any p). Picture cuts (black/static/menu) are timed off frames,
    # not speech, and run 1b shipped one ending mid-word (690.55). Shrink the cut to clear it.
    wsa = np.array([w[0] for w in words]); wea = np.array([w[1] for w in words])

    def clear_words(spans):
        fixed = []
        for c in spans:
            a0, b0 = c[0], c[1]
            for _ in range(len(words)):
                hit = np.where((wsa - 0.05 < a0) & (a0 < wea + 0.05))[0]
                if not len(hit):
                    break
                a0 = float(wea[hit].max()) + 0.08
            for _ in range(len(words)):
                hit = np.where((wsa - 0.05 < b0) & (b0 < wea + 0.05))[0]
                if not len(hit):
                    break
                b0 = float(wsa[hit].min()) - 0.08
            if b0 - a0 >= 1.0:
                fixed.append([round(a0, 3), round(b0, 3)] + list(c[2:]))
        return fixed
    cuts = clear_words(cuts)

    def to_sentence(c):
        # menu edges follow the PICTURE; move each inward to a pause between sentences so the
        # seam doesn't land mid-thought (WD2 T1 12:27 joined "...what's this? || are to attract")
        a0, b0 = c[0], c[1]
        # sentence end = a word ending . ? ! followed by a pause; fall back to any >= 0.6 s pause
        def ends(w):
            return w.strip().endswith((".", "?", "!"))
        pairs = list(zip(words, words[1:]))
        g_after = [e0 + 0.15 for (s0, e0, w0, _), (s1, *__) in pairs
                   if ends(w0) and s1 - e0 >= 0.25 and a0 <= e0 <= a0 + SENT_REACH] or                   [e0 + 0.15 for (s0, e0, *_), (s1, *__) in pairs
                   if s1 - e0 >= SENT_GAP and a0 <= e0 <= a0 + SENT_REACH]
        g_before = [s1 - 0.15 for (s0, e0, w0, _), (s1, *__) in pairs
                    if ends(w0) and s1 - e0 >= 0.25 and b0 - SENT_REACH <= s1 <= b0] or                    [s1 - 0.15 for (s0, e0, *_), (s1, *__) in pairs
                    if s1 - e0 >= SENT_GAP and b0 - SENT_REACH <= s1 <= b0]
        # no clean break inside: move OUTWARD to the next real pause (<= 20 s), so the seam lands
        # after he finishes, not mid-flow (WD2 T1: one 16 s run-on read of a controls tutorial)
        if not g_after:
            g_after = [e0 + 0.15 for (s0, e0, *_), (s1, *__) in pairs
                       if s1 - e0 >= 1.0 and a0 - 20 <= e0 < a0][-1:]
        if not g_before:
            g_before = [s1 - 0.15 for (s0, e0, *_), (s1, *__) in pairs
                        if s1 - e0 >= 1.0 and b0 < s1 <= b0 + 20][:1]
        a0 = g_after[0] if g_after else a0
        b0 = g_before[-1] if g_before else b0
        return [round(a0, 3), round(b0, 3)] + list(c[2:])
    menu_cands = clear_words([to_sentence(c) for c in menu_cands])

    # ---- boundary candidates: word gaps >= 3 s, boundary at the middle, clear of cuts
    gaps = []
    for (s0, e0, w0, _), (s1, e1, w1, _) in zip(words, words[1:]):
        g = s1 - e0
        if g >= BOUND_GAP:
            mid = (e0 + s1) / 2
            if any(c[0] - 4 <= mid <= c[1] + 4 for c in cuts + menu_cands):
                continue
            if mid - e0 >= BOUND_CLEAR and s1 - mid >= BOUND_CLEAR:
                gaps.append({"t": round(mid, 2), "gap": round(g, 2), "before": e0, "after": s1})
    if a.start is not None:
        start = float(a.start)                       # chained: EXACTLY where the last episode ended
        for c in cuts:                               # a start inside a cut begins where the cut ends
            if c[0] <= start < c[1]:
                start = float(c[1])
    elif a.vod_start:
        start = max(0.0, words[0][0] - 3.0)          # the VOD opens: just before the first word
    else:
        head_gaps = [g for g in gaps if 5.0 <= g["t"] <= 240.0]
        if not head_gaps:
            sys.exit("ABORT: no legal start boundary (word gap >= 3 s) in the first 4 minutes")
        start = head_gaps[0]["t"]

    def content(lo, hi):
        return sum(e - s for s, e in subtract(lo, hi, cuts))

    def tail(next_start, why):
        json.dump({"next_start": round(next_start, 3), "why": why, "left_content_s": round(content(next_start, dur), 1)},
                  open(os.path.join(a.work, "tail.json"), "w", encoding="utf-8"))
        print("TAIL: %s; next window starts at %.1f" % (why, next_start))
        sys.exit(2)

    # a long cut (asleep / away) ends the session: the episode must close before it
    brk = next((c for c in cuts if c[1] - c[0] >= SESSION_BREAK and c[1] > start), None)
    hard_end = brk[0] if brk else dur
    last_word = max((e for s_, e, *_ in words if start <= s_ < hard_end), default=start)
    close_t = min(hard_end, last_word + 3.0)         # where the talk ends before the break / at the VOD end
    closing = bool(brk) or a.final                   # this window may end an episode short
    cands = [dict(g, content=round(content(start, g["t"]), 1)) for g in gaps if start < g["t"] < hard_end]
    left = content(start, close_t)
    if left < 1.0 and brk:
        tail(float(brk[1]), "nothing but the break left")   # skip the break itself; the next episode starts after it
    if left < MIN_EP and not closing:
        tail(start, "only %.0f s of content left in the window" % left)
    if closing and left < MIN_EP + TAIL_MERGE:
        # the rest of the session fits one episode: take all of it, ending where the talk ends
        if left < SHORT_MIN:
            print("NOTE: only %.0f s of content before the %s; kept as a short episode" % (
                left, "break" if brk else "end of the VOD"))
        if left < 1.0:
            tail(dur, "nothing left in the VOD")
        legal = [{"t": round(close_t, 2), "gap": 8.0, "content": round(left, 1), "close": True}]
    else:
        legal = [c for c in cands if MIN_EP <= c["content"] <= MAX_EP]
        if closing:   # never leave a leftover under the floor after this episode, unless it is nothing
            ok = [c for c in legal if content(c["t"], close_t) >= MIN_EP or content(c["t"], close_t) < 1.0]
            legal = ok or legal
        if not legal:   # no pause in the 15-25 min band: the first legal pause past the floor
            legal = [c for c in cands if c["content"] >= MIN_EP][:1]
        if not legal and closing:
            legal = [{"t": round(close_t, 2), "gap": 8.0, "content": round(left, 1), "close": True}]
        if not legal:
            tail(start, "no legal end boundary in the window")
    # default (no model / model invalid): longest gap, ties to the one nearest 20 min of content
    default_end = max(legal, key=lambda c: (min(c["gap"], 8.0), -abs(c["content"] - 1200)))

    def text_between(x, y):
        return " ".join(w.strip() for s, e, w, _ in words if x <= s < y).strip()

    # ---- the ONE capped Claude call
    choice, cost_line = {}, "no-llm"
    if a.reuse_llm:   # re-plan from a saved model answer: no new call, same validation
        raw = open(a.reuse_llm, encoding="utf-8").read()
        m = re.search(r"\{.*\}", raw, re.S)
        choice, cost_line = (json.loads(m.group(0)) if m else {}), "reused " + a.reuse_llm
    elif not a.no_llm:
        lines = []
        for sg in segs:
            if sg["e"] < start or sg["s"] > legal[-1]["t"] + 5:
                continue
            lines.append("[%7.1f] %s" % (sg["s"], sg["text"].strip()))
        opts = ["E%d  t=%.1f  gap=%.1fs  content=%.0fs  ...%s | %s..." % (
            n, c["t"], c["gap"], c["content"], text_between(c["t"] - 25, c["t"])[-110:],
            text_between(c["t"], c["t"] + 60)[:240]) for n, c in enumerate(legal)]
        mlines = ["M%d  src %.0f-%.0f (%.0f s): \"%s\"" % (n, m_[0], m_[1], m_[1] - m_[0],
                                                        text_between(m_[0], m_[1])[:600])
                  for n, m_ in enumerate(menu_cands) if start <= m_[0] <= legal[-1]["t"]]
        example = json.load(open(os.path.join(HERE, "style_example.json"), encoding="utf-8"))
        prompt = open(os.path.join(HERE, "plan_prompt.md"), encoding="utf-8").read()
        prompt = (prompt.replace("{GAME}", a.game).replace("{PART}", str(a.part))
                  .replace("{START}", "%.1f" % start).replace("{OPTIONS}", "\n".join(opts))
                  .replace("{EXAMPLE}", json.dumps(example, indent=1, ensure_ascii=False))
                  .replace("{MENUS}", "\n".join(mlines) or "(none)")
                  .replace("{TRANSCRIPT}", "\n".join(lines)))
        prompt = prompt.replace("{BRAND}", a.brand or "the streamer")
        pf = os.path.join(a.work, "plan_prompt.txt")
        open(pf, "w", encoding="utf-8").write(prompt)
        of = os.path.join(a.work, "plan_llm.json")
        bud = llm.Budget(a.budget_limit, a.budget_spent)
        try:
            raw, c_ = bud.call("gta6-review-plan", prompt, model=a.model, max_tokens=5000)
        except llm.Dry as ex:
            json.dump({"why": str(ex), "start": start}, open(os.path.join(a.work, "hold.json"), "w", encoding="utf-8"))
            print("HOLD: %s (prompt saved: %s)" % (ex, pf))
            sys.exit(3)
        json.dump(bud.calls, open(os.path.join(a.work, "llm_calls.json"), "w", encoding="utf-8"))
        open(of, "w", encoding="utf-8").write(raw)
        cost_line = "cloud %s $%.4f" % (a.model, c_)
        print(cost_line)
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            choice = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            choice = {}
    if not a.no_llm and not choice:
        # POLISHED ONLY: code defaults are legal but carry no title, cold open or chapters worth
        # publishing. HOLD rather than ship them; a Claude session answers plan_prompt.txt instead.
        json.dump({"why": "model answer unusable", "start": start},
                  open(os.path.join(a.work, "hold.json"), "w", encoding="utf-8"))
        print("HOLD: model answer unusable")
        sys.exit(3)

    problems = []
    # end boundary
    end = default_end
    ei = choice.get("end_option")
    et = choice.get("end_t")   # a reused answer pins its end TIME (option indexes can shift)
    near = [c for c in legal if isinstance(et, (int, float)) and abs(c["t"] - et) <= 1.0]
    if near:
        end = near[0]
    elif isinstance(ei, int) and 0 <= ei < len(legal):
        end = legal[ei]
    elif choice:
        problems.append("end_option %r not a listed option; used code default" % ei)
    # the model's own per-option check: never end where IT says the scene carries on
    # (WD2 T2: ended on "Welcome to Dedsec" with the crew intro still to come; Leon preferred
    # the published cut that kept it). Fall to the latest option it marked as a clean end.
    checks = {r.get("option"): r for r in choice.get("end_checks") or [] if isinstance(r, dict)}
    if checks:
        mine = checks.get(legal.index(end))
        if mine and mine.get("same_scene") is True:
            clean_ends = [legal[n] for n, r in checks.items()
                          if isinstance(n, int) and 0 <= n < len(legal) and r.get("same_scene") is False]
            if clean_ends:
                later_ok = [c for c in clean_ends if c["t"] > end["t"]]   # first clean end AFTER the scene
                new = min(later_ok, key=lambda c: c["t"]) if later_ok else max(clean_ends, key=lambda c: c["t"])
                problems.append("end %.1f cuts into a scene (model's own check); moved to %.1f"
                                % (end["t"], new["t"]))
                end = new
    # menu rulings: only listed ids, only "cut" removes anything
    chosen = []
    for mi in choice.get("menu_cut") or []:
        if isinstance(mi, int) and 0 <= mi < len(menu_cands):
            sp = speech_frac(menu_cands[mi][0], menu_cands[mi][1])
            if sp >= MENU_MAX_SPEECH:   # he's talking through it: his riff IS the content
                problems.append("menu %d cut refused: he talks %.0f%% of it" % (mi, sp * 100))
                continue
            chosen.append(menu_cands[mi])
        else:
            problems.append("menu_cut id %r not listed, ignored" % (mi,))
    if chosen:
        cuts = clear_words(merge(cuts + chosen))
    menu_kept = [m_ for m_ in menu_cands if m_ not in chosen]
    if content(start, end["t"]) < MIN_EP and not end.get("close"):   # menu cuts pushed it under the floor: extend
        later = [g for g in gaps if end["t"] < g["t"] < hard_end and content(start, g["t"]) >= MIN_EP]
        if later:
            problems.append("menu cuts took it under %.0f s; end moved %.1f -> %.1f"
                            % (MIN_EP, end["t"], later[0]["t"]))
            end = dict(later[0], content=content(start, later[0]["t"]))
    bounds = (start, end["t"])
    keep = subtract(bounds[0], bounds[1], cuts)

    # cold open: snap to word gaps, must sit in ONE kept span, 5-15 s, not dead picture
    def snap_cold(cs, ce):
        ws = [w for w in words if w[0] >= cs - 0.3 and w[1] <= ce + 0.3]
        if not ws:
            return None
        i0 = words.index(ws[0]); i1 = words.index(ws[-1])
        prev_e = words[i0 - 1][1] if i0 > 0 else ws[0][0] - 1
        next_s = words[i1 + 1][0] if i1 + 1 < len(words) else ws[-1][1] + 1
        s = max(ws[0][0] - 0.6, (prev_e + ws[0][0]) / 2)
        e = min(ws[-1][1] + 1.2, (ws[-1][1] + next_s) / 2)
        return round(s, 2), round(e, 2)

    def cold_ok(c):
        if not c:
            return "no words in that span"
        s, e = c
        if not 5.0 <= e - s <= 15.5:
            return "length %.1f s" % (e - s)
        if not any(k0 <= s and e <= k1 for k0, k1 in keep):
            return "not inside one kept span"
        tile = [v for v in vis if s <= v["t"] <= e]
        if tile and (np.mean([v["luma"] for v in tile]) < BLACK_Y or
                     np.median([v["diff"] for v in tile]) < STATIC_DIFF):
            return "black/static picture"
        return None

    cold, cold_label = None, None
    co = choice.get("cold_open") or {}
    try:
        c = snap_cold(float(co["s"]), float(co["e"]))
        if cold_ok(c) == "not inside one kept span":
            # bl_render.clamp_cold: clip to the kept segment holding most of it, if enough is left
            best_k = max(keep, key=lambda k: min(k[1], c[1]) - max(k[0], c[0])) if c else None
            if best_k:
                # trim the PADDING, not words: the overhang sits in a removed silence
                c2 = (round(max(best_k[0], c[0]), 2), round(min(best_k[1], c[1]), 2))
                if c2 and not cold_ok(c2):
                    problems.append("model cold open clipped to its kept segment")
                    c = c2
        why = cold_ok(c)
        if why:
            problems.append("model cold open rejected: %s" % why)
        else:
            cold, cold_label = c, str(co.get("label", "")).strip()
    except (KeyError, TypeError, ValueError):
        if choice:
            problems.append("model cold open missing")
    if cold is None:   # code fallback: loudest speech-dense 8-12 s window inside a kept span
        best = None
        for k0, k1 in keep:
            t = k0
            while t + 10 <= k1:
                i0, i1 = int(t / hop), int((t + 10) / hop)
                sc = float(speech[i0:i1].mean()) * 10 + float(rms[i0:i1].mean())
                c = snap_cold(t, t + 10)
                if (best is None or sc > best[0]) and not cold_ok(c):
                    best = (sc, c)
                t += 5
        if not best:
            sys.exit("ABORT: no legal cold open anywhere in the episode")
        cold, cold_label = best[1], cold_label or "Cold open"

    # map source second -> render second (chapters)
    starts = [k0 for k0, _ in keep]
    pre = np.cumsum([0] + [k1 - k0 for k0, k1 in keep])
    cold_len = cold[1] - cold[0]

    def render_t(src_s):
        i = bisect.bisect_right(starts, src_s) - 1
        if i < 0:
            return cold_len + INTRO_D
        k0, k1 = keep[i]
        if src_s > k1:   # inside a cut: snap FORWARD
            if i + 1 >= len(keep):
                return None
            return cold_len + INTRO_D + pre[i + 1]
        return cold_len + INTRO_D + pre[i] + (src_s - k0)

    body_len = float(pre[-1])
    total = cold_len + INTRO_D + body_len + OUTRO_D
    chaps = [[0.0, cold_label or "Cold open"]]
    for row in choice.get("chapters") or []:
        try:
            t = render_t(float(row[0]))
        except (TypeError, ValueError, IndexError):
            continue
        if t is None or not (bounds[0] <= float(row[0]) <= bounds[1]):
            problems.append("chapter %r outside the episode, dropped" % (row,))
            continue
        chaps.append([round(float(t), 1), str(row[1]).strip()])
    chaps.sort()
    clean = [chaps[0]]
    for t, lab in chaps[1:]:
        if t - clean[-1][0] >= 10.0 and t < total - OUTRO_D - 5:
            clean.append([t, lab])
    if len(clean) < 3:   # code fallback: evenly spaced, labelled by the first words there
        problems.append("fewer than 3 usable chapters from the model; filled by code")
        for f in (0.33, 0.66):
            src_s = keep[0][0] + f * (bounds[1] - bounds[0])
            t = render_t(src_s)
            if t is not None:
                clean.append([round(float(t), 1), text_between(src_s, src_s + 15)[:40] or "Part %d" % (len(clean) + 1)])
        clean.sort()

    title_core = str(choice.get("title") or "").strip()
    title = "%s Part N - %s%s" % (a.game, title_core, SUFFIX)
    if len(title) > 97:   # "Part N" can become "Part 120"
        problems.append("title too long (%d), trimmed to the core" % len(title))
        title_core = title_core[:max(0, 97 - len(title) + len(title_core))].rsplit(" ", 1)[0]
        title = "%s Part N - %s%s" % (a.game, title_core, SUFFIX)
    desc = str(choice.get("description") or "Part {PART}.").replace("\u2014", ",").replace("\u2013", "-").strip()
    if not desc.startswith("Part {PART}."):
        desc = "Part {PART}. " + re.sub(r"^Part\s+\S+\.\s*", "", desc)
    for bad, rx in (("kick link", r"kick\.com"), ("language warning", r"language|swear|profanity"),
                    ("part count", r"\bof \d+ parts?\b|\d+ parts? (in total|total)")):
        if re.search(rx, desc, re.I):
            sys.exit("ABORT: description breaks the '%s' rule" % bad)
    chapter_block = "\n".join("%s %s" % (mmss(t), lab) for t, lab in clean)
    full_desc = desc + "\n\n" + chapter_block      # ep_publish adds the footer (handles) + marker
    if len(full_desc) > 4300:
        sys.exit("ABORT: description %d chars, no room left for the footer" % len(full_desc))

    stats = {"source_s": round(dur, 1), "bounds": bounds, "window_s": round(bounds[1] - bounds[0], 1),
             "body_s": round(body_len, 1), "cut_s": round(bounds[1] - bounds[0] - body_len, 1),
             "n_cuts_audio": n_audio, "n_cuts_visual": len(vcuts), "n_keep_spans": len(keep),
             "cold_s": round(cold_len, 2), "episode_s": round(total, 2),
             "legal_end_options": len(legal), "menu_candidates": [m_[:2] for m_ in menu_cands],
             "menu_cut": [m_[:2] for m_ in chosen], "menu_kept": [m_[:2] for m_ in menu_kept], "used_model_end": end is not default_end or ei == legal.index(default_end),
             "problems": problems, "llm": cost_line[-300:]}
    plan = {"keep": keep, "cold": list(cold), "title": title_core, "youtube_title": title,
            "sting_name": title_core.upper(), "description": full_desc, "chapters": clean,
            "bounds": list(bounds), "cuts": [c for c in cuts if bounds[0] <= c[0] <= bounds[1]],
            # the NEXT episode must start here: everything after bounds[1] is not in this one
            "next_start": (float(brk[1]) if (brk and end.get("close")) else bounds[1]),
            "tail_unused_s": round(dur - bounds[1], 1), "closes_session": bool(end.get("close")),
            "stats": stats}
    tmp = os.path.join(a.work, "plan.json.part")
    json.dump(plan, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    os.replace(tmp, os.path.join(a.work, "plan.json"))
    print(json.dumps(stats, indent=1))
    print(title)


if __name__ == "__main__":
    main()

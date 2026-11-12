"""Stage 6b: the AI review that stands in for the watch-through (PROOF-PLAN §3).

    python -m polished.review <episode.mp4> <workdir> --brand <name> --budget-limit $ --budget-spent $

<workdir> holds plan.json + analysis.json (the same time base). Exit 0 = review.json written
(read its verdict); 3 = HOLD: the cloud budget cannot pay for the call (prompt + sheets kept, so a
Claude session can review them instead). Never exits 0 without a verdict from a model.

Builds review sheets (one labelled tile every 20 s of the delivered file, 6x6 per image so
each tile survives the API's 1568 px downscale), plus a text pack: title, description,
chapters, and the words either side of every seam. ONE capped Claude call (api-lane,
--task gta6-review-episode) returns {"verdict": "pass"|"fail", "issues": [...]}.

The reviewer RANKS; it never edits. A fail holds the episode (polished only, never a rough
cut). Its claims about the picture are checked by eye before anything is changed (brain:
"never delete good work on a verifier's say-so").
"""
import argparse
import json
import os
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polished import llm  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
TW, TH, LAB, COLS, ROWS = 320, 180, 20, 6, 6
INTRO_D = 4.6


def dur(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                 "csv=p=0", p], capture_output=True, text=True).stdout.strip())


def mmss(t):
    return "%d:%02d" % (int(t) // 60, int(t) % 60)


def sheets(mp4, work, step=20.0):
    d = dur(mp4)
    times, t = [], 1.0
    while t < d - 0.5:
        times.append(t)
        t += step
    tdir = os.path.join(work, "_review_tiles")
    os.makedirs(tdir, exist_ok=True)
    fnt = None
    for fp in (r"C:\Windows\Fonts\arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            fnt = ImageFont.truetype(fp, 14)
            break
        except OSError:
            continue
    fnt = fnt or ImageFont.load_default()
    outs, per = [], COLS * ROWS
    for k in range(0, len(times), per):
        chunk = times[k:k + per]
        rows = (len(chunk) + COLS - 1) // COLS
        sheet = Image.new("RGB", (COLS * TW, rows * (TH + LAB)), (12, 12, 18))
        dr = ImageDraw.Draw(sheet)
        for n, t in enumerate(chunk):
            p = os.path.join(tdir, "t%05d.jpg" % int(t * 10))
            if not os.path.exists(p):
                subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.2f" % t, "-i", mp4, "-frames:v", "1",
                                "-vf", "scale=%d:%d" % (TW, TH), "-y", p], check=True)
            x, y = (n % COLS) * TW, (n // COLS) * (TH + LAB)
            sheet.paste(Image.open(p), (x, y + LAB))
            dr.text((x + 4, y + 3), mmss(t), font=fnt, fill=(140, 255, 220))
        o = os.path.join(work, "review_sheet_%d.jpg" % (k // per + 1))
        sheet.save(o, quality=85)
        outs.append(o)
    return outs, times


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mp4")
    ap.add_argument("work")
    ap.add_argument("--model", default="haiku")   # 10-30: same verdicts as Sonnet on both known cases at 1/15 the cost
    ap.add_argument("--brand", default="")
    ap.add_argument("--budget-limit", type=float, default=0.0)
    ap.add_argument("--budget-spent", type=float, default=0.0)
    a = ap.parse_args()
    plan = json.load(open(os.path.join(a.work, "plan.json"), encoding="utf-8"))
    an = json.load(open(os.path.join(a.work, "analysis.json"), encoding="utf-8"))
    words = sorted((w["s"], w["e"], w["w"].strip()) for s in an["transcript"]["segments"] for w in (s.get("words") or []))
    imgs, times = sheets(a.mp4, a.work)

    keep = plan["keep"]
    cold = plan["cold"]
    cold_len = cold[1] - cold[0]
    pre, acc = [], 0.0
    for s, e in keep:
        pre.append(acc)
        acc += e - s
    seam_lines = []
    for i in range(1, len(keep)):
        t_out = cold_len + INTRO_D + pre[i]
        a_ = " ".join(w for s, e, w in words if keep[i - 1][1] - 6 <= s < keep[i - 1][1])[-90:]
        b_ = " ".join(w for s, e, w in words if keep[i][0] <= s < keep[i][0] + 6)[:90]
        seam_lines.append("%s  removed %.1fs  ...%s || %s..." % (mmss(t_out), keep[i][0] - keep[i - 1][1], a_, b_))
    cold_text = " ".join(w for s, e, w in words if cold[0] <= s < cold[1])

    def render_t(src):
        for i, (s, e) in enumerate(keep):
            if s <= src < e:
                return cold_len + INTRO_D + pre[i] + (src - s)
        return None
    kept_menus = []   # menu screens the planner KEPT on purpose because he talks over them (owner, T2 30 Oct)
    for m0, m1 in (plan.get("stats", {}).get("menu_kept") or []):
        r0, r1 = render_t(max(m0, keep[0][0])), render_t(min(m1, keep[-1][1]) - 0.01)
        if r0 is not None and r1 is not None and r1 > r0:
            kept_menus.append({"render": "%s-%s" % (mmss(r0), mmss(r1)),
                               "his_words": " ".join(w for s, e, w in words if m0 <= s < m1)[:400]})
    body_end = cold_len + INTRO_D + acc
    pack = {
        "title": plan["youtube_title"], "description": plan["description"],
        "cold_open": {"render": "0:00-%s" % mmss(cold_len), "words": cold_text},
        "structure": "cold open 0:00-%s, intro sting %s-%s, body to %s, Twitch outro %s-end" % (
            mmss(cold_len), mmss(cold_len), mmss(cold_len + INTRO_D), mmss(body_end), mmss(body_end)),
        "seams": seam_lines[:80], "n_seams": len(seam_lines),
        "first_words_of_body": " ".join(w for s, e, w in words if keep[0][0] <= s < keep[0][0] + 12),
        "last_words_of_body": " ".join(w for s, e, w in words if keep[-1][1] - 12 <= s < keep[-1][1]),
        "kept_menus": kept_menus,
    }
    prompt = open(os.path.join(HERE, "review_prompt.md"), encoding="utf-8").read()
    prompt = (prompt.replace("{N_IMAGES}", str(len(imgs))).replace("{BRAND}", a.brand or "the streamer")
              .replace("{PACK}", json.dumps(pack, indent=1, ensure_ascii=False)))
    pf = os.path.join(a.work, "review_prompt.txt")
    open(pf, "w", encoding="utf-8").write(prompt)
    of = os.path.join(a.work, "review_llm.json")
    bud = llm.Budget(a.budget_limit, a.budget_spent)
    try:   # Haiku needs 4000 output tokens: it reasons first, and 1500 cut its answer off (10-30)
        raw, c_ = bud.call("gta6-review-episode", prompt, model=a.model, max_tokens=4000, images=imgs)
    except llm.Dry as ex:
        json.dump({"why": str(ex)}, open(os.path.join(a.work, "review_hold.json"), "w", encoding="utf-8"))
        print("HOLD: %s" % ex)
        sys.exit(3)
    json.dump(bud.calls, open(os.path.join(a.work, "review_calls.json"), "w", encoding="utf-8"))
    open(of, "w", encoding="utf-8").write(raw)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        res = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        res = None
    if not isinstance(res, dict) or res.get("verdict") not in ("pass", "fail"):
        json.dump({"why": "review answer unusable"}, open(os.path.join(a.work, "review_hold.json"), "w"))
        print("HOLD: review answer unusable")
        sys.exit(3)
    res["lane"] = "cloud %s $%.4f" % (a.model, c_)
    res["images"] = [os.path.basename(i) for i in imgs]
    json.dump(res, open(os.path.join(a.work, "review.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(json.dumps(res, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()

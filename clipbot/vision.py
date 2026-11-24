"""Vision gate + copy writer for clips (boss 19 Nov, after the quality sample: a title invented over light-switching, 17 s of
a blurred radio wheel; Leon's copy standard in kick-pivot-1117/CLIP-COPY-REVIEW-1119.md).

Before a machine pick is offered, a vision model LOOKS at it: one contact sheet of the cut (8 time-stamped frames) + the
transcript + the proposed title. It rejects what is mostly menu / HUD / radio wheel / blur / black / loading or has no clear
moment, trims a cut whose payoff sits after dead air, and writes ALL the copy from what is on screen:
    "GTA 6: <true moment> | <brand>" title, a one-line first-person description, a CamelCase moment tag, a hook line.

    check(raw_mp4, title, transcript, game=...) -> {"verdict": "pass"|"reject"|"trim"|"unchecked", "reason", "trim",
                                                    "moment", "line", "tag", "hook", "title", "cost", "lane"}
    copy_for(verdict, game, brand) -> the offer's copy fields (description, hashtags, tags, tiktok/ig hook + hashtags)

Models: Gemini on the Vertex lane first (boss 19 Nov: nothing that depends on Claude limits), 3.5 Flash then 3.5 Flash-Lite,
each call worst-case-checked against CLIPBOT_VISION_GEM_BUDGET per run; Haiku (polished.llm) only if Gemini errors.
"unchecked" (no credentials, budget, unusable answer) never auto-publishes: the clip still goes to a human card.
"""
import json
import os
import re
import subprocess
import sys

N_FRAMES = 8
# Boss 19 Nov: copy written by THIS writer (it watched the clip, Leon's 19 Nov standard) carries copy_std = COPY_STD;
# every posting lane (publish, TikTok, Reels, X, Stories) refuses an item without it.
COPY_STD = "1119"
W, H = 480, 270
_spent = {"gemini": 0.0, "claude": 0.0}
MODELS = [m.strip() for m in (os.environ.get("CLIPBOT_VISION_MODELS") or "gemini-3.5-flash,gemini-3.5-flash-lite").split(",")
          if m.strip()]
GEM_CAP = float(os.environ.get("CLIPBOT_VISION_GEM_BUDGET") or 0.60)       # $ of Gemini per run (worst-case checked)
CLAUDE_CAP = float(os.environ.get("CLIPBOT_VISION_BUDGET") or 0.02)        # $ of Haiku per run, fallback only

# Leon 19 Nov: a character is named in copy ONLY if it is in this list AND the clip shows it (a name tag, a subtitle, a
# line that says it). Anything else says "the girl", "a cop", "the guy". The model lists what it actually saw in names_seen.
CAST = ["Jason Duval", "Lucia Caminos", "Cal Hampton", "Boobie Ike", "Dre'Quan Priest", "Raul Bautista", "Brian Heder", "Real Dimez"]

PROMPT = """You are checking one short gaming clip before it is posted as a YouTube Short / TikTok / Reel. The image is a
contact sheet of the clip: {n} frames left-to-right, top-to-bottom, each stamped with its time in seconds. Length: {dur:.1f} s.
Game (from the stream): "{game}".
What the streamer says (auto transcript, may have errors; game dialogue may be mixed in): "{transcript}"
Proposed title: "{title}"

Decide from what you SEE first and the words second:
- reject if most frames are a menu, map, inventory, settings, a radio/weapon wheel, a loading or black screen, heavy blur,
  or a static/pause screen; or if there is no clear moment worth watching. Plain driving, walking, cruising, flying,
  sightseeing or phone browsing with no fail, surprise, funny line or reaction, skilful play or story payoff is a reject,
  however clean the gameplay looks. Pass only what someone scrolling Shorts would stop for.
- trim if the good part is real but most of the clip is dead air or menus around it: give start and end seconds of the part
  worth keeping (at least 8 s, inside 0..{dur:.1f}).
- otherwise pass.
Then write the copy (for pass or trim), all of it TRUE to what is visibly on screen, in the streamer's own voice:
- moment: the title after the game name: what visibly happens, specific (vehicle, animal, place, character), first person
  where it fits ("I Ran Over A Tiger On The Freeway"), Title Case, max 55 characters, no all-caps words, no third person,
  no emojis, no swearing, never a fragment of the transcript.
- line: ONE first-person sentence about the moment for the YouTube description, casual, at most one emoji, no swearing,
  no content warning.
- Characters: name one ONLY if it is in this list: {cast}, AND its name or voice is clearly shown in the clip (a name tag,
  a subtitle, someone saying it). Otherwise describe them ("the girl", "a cop"), never guess or invent a name.
  names_seen: the list names you based that on ([] if none).
- tag: ONE CamelCase hashtag for this moment (e.g. "#TigerRunOver"), max 25 characters.
- hook: a short caption line for TikTok / Reels / X that makes people watch: a reaction, a quote or a question; max 90
  characters, at most one emoji, no hashtags, no swearing.

Reply with JSON only:
{{"verdict": "pass|reject|trim", "reason": "one short sentence", "trim": [start, end] or null,
  "moment": "...", "line": "...", "tag": "#...", "hook": "...", "names_seen": []}}"""


def sheet(mp4, out_jpg, dur=None):
    """8 time-stamped frames of the clip in one 4x2 jpg."""
    from PIL import Image, ImageDraw
    if dur is None:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", mp4],
                                   capture_output=True, text=True, check=True).stdout.strip() or 0)
    cols, rows = 4, 2
    # tiles take the clip's own shape: a vertical Short in 16:9 tiles left black side bars that the model read as
    # "black frames" and rejected good clips for (19 Nov tidy-old run, 8 of 8 'black' rejects were this)
    wh = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", mp4], capture_output=True, text=True).stdout.strip().split(",")
    tw, th = W, H
    if len(wh) >= 2 and wh[0].isdigit() and wh[1].isdigit() and int(wh[1]) > int(wh[0]):
        tw, th = H, W   # 9:16 tiles (270x480), the same pixels per frame as the 16:9 ones
    im = Image.new("RGB", (cols * tw, rows * th))
    for i in range(N_FRAMES):
        t = dur * (i + 0.5) / N_FRAMES
        fp = out_jpg + f".f{i}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", mp4, "-frames:v", "1",
                        "-vf", f"scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2", fp],
                       check=True, capture_output=True)
        fr = Image.open(fp).convert("RGB")
        d = ImageDraw.Draw(fr)
        d.rectangle([0, 0, 64, 20], fill="black")
        d.text((4, 4), f"{t:.1f}s", fill="yellow")
        im.paste(fr, ((i % cols) * tw, (i // cols) * th))
        os.remove(fp)
    im.save(out_jpg, quality=85)
    return dur


def _json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    for cand in (m.group(0), re.sub(r",\s*([}\]])", r"\1", m.group(0))):   # a trailing comma is the usual slip
        try:
            return json.loads(cand)
        except ValueError:
            continue
    return None


def game_name(hint):
    """'GTA 6' for any GTA VI hint (category or stream title); another short category name as it is; else ''."""
    h = str(hint or "").lower()
    if re.search(r"\bgta\s*(6|vi)\b", h) or ("grand theft auto" in h and re.search(r"\b(vi|6)\b", h)):
        return "GTA 6"
    h = str(hint or "").strip()
    return h if 2 <= len(h) <= 30 else ""


def game_tags(game):
    if game == "GTA 6":
        return ["#GTA6", "#GTAVI"]
    t = re.sub(r"[^A-Za-z0-9]", "", game.title() if game.islower() else game)
    return ["#" + t] if 2 <= len(t) <= 30 else []


def _clean(text, n):
    return re.sub(r"\s+", " ", str(text or "")).strip().strip('"')[:n].rstrip()


def parse(text, dur):
    """The model's JSON -> a checked verdict dict, or None if unusable."""
    d = _json(text)
    if not isinstance(d, dict):
        return None
    v = str(d.get("verdict", "")).lower().strip()
    if v not in ("pass", "reject", "trim"):
        return None
    trim = None
    if v == "trim":
        try:
            a, b = (float(x) for x in d.get("trim") or [])
            a, b = max(0.0, a), min(dur, b)
            if b - a >= 8.0:
                trim = [round(a, 2), round(b, 2)]
        except (TypeError, ValueError):
            trim = None
        if trim is None:
            v = "pass"                       # an unusable trim range: keep the whole cut, a human still sees the card
    moment = _clean(d.get("moment") or d.get("title"), 55)
    if moment and moment.upper() == moment and len(moment) > 6:
        moment = moment.title()             # no all-caps titles
    tag = _clean(d.get("tag"), 26)
    tag = ("#" + re.sub(r"[^A-Za-z0-9_]", "", tag.lstrip("#"))) if tag else ""
    return {"verdict": v, "reason": _clean(d.get("reason"), 200), "trim": trim, "moment": moment or None,
            "line": _clean(d.get("line"), 220) or None, "tag": tag if len(tag) > 2 else None,
            "hook": _clean(d.get("hook"), 90) or None,
            "names_seen": [n for n in (d.get("names_seen") or []) if isinstance(n, str)][:8]}


def unseen_names(vz):
    """Cast names (full, or first/last when unambiguous) written in the copy but not in vz['names_seen']."""
    text = " ".join(str(vz.get(k) or "") for k in ("moment", "line", "hook", "tag")).lower()
    seen = " ".join(vz.get("names_seen") or []).lower()
    bad = []
    for full in CAST:
        parts = [full.lower()] + [w.lower() for w in re.split(r"\s+", full) if len(w) > 2]
        hit = [x for x in parts if re.search(r"(?<![a-z])" + re.escape(x) + r"(?![a-z])", text)]
        if hit and not any(x in seen for x in parts):
            bad.append(full)
    return bad


def copy_for(vz, game, brand):
    """Leon's 19 Nov standard from one verdict -> the offer's copy fields (title + YouTube + TikTok + Reels)."""
    if not vz or not vz.get("moment"):
        return None
    if unseen_names(vz):   # a cast name in the copy the clip does not show = no copy (fail closed, a human re-copies)
        return None
    tags = game_tags(game)
    moment_tag = [vz["tag"]] if vz.get("tag") else []
    brand_tag = "#" + re.sub(r"[^A-Za-z0-9]", "", brand or "")
    low = list(dict.fromkeys([t.lower() for t in tags + moment_tag] + ["#gaming", brand_tag.lower()]))
    hook = vz.get("hook") or vz["moment"]
    if game:   # the model sometimes repeats the game name ("Cyberpunk 2077: Cyberpunk 2077: ...")
        vz = {**vz, "moment": re.sub(r"^\s*(" + re.escape(game) + r"|GTA ?(6|VI))\s*[:\-|]\s*", "", vz["moment"], flags=re.I) or vz["moment"]}
    return {
        "title": f"{game}: {vz['moment']}" if game else vz["moment"],
        "description": vz.get("line") or "",
        "hashtags": tags + moment_tag + ["#Shorts", brand_tag],
        "tags": [x for x in [game, "GTA VI" if game == "GTA 6" else "", vz["moment"],
                             f"{game} funny moments" if game else "funny moments", brand] if x],
        "game": game,
        "tiktok_hook": hook, "tiktok_hashtags": low[:5],
        "ig_hook": hook, "ig_hashtags": low[:5],
    }


def _ask(prompt, jpg):
    """-> (text, cost, lane). Gemini models in order, each worst-case-checked against GEM_CAP; then Haiku (CLAUDE_CAP)."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    errs = []
    try:
        from polished import gemini
        for mid in MODELS:
            if gemini.price_of(mid) is None:
                errs.append(f"no price for {mid}")
                continue
            worst = gemini.worst_case(mid, prompt, 1, 700)
            if _spent["gemini"] + worst > GEM_CAP:
                errs.append(f"gemini cap ${GEM_CAP:.2f} reached")
                break
            try:
                text, usage, finish, ver = gemini.generate("clip-vision-review", prompt, mid, 700, images=[jpg])
            except gemini.GeminiDry as e:
                errs.append(f"{mid}: {str(e)[:120]}")
                continue
            c = gemini.cost(mid, usage)
            _spent["gemini"] += c
            if _json(text) is not None:
                return text, c, "gemini " + mid
            errs.append(f"{mid}: answer not JSON")
    except Exception as e:   # noqa: BLE001
        errs.append(f"gemini: {type(e).__name__}: {str(e)[:120]}")
    from polished import llm
    b = llm.Budget(max(0.0, CLAUDE_CAP - _spent["claude"]))
    text, c = b.call("clip-vision-check", prompt, model="haiku", max_tokens=700, images=[jpg])
    _spent["claude"] += c
    return text, c, "haiku (gemini: " + "; ".join(errs)[:160] + ")"


def check(raw_mp4, title, transcript, workdir=None, game=""):
    workdir = workdir or os.path.dirname(os.path.abspath(raw_mp4))
    jpg = os.path.join(workdir, os.path.basename(raw_mp4) + ".sheet.jpg")
    out = {"verdict": "unchecked", "reason": "", "trim": None, "moment": None, "line": None, "tag": None, "hook": None,
           "title": None, "cost": 0.0, "lane": None}
    try:
        dur = sheet(raw_mp4, jpg)
        prompt = PROMPT.format(n=N_FRAMES, cast=", ".join(CAST), dur=dur, game=str(game or "unknown").replace('"', "'"),
                               transcript=str(transcript or "")[:1200].replace('"', "'"),
                               title=str(title or "").replace('"', "'"))
        text, cost, lane = _ask(prompt, jpg)
        out.update(cost=round(cost, 5), lane=lane)
        d = parse(text, dur)
        if d is None:
            out["reason"] = "the vision answer was not usable JSON"
        else:
            out.update(d)
            out["title"] = d.get("moment")
    except Exception as e:   # noqa: BLE001 - unchecked, never auto: a human card still decides
        out["reason"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out


def trim_file(raw_mp4, a, b, out_mp4):
    """Re-encode [a, b] of the cut (frame-accurate; it is rendered again afterwards anyway)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", raw_mp4,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-b:a", "192k", out_mp4],
                   check=True, capture_output=True)


if __name__ == "__main__":
    # py -m clipbot.vision <clip.mp4> "<title>" "<transcript>" "<game>"
    a = sys.argv + ["", "", "", ""]
    print(json.dumps(check(a[1], a[2], a[3], game=game_name(a[4])), ensure_ascii=False))

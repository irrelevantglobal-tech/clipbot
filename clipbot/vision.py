"""Vision check for machine picks (boss 19 Nov, after the quality sample: a title invented over light-switching, 17 s of
a blurred radio wheel). Before a machine pick is offered, a cheap vision model LOOKS at it: one contact sheet of the cut
(8 frames, time-stamped) + the transcript + the proposed title. It rejects what is mostly menu / HUD / radio wheel / blur /
black / loading, rejects a title that is not visibly true, and trims a cut whose payoff sits at the very end after dead air.
The title it returns describes what is ON SCREEN (the transcript only helps), never transcript fragments alone.

    check(raw_mp4, title, transcript) -> {"verdict": "pass"|"reject"|"trim"|"unchecked", "reason": str,
                                          "title": str|None, "trim": [start_s, end_s]|None, "cost": float}

"unchecked" (no key, budget, API error, unreadable answer) never auto-publishes: the clip still goes to a human card.
Model: Haiku via polished.llm (GTA6_ANTHROPIC_KEY, capped by CLIPBOT_VISION_BUDGET per run, default $0.05).
"""
import json
import os
import re
import subprocess
import sys

N_FRAMES = 8
W, H = 480, 270
_budget = None

PROMPT = """You are checking one short gaming clip before it is posted as a YouTube Short / TikTok. The image is a contact
sheet of the clip: {n} frames left-to-right, top-to-bottom, each stamped with its time in seconds. Length: {dur:.1f} s.

What the streamer says (auto transcript, may have errors; game dialogue may be mixed in): "{transcript}"
Proposed title: "{title}"

Decide, from what you SEE first and the words second:
- reject if most of the frames are a menu, map, inventory, settings, a radio/weapon wheel, a loading or black screen,
  heavy blur, or a static/pause screen; or if nothing visibly happens worth watching.
- reject if the proposed title claims something that is not visible on screen (e.g. "blew up my car" with no car or
  explosion in any frame) AND you cannot write a true title for what is shown.
- trim if the good part is real but the clip spends most of its length on dead air or menus before or after it: give
  the start and end second of the part worth keeping (at least 8 s, inside 0..{dur:.1f}).
- otherwise pass.
Always write a title that is TRUE to what is on screen: what visibly happens, specific (the vehicle, animal, place,
character), first person where it fits ("I Ran Over A Tiger On The Freeway"), Title Case, max 50 characters,
no emojis, no hashtags, no swearing, never just a fragment of the transcript.

Reply with JSON only: {{"verdict": "pass|reject|trim", "reason": "one short sentence", "title": "...", "trim": [start, end] or null}}"""


def sheet(mp4, out_jpg, dur=None):
    """8 time-stamped frames of the clip in one 4x2 jpg."""
    from PIL import Image, ImageDraw
    if dur is None:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", mp4],
                                   capture_output=True, text=True, check=True).stdout.strip() or 0)
    cols, rows = 4, 2
    im = Image.new("RGB", (cols * W, rows * H))
    for i in range(N_FRAMES):
        t = dur * (i + 0.5) / N_FRAMES
        fp = out_jpg + f".f{i}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", mp4, "-frames:v", "1",
                        "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2", fp],
                       check=True, capture_output=True)
        fr = Image.open(fp).convert("RGB")
        d = ImageDraw.Draw(fr)
        d.rectangle([0, 0, 64, 20], fill="black")
        d.text((4, 4), f"{t:.1f}s", fill="yellow")
        im.paste(fr, ((i % cols) * W, (i // cols) * H))
        os.remove(fp)
    im.save(out_jpg, quality=85)
    return dur


def parse(text, dur):
    """The model's JSON -> a checked verdict dict, or None if unusable."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    v = str(d.get("verdict", "")).lower().strip()
    if v not in ("pass", "reject", "trim"):
        return None
    title = re.sub(r"\s+", " ", str(d.get("title") or "")).strip().strip('"')[:50] or None
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
    return {"verdict": v, "reason": str(d.get("reason") or "")[:200], "title": title, "trim": trim}


def budget():
    global _budget
    if _budget is None:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from polished import llm
        _budget = llm.Budget(float(os.environ.get("CLIPBOT_VISION_BUDGET") or 0.05))
    return _budget


def check(raw_mp4, title, transcript, workdir=None):
    workdir = workdir or os.path.dirname(os.path.abspath(raw_mp4))
    jpg = os.path.join(workdir, os.path.basename(raw_mp4) + ".sheet.jpg")
    out = {"verdict": "unchecked", "reason": "", "title": None, "trim": None, "cost": 0.0}
    try:
        dur = sheet(raw_mp4, jpg)
        prompt = PROMPT.format(n=N_FRAMES, dur=dur, transcript=str(transcript or "")[:1200].replace('"', "'"),
                               title=str(title or "").replace('"', "'"))
        text, cost = budget().call("clip-vision-check", prompt, model="haiku", max_tokens=400, images=[jpg])
        out["cost"] = round(cost, 5)
        d = parse(text, dur)
        if d is None:
            out["reason"] = "the vision answer was not usable JSON"
        else:
            out.update(d)
    except Exception as e:   # noqa: BLE001 - unchecked, never auto: a human card still decides
        out["reason"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out


def trim_file(raw_mp4, a, b, out_mp4):
    """Re-encode [a, b] of the cut (frame-accurate; it is rendered again afterwards anyway)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", raw_mp4,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-b:a", "192k", out_mp4],
                   check=True, capture_output=True)


if __name__ == "__main__":
    # py -m clipbot.vision <clip.mp4> "<title>" "<transcript>"
    print(json.dumps(check(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "", sys.argv[3] if len(sys.argv) > 3 else ""),
                     ensure_ascii=False))

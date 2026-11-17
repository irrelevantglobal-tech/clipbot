"""Kick viewer clips -> vertical Shorts, offered for approval like Twitch clips (owner 17 Nov: "the clip thing too").
Nothing is uploaded here: publish.py does that, only for clips he approves.

    python -m clipbot.kick_clips --out out                 new clips of the last HOURS, claimed with the worker
    python -m clipbot.kick_clips --out out --clips ID,ID   a re-run of named clips (no claim)

The Forge worker cannot read Kick's public JSON (Cloudflare answers Workers with 429), so this job lists the clips
itself (polished/source.py: KICK_SLUG, checked against KICK_CHANNEL_ID) and asks the worker which ones are new
(POST /clipbot/kickclips: it claims them, so two runs never offer one clip twice). A clip is offered once it is
SETTLE_S old (its title is final by then). Same render, copy, encrypted release and cards as twitch_clips.py.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import common, render
from .twitch_clips import clean_emoji
from polished import source  # noqa: E402

SETTLE_S = 15 * 60
HOURS = 48          # Kick keeps an unverified channel's VODs 7 days; clips are listed a week back, offered from 48 h
MAX_PER_RUN = 10    # one approval batch (as Twitch); the rest are claimed by the next run


def claim(clips):
    """The worker's answer: which of these clip ids have never been offered (and are now claimed)."""
    r = common.worker("/clipbot/kickclips", {"clips": [{"id": c["id"], "created": int(c["created"])} for c in clips]})
    fresh = set(r.get("fresh") or [])
    return [c for c in clips if c["id"] in fresh]


def download(url, out):
    """The clip's own HLS playlist on clips.kick.com -> mp4 (stream copy)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", out], check=True, timeout=300)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--clips", default="", help="re-run: comma-separated Kick clip ids (no claim)")
    ap.add_argument("--hours", type=float, default=HOURS)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    now = time.time()
    listed = source.kick_clips(now - 7 * 86400 if a.clips else now - a.hours * 3600)
    if a.clips:
        want = {x for x in a.clips.split(",") if re.fullmatch(r"[A-Za-z0-9_-]{5,120}", x)}
        todo = [c for c in listed if c["id"] in want][:MAX_PER_RUN]
    else:
        ripe = [c for c in listed if now - c["created"] >= SETTLE_S][:MAX_PER_RUN]
        print(f"{len(listed)} clip(s) in the last {a.hours:g} h, {len(ripe)} settled")
        todo = claim(ripe) if ripe else []
    batch = str(int(now))
    clips = []
    for c in todo:
        n = len(clips) + 1
        raw = os.path.join(a.out, f"raw{n}.mp4")
        tmp = raw + ".part.mp4"
        try:
            download(c["url"], tmp)
            render.require_1080(tmp, f"Kick clip {c['id'][:30]}")
        except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as ex:   # one bad clip is skipped
            print(f"{c['id']}: {str(ex)[:200]}; skipped")
            if os.path.exists(tmp):
                os.remove(tmp)
            continue
        shutil.move(tmp, raw)
        dur = render.duration(raw)
        if dur > 70:                                # a "clip" file has been a whole VOD before (brain)
            print(f"{c['id']}: {dur:.0f}s is not a clip, skipped")
            os.remove(raw)
            continue
        words = render.transcribe(raw)
        hook = clean_emoji(c["title"]) if c["title"].strip() else "Kick clip"
        info = render.render(raw, os.path.join(a.out, f"clip{n}.mp4"), os.path.join(a.out, f"preview{n}.mp4"), hook=hook, words=words)
        copy = common.clip_copy(hook, game="", transcript=" ".join(w["w"] for w in words))
        clips.append({"n": n, "slug": c["id"], "title": hook, "creator": c["creator"], "start": 0, "end": info["duration"],
                      **copy, **info})
        print(f"clip {n}: {c['id'][:30]} {info['duration']:.0f}s by {c['creator'] or '?'} tags={copy['hashtags']}")
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
        f.write(f"batch={batch if clips else ''}\n")
    if not clips:
        print("no new Kick clips to offer" + (f" ({len(todo)} claimed but none usable)" if todo else ""))
        return 0
    with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"vod": batch, "kind": "clips", "platform": "kick", "vod_title": "Kick clips",
                   "vod_created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(todo[0]["created"])), "clips": clips},
                  f, ensure_ascii=False, indent=1)
    print(f"batch {batch}: {len(clips)} Kick clip(s) ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())

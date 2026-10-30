"""Render new Twitch clips as vertical Shorts and send them to Leon for approval.
Nothing is uploaded here: publish.py does that, only for clips he approves.

    python -m clipbot.twitch_clips --clips SlugA,SlugB --out out
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


def clean_emoji(t):
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️‍]+", " ", t)
    t = re.sub(r"#\w+", "", t)
    return re.sub(r"\s+", " ", t).strip() or "Twitch clip"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", required=True)
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    ids = [x for x in a.clips.split(",") if re.fullmatch(r"[A-Za-z0-9_-]{5,120}", x)][:10]
    if not ids:
        raise SystemExit("no valid clip ids")
    data = common.helix("clips?" + "&".join(f"id={i}" for i in ids))["data"]
    if common.WHO != "leon":                         # only this person's own clips go on his channel
        data = [c for c in data if c.get("broadcaster_id") == common.TWITCH_UID]
    games = {}
    gids = sorted({c["game_id"] for c in data if c.get("game_id")})
    if gids:
        games = {g["id"]: g["name"] for g in common.helix("games?" + "&".join(f"id={g}" for g in gids))["data"]}

    clips = []
    for c in sorted(data, key=lambda x: x["created_at"]):
        n = len(clips) + 1
        raw = os.path.join(a.out, f"raw{n}.mp4")
        tmp = raw + ".part.mp4"
        subprocess.run(["yt-dlp", "-q", "--no-progress", "-f", "best[height<=1080]", "-o", tmp, c["url"]], check=True)
        shutil.move(tmp, raw)
        dur = render.duration(raw)
        if dur > 70:                               # brain: a "clip" file has been a whole VOD before
            print(f"{c['id']}: {dur:.0f}s is not a clip, skipped")
            os.remove(raw)
            continue
        words = render.transcribe(raw)
        hook = clean_emoji(c["title"])
        info = render.render(raw, os.path.join(a.out, f"clip{n}.mp4"), os.path.join(a.out, f"preview{n}.mp4"), hook=hook, words=words)
        game = games.get(c.get("game_id"), "")
        copy = common.clip_copy(hook, game=game, transcript=" ".join(w["w"] for w in words))
        clips.append({"n": n, "slug": c["id"], "title": hook, "creator": c.get("creator_name", ""),
                      "start": 0, "end": info["duration"], **copy, **info})
        print(f"clip {n}: {c['id'][:30]} {info['duration']:.0f}s game={copy['game'] or '?'} tags={copy['hashtags']}")
    if not clips:
        raise SystemExit("nothing to offer")
    batch = str(int(time.time()))
    json.dump({"vod": batch, "kind": "clips", "vod_title": "Twitch clips", "vod_created": data[0]["created_at"], "clips": clips},
              open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
        f.write(f"batch={batch}\n")
    print(f"batch {batch}: {len(clips)} clips ready")


if __name__ == "__main__":
    sys.exit(main())

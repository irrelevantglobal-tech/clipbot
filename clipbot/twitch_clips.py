"""Render new Twitch clips as vertical Shorts and send them to the owner for approval.
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

from . import common, render, vision


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
        subprocess.run(["yt-dlp", "-q", "--no-progress", "-f", "best[height=1080]/best[height<=1080]", "-o", tmp, c["url"]], check=True)
        try:
            render.require_1080(tmp, f"Twitch clip {c['id'][:30]}")
        except RuntimeError as ex:                 # one soft clip is skipped, the rest still go
            print(f"{ex}; skipped")
            os.remove(tmp)
            continue
        shutil.move(tmp, raw)
        dur = render.duration(raw)
        if dur > 70:                               # brain: a "clip" file has been a whole VOD before
            print(f"{c['id']}: {dur:.0f}s is not a clip, skipped")
            os.remove(raw)
            continue
        words = render.transcribe(raw)
        viewer_title = clean_emoji(c["title"]).strip()
        # Boss 19 Nov: same rule as kick_clips.py. Game = Twitch's own game for the clip; title + burned-in hook ONLY from the
        # vision writer; the viewer's clip title is never public text. No vision copy = no hook, no copy_std, never posted.
        raw_game = games.get(c.get("game_id"), "")
        game = vision.game_name(raw_game) or raw_game.strip()
        vz = vision.check(raw, viewer_title, " ".join(w["w"] for w in words), workdir=a.out, game=game)
        vcopy = vision.copy_for(vz, game, common.BRAND) if game and vz["verdict"] in ("pass", "trim") else None
        if vcopy and vz["verdict"] == "trim" and vz["trim"]:
            ta, tb = vz["trim"]
            vision.trim_file(raw, ta, tb, raw + ".trim.mp4")
            shutil.move(raw + ".trim.mp4", raw)
            words = [{**w, "s": round(w["s"] - ta, 2), "e": round(w["e"] - ta, 2)} for w in words if ta <= w["s"] < tb]
        info = render.render(raw, os.path.join(a.out, f"clip{n}.mp4"), os.path.join(a.out, f"preview{n}.mp4"),
                             hook=vcopy["title"] if vcopy else "", words=words)
        copy = ({**{k: x for k, x in vcopy.items() if k != "title"}, "copy_std": vision.COPY_STD} if vcopy
                else {"description": "", "hashtags": [], "tags": [], "game": game})
        clips.append({"n": n, "slug": c["id"], "title": vcopy["title"] if vcopy else "No copy yet", "viewer_title": viewer_title,
                      "creator": c.get("creator_name", ""), "start": 0, "end": info["duration"],
                      "vision": {k: vz[k] for k in ("verdict", "reason", "cost", "lane")}, **copy, **info})
        print(f"clip {n}: {c['id'][:30]} {info['duration']:.0f}s game={game or '?'} vision={vz['verdict']} tags={copy.get('hashtags')}")
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

"""Instagram Story for every new episode once it is public on YouTube.

    python -m clipbot.ep_story                    (EPS_DRY=1: everything except the final media_publish)
    python -m clipbot.ep_story --sample VIDEO_ID --out story.jpg     (render only, nothing posted)
    python -m clipbot.ep_story --check VIDEO_ID                      (render + Instagram processes it, NOT published)

Owner 2026-11-14: "an instagram story uploaded when new episodes go out too? like the thumbnail and something saying
new episode go check it out". Runs in the hourly Instagram job, only when the repo variable EP_STORY is "1".
Work list: our own recent uploads, long-form (> 3 min), PUBLIC, made public in the last FRESH hours (so switching it
on never posts a backlog), not sponsored. Done list: the worker's KV (/clipbot/epstory), checked first, written
right after posting. The API cannot add a link sticker, so the card says LINK IN BIO.
"""
import argparse
import os
import sys
import urllib.request
from datetime import datetime, timedelta

from . import common, instagram, legacy

FRESH = timedelta(hours=6)
MAX_PER_RUN = 1
SPONSORED = ("#ad", "#sponsored", "paid promotion", "sponsored by")


def thumb_url(v):
    t = v["snippet"].get("thumbnails", {})
    for k in ("maxres", "standard", "high"):
        if t.get(k, {}).get("url"):
            return t[k]["url"]
    return f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg"


def render(v, out):
    from .style import gta_style
    src = out + ".thumb.jpg"
    req = urllib.request.Request(thumb_url(v), headers={"User-Agent": "ClipBot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(src, "wb") as f:
        f.write(r.read())
    tmp = out + ".png"
    gta_style.episode_story(src, out=tmp, seed=sum(map(ord, v["id"])))
    from PIL import Image
    Image.open(tmp).convert("RGB").save(out, "JPEG", quality=90)   # Instagram image Stories take JPEG only
    return out


def new_episodes(uploads, now=None):
    now = now or datetime.now(common.UTC)
    out = []
    for v in uploads:
        st, sn = v["status"], v["snippet"]
        if st.get("privacyStatus") != "public" or common._iso_dur(v["contentDetails"].get("duration")) <= 180:
            continue
        text = (sn.get("title", "") + " " + sn.get("description", "")).lower()
        if any(s in text for s in SPONSORED):
            continue
        at = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
        if now - at <= FRESH:
            out.append(v)
    return sorted(out, key=lambda v: v["snippet"]["publishedAt"])


def post_story(tok, path, dry):
    parked = instagram.park(path)
    try:
        c = instagram.graph(f"/{tok['user_id']}/media", tok["token"], {"media_type": "STORIES", "image_url": parked["url"] + ".jpg"}, post=True)
        instagram.wait_finished(tok, c["id"])
        if dry:
            return None
        return instagram.publish_container(tok, c["id"])[0]
    finally:
        instagram.unpark([parked])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample")
    ap.add_argument("--out", default="story.jpg")
    ap.add_argument("--check")
    a = ap.parse_args()
    if a.check:
        v = common.yt("videos", part="snippet,status,contentDetails", id=a.check)["items"][0]
        os.makedirs("work", exist_ok=True)
        post_story(instagram.login(), render(v, os.path.abspath(f"work/story-{v['id']}.jpg")), dry=True)
        print(f"{v['id']}: Story container FINISHED on Instagram, not published ({v['snippet']['title']})")
        return
    if a.sample:
        v = common.yt("videos", part="snippet,status,contentDetails", id=a.sample)["items"][0]
        print(render(v, os.path.abspath(a.out)), v["snippet"]["title"])
        return
    if common.WHO != "leon":
        return
    dry = os.environ.get("EPS_DRY") == "1"
    eps = new_episodes(common.recent_uploads(25))
    done = set(common.worker("/clipbot/epstory").get("ids", []))
    todo = [v for v in eps if v["id"] not in done]
    print(f"{len(eps)} episodes public in the last {FRESH.seconds // 3600} h, {len(todo)} without a Story yet")
    if not todo:
        return
    tok = instagram.login()
    work = os.path.abspath("work")
    os.makedirs(work, exist_ok=True)
    for v in todo[:MAX_PER_RUN]:
        title = v["snippet"]["title"]
        try:
            legacy.check(title, where=v["id"])
            path = render(v, os.path.join(work, f"story-{v['id']}.jpg"))
            sid = post_story(tok, path, dry)
            if sid is None:
                print(f"{v['id']}: DRY, Story container FINISHED, not published ({title})")
                continue
            common.worker("/clipbot/epstory", {"id": v["id"], "story": sid})
            print(f"{v['id']}: Story {sid} for '{title}'")
        except Exception as e:
            print(f"{v['id']}: FAILED {str(e)[:300]}")
            common.worker("/clipbot/failed", {"id": f"Episode Story {v['id']}", "error": f"'{title[:80]}': {str(e)[:220]}"})
            raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())

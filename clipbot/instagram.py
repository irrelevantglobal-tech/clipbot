"""Post approved Shorts to Instagram as Reels (and the same clip as a Story), through Meta's Instagram API (Instagram Login).

    python -m clipbot.instagram         (IG_DRY=1: everything except the final media_publish)

Leon 2026-10-29: "set up the clipbot so it posts [Reels]" on the new @twinkiesghost (Creator).
One Discord approval covers YouTube, TikTok and Reels. The API cannot schedule, so a Reel goes out
once its YouTube slot time has passed (cron just after each slot, see instagram.yml).

Work list: the worker's /clipbot/igqueue (every Short publish.py scheduled on YouTube).
Login: the long-lived Instagram token lives in the worker's KV (ig_login.py puts it there), never in
the repo. It is refreshed here once it is a week old; a dead token stops the run and tells Leon.
Meta fetches the video from a URL, so the mp4 is parked on the worker for a day under a random key
and deleted after publishing.
Idempotent: before posting, Instagram's own list of recent posts is checked for the same caption.
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta

from . import common, publish, render, tiktok

GRAPH = "https://graph.instagram.com/v26.0"   # replaced by the host stored with the token
MAX_PER_RUN = 2                       # never dump a backlog: the next cron run takes the rest
STALE = timedelta(days=3)             # a slot this old is skipped, not posted late
REFRESH_AFTER = timedelta(days=7)
MAX_BYTES = 24 * 1024 * 1024          # worker KV value limit is 25 MB


class LoginNeeded(Exception):
    pass


def graph(path, token, params=None, post=False, retries=3):
    q = dict(params or {}, access_token=token)
    if post:      # token in the body, never in a URL that could reach a log
        return common.http(GRAPH + path, urllib.parse.urlencode(q).encode(), retries=retries,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
    return common.http(GRAPH + path + "?" + urllib.parse.urlencode(q), retries=retries)


def use_host(t):
    """Unified-login (system-user) tokens live on graph.facebook.com and never expire."""
    global GRAPH
    GRAPH = t.get("host") or "https://graph.instagram.com/v26.0"
    return "graph.facebook.com" in GRAPH


def login():
    t = common.worker("/clipbot/igtoken")
    if not t.get("token"):
        raise LoginNeeded("no Instagram login saved")
    if use_host(t):
        return t
    age = datetime.now(common.UTC) - datetime.fromisoformat(t["at"].replace("Z", "+00:00"))
    if age > REFRESH_AFTER:
        try:
            r = common.http("https://graph.instagram.com/refresh_access_token?" + urllib.parse.urlencode(
                {"grant_type": "ig_refresh_token", "access_token": t["token"]}), retries=1)
        except RuntimeError as e:
            raise LoginNeeded(f"Instagram login could not be refreshed ({str(e)[:120]})")
        t["token"] = r["access_token"]
        common.worker("/clipbot/igtoken", {"token": t["token"], "user_id": t["user_id"], "username": t.get("username", "")})
        print(f"Instagram token refreshed (was {age.days} days old)")
    return t


def ig_caption(hook, tags):
    own = "#" + common.LOGIN
    tags = list(dict.fromkeys([*tags, own]))[:5]
    return f"{hook}\n\nLive on Twitch: {common.LOGIN}\n\n{' '.join(tags)}".strip()[:2200]


def caption_for(it, work=None):
    """Leon's own IG text (edited in Discord) or the IG copy written at approval; clips approved before that get
    fresh Instagram copy written now from what is said in the clip. Last resort: the TikTok caption."""
    if it.get("ig_caption"):
        return it["ig_caption"][:2200]
    if work:
        c, vod_title = common.manifest_clip(it, work)
        quote = c.get("quote") or ""
        if quote:
            cp = common.clip_copy(it["title"], game=it.get("game") or c.get("game", ""), stream_title=vod_title, transcript=quote)
            if cp["ig_hook"]:
                return ig_caption(cp["ig_hook"], cp["ig_hashtags"])
    cap = tiktok.caption_for(it)
    hook, _, tags = cap.partition("\n\n")
    return ig_caption(hook, [t for t in tags.split() if t.lower() not in common.GENERIC_TAGS])


def small_enough(path, work):
    if os.path.getsize(path) <= MAX_BYTES:
        return path
    out = os.path.join(work, os.path.basename(path).replace(".mp4", "-ig.mp4"))
    # 2026-10-30: crf 24 + 6M cap still gave 32 MB on a busy clip, so cap the bitrate from the length
    # (90% of the limit, minus audio). Clips are <= 58 s, so this stays around 3 Mbps or more.
    kbps = max(int(MAX_BYTES * 8 * 0.9 / max(render.duration(path), 1) / 1000) - 128, 800)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-c:v", "libx264", "-preset", "medium",
                    "-crf", "24", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", out], check=True)
    if os.path.getsize(out) > MAX_BYTES:
        raise RuntimeError(f"clip is {os.path.getsize(out) >> 20} MB even re-encoded")
    return out


def park(path):
    with open(path, "rb") as f:
        data = f.read()
    return common.http(common.WORKER + "/clipbot/igfile", data, timeout=300,
                       headers={"Authorization": "Bearer " + common.env("CLIPBOT_TOKEN"), "Content-Type": "application/octet-stream"})


def wait_finished(tok, cid):
    end = time.time() + 900
    status = ""
    while time.time() < end:
        status = graph(f"/{cid}", tok["token"], {"fields": "status_code,status"}).get("status_code", "")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video ({status})")
        time.sleep(20)
    raise RuntimeError(f"Instagram still processing after 15 min ({status})")


def publish_container(tok, cid):
    m = graph(f"/{tok['user_id']}/media_publish", tok["token"], {"creation_id": cid}, post=True, retries=0)
    link = ""
    try:
        link = graph(f"/{m['id']}", tok["token"], {"fields": "permalink"}).get("permalink", "")
    except Exception:
        pass
    return m["id"], link


def unpark(parked):
    for p in parked:
        try:
            common.worker(f"/clipbot/igfile?key={p['key']}", method="DELETE")
        except Exception:
            pass


def post_video(tok, path, params, dry):
    """Park the mp4, make a container (Reel or Story), wait for Meta to process it, publish. Dry = stop before publish."""
    parked = park(path)
    try:
        c = graph(f"/{tok['user_id']}/media", tok["token"], dict(params, video_url=parked["url"]), post=True)
        wait_finished(tok, c["id"])
        if dry:
            return None, None
        return publish_container(tok, c["id"])
    finally:
        unpark([parked])


def post_carousel(tok, it, work, dry):
    """Leon 2026-10-31 ("Add a post too"): a hand-booked item can also go out as a normal swipe post. The API makes
    every single video a Reel, so a post needs 2+ slides: it['carousel'] lists files from the item's own release
    (.mp4 = video slide, else a JPEG image), first slide sets the shape. Tried once after the Reel + Story, like
    the Story: a failure is reported, never retried, and never fails the Reel."""
    parked = []
    try:
        d = os.path.dirname(publish.video_file(it, work))
        kids = []
        for name in it["carousel"]:
            path = os.path.join(d, name)
            video = name.endswith(".mp4")
            p = park(small_enough(path, work) if video else path)
            parked.append(p)
            params = {"is_carousel_item": "true", **({"media_type": "VIDEO", "video_url": p["url"]} if video else {"image_url": p["url"]})}
            kids.append(graph(f"/{tok['user_id']}/media", tok["token"], params, post=True)["id"])
        for k in kids:
            wait_finished(tok, k)
        c = graph(f"/{tok['user_id']}/media", tok["token"], {"media_type": "CAROUSEL", "children": ",".join(kids),
                                                              "caption": it.get("post_caption") or caption_for(it, work)}, post=True)
        wait_finished(tok, c["id"])
        if dry:
            print(f"{it['id']}: DRY, post container FINISHED ({len(kids)} slides), not published")
            return
        pid, link = publish_container(tok, c["id"])
        print(f"{it['id']}: post {pid} {link}")
    except Exception as e:
        print(f"{it['id']}: post FAILED {str(e)[:300]}")
        try:
            common.worker("/clipbot/failed", {"id": f"Post {it['id']}", "error": "Reel is up, the swipe post was not posted: " + str(e)[:250]})
        except Exception:
            pass
    finally:
        unpark(parked)


def post_reel(tok, path, caption, dry):
    return post_video(tok, path, {"media_type": "REELS", "caption": caption, "share_to_feed": "true"}, dry)


def post_story(tok, it, path, dry):
    """Leon 2026-10-30: every new Reel also goes out as a Story for reach. Clips are <= 58 s, inside the Story
    limit. Tried once, right after its Reel: a failed Story is reported, never retried (no double Stories),
    and never fails the Reel."""
    try:
        sid, _ = post_video(tok, path, {"media_type": "STORIES"}, dry)
        print(f"{it['id']}: " + ("DRY, Story container FINISHED, not published" if sid is None else f"Story {sid}"))
    except Exception as e:
        print(f"{it['id']}: Story FAILED {str(e)[:300]}")
        try:
            common.worker("/clipbot/failed", {"id": f"Story {it['id']}", "error": "Reel is up, Story was not posted: " + str(e)[:250]})
        except Exception:
            pass


def main():
    dry = os.environ.get("IG_DRY") == "1"
    q = common.worker("/clipbot/igqueue")
    if not q.get("login"):
        print("no Instagram login saved yet (run py -m clipbot.ig_login on Leon's laptop)")
        return
    now = datetime.now(common.UTC)
    at = lambda it: datetime.fromisoformat(it["at"].replace("Z", "+00:00"))
    due = sorted((it for it in q["items"] if at(it) <= now), key=at)
    print(f"{len(q['items'])} waiting for Reels, {len(due)} past their slot")
    if not due:
        return
    try:
        tok = login()
        recent = graph(f"/{tok['user_id']}/media", tok["token"], {"fields": "id,caption,permalink", "limit": 25}).get("data", [])
    except (LoginNeeded, RuntimeError) as e:
        common.worker("/clipbot/failed", {"id": "Reels", "error": f"{str(e)[:200]}. Nothing was posted. Log in again on your laptop: "
                                          "py -m clipbot.ig_login in Documents\\ClipBot"})
        raise SystemExit(1)
    work = os.path.abspath("work")
    os.makedirs(work, exist_ok=True)
    posted = failed = 0
    for it in due:
        if posted >= MAX_PER_RUN:
            break
        try:
            if now - at(it) > STALE:
                print(f"{it['id']}: slot {it['at']} is over {STALE.days} days old, skipped")
                if not dry:
                    common.worker("/clipbot/igdone", {"id": it["id"], "dup": True})
                continue
            path = small_enough(publish.video_file(it, work), work)
            caption = caption_for(it, work)
            same = next((m for m in recent if (m.get("caption") or "")[:60] == caption[:60]), None)
            if same:
                print(f"{it['id']}: already on Instagram {same.get('permalink', same['id'])}")
                if not dry:
                    common.worker("/clipbot/igdone", {"id": it["id"], "media_id": same["id"], "dup": True})
                continue
            mid, link = post_reel(tok, path, caption, dry)
            if mid is None:
                print(f"{it['id']}: DRY, container FINISHED, not published. caption={json.dumps(caption, ensure_ascii=False)[:300]}")
                post_story(tok, it, path, dry)
                if it.get("carousel"):
                    post_carousel(tok, it, work, dry)
                continue
            posted += 1
            recent.insert(0, {"id": mid, "caption": caption, "permalink": link})
            print(f"{it['id']}: Reel {mid} {link}")
            common.worker("/clipbot/igdone", {"id": it["id"], "media_id": mid, "url": link})
            post_story(tok, it, path, dry)
            if it.get("carousel"):
                post_carousel(tok, it, work, dry)
        except Exception as e:
            failed += 1
            print(f"{it['id']}: FAILED {str(e)[:300]}")
            try:
                common.worker("/clipbot/failed", {"id": f"Reels {it['id']}", "error": str(e)[:300]})
            except Exception:
                pass
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())

"""Post approved Shorts to Instagram as Reels, through Meta's Instagram API (Instagram Login).

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

from . import common, publish, tiktok

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
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-c:v", "libx264", "-preset", "medium",
                    "-crf", "24", "-maxrate", "6M", "-bufsize", "12M", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", out], check=True)
    if os.path.getsize(out) > MAX_BYTES:
        raise RuntimeError(f"clip is {os.path.getsize(out) >> 20} MB even re-encoded")
    return out


def park(path):
    with open(path, "rb") as f:
        data = f.read()
    return common.http(common.WORKER + "/clipbot/igfile", data, timeout=300,
                       headers={"Authorization": "Bearer " + common.env("CLIPBOT_TOKEN"), "Content-Type": "application/octet-stream"})


def post_reel(tok, path, caption, dry):
    parked = park(path)
    try:
        c = graph(f"/{tok['user_id']}/media", tok["token"], {"media_type": "REELS", "video_url": parked["url"],
                                                              "caption": caption, "share_to_feed": "true"}, post=True)
        cid = c["id"]
        end = time.time() + 900
        status = ""
        while time.time() < end:
            status = graph(f"/{cid}", tok["token"], {"fields": "status_code,status"}).get("status_code", "")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram could not process the video ({status})")
            time.sleep(20)
        if status != "FINISHED":
            raise RuntimeError(f"Instagram still processing after 15 min ({status})")
        if dry:
            return None, None
        m = graph(f"/{tok['user_id']}/media_publish", tok["token"], {"creation_id": cid}, post=True, retries=0)
        link = ""
        try:
            link = graph(f"/{m['id']}", tok["token"], {"fields": "permalink"}).get("permalink", "")
        except Exception:
            pass
        return m["id"], link
    finally:
        try:
            common.worker(f"/clipbot/igfile?key={parked['key']}", method="DELETE")
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
                continue
            posted += 1
            recent.insert(0, {"id": mid, "caption": caption, "permalink": link})
            print(f"{it['id']}: Reel {mid} {link}")
            common.worker("/clipbot/igdone", {"id": it["id"], "media_id": mid, "url": link})
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

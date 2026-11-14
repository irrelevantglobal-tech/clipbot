"""Post approved Shorts to X (Twitter) through Buffer's free plan, once their YouTube slot has passed.

    python -m clipbot.x                 (X_DRY=1: everything except the final createPost)

Owner 2026-11-14: "get the clip auto posting thing set up for X the same way it is for tiktok, youtube and
instagram" + "we need to find a free way". X's own API charges per post, so the post goes through Buffer
(free plan, API key on every plan): Buffer is the one paying X. One Discord approval covers every lane.

Work list: the worker's /clipbot/xqueue (every Short publish.py scheduled on YouTube).
Login: BUFFER_API_KEY (Actions secret, written by x_login.py on the owner's laptop; never in the repo).
The X account is the single X channel connected in that Buffer account; zero or several = stop and say so.
Buffer fetches the video from a URL, so the mp4 is parked on the worker (1 day) and deleted afterwards.
Idempotent: the post text is a fixed function of the approved title + hashtags, and Buffer's own recent
posts on the channel are checked for it first. If that list cannot be read, nothing is posted.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

from . import common, instagram, legacy, publish

API = "https://api.buffer.com"
MAX_PER_RUN = 2
STALE = timedelta(days=3)
LIMIT = 280
URL_LEN = 23                     # X counts every link as 23 characters


class LoginNeeded(Exception):
    pass


def gql(key, query, variables=None, retries=2):
    r = common.http(API, {"query": query, "variables": variables or {}}, retries=retries, timeout=90,
                    headers={"Authorization": "Bearer " + key})
    if r.get("errors"):
        msg = "; ".join(e.get("message", "") for e in r["errors"])[:300]
        if re.search(r"unauth|invalid.*(key|token)|forbidden", msg, re.I):
            raise LoginNeeded(f"Buffer refused the API key ({msg})")
        raise RuntimeError(f"Buffer: {msg}")
    return r["data"]


def x_channel(key):
    orgs = gql(key, "query { account { organizations { id } } }")["account"]["organizations"]
    found = []
    for o in orgs:
        for c in gql(key, "query($o: OrganizationId!) { channels(input: {organizationId: $o}) { id name service isQueuePaused } }",
                     {"o": o["id"]})["channels"]:
            if c["service"] in ("twitter", "x"):
                found.append((o["id"], c))
    if len(found) != 1:
        raise LoginNeeded(f"expected exactly one X channel connected in Buffer, found {len(found)}")
    return found[0]


def recent_posts(key, org, chan):
    q = """query($o: OrganizationId!, $c: ChannelId!) {
      posts(first: 40, input: {organizationId: $o, filter: {channelIds: [$c], status: [sent, sending, scheduled, error]},
             sort: [{field: createdAt, direction: desc}]}) { edges { node { id text status externalLink } } } }"""
    return [e["node"] for e in gql(key, q, {"o": org, "c": chan})["posts"]["edges"]]


def tags_for(it):
    cap = legacy.fix(it.get("tiktok_caption") or "")
    tags = [t for t in re.findall(r"#\w+", cap) if t.lower() not in common.GENERIC_TAGS]
    return list(dict.fromkeys([*tags, "#" + common.LOGIN]))[:3]


def x_text(it):
    title = publish.mask(legacy.fix(it["title"]).strip())
    link = f"https://twitch.tv/{common.LOGIN}"
    tags = tags_for(it)
    while True:
        tail = f"\n\nLive on Twitch: {link}" + (f"\n\n{' '.join(tags)}" if tags else "")
        room = LIMIT - (len(tail) - len(link) + URL_LEN)
        if len(title) <= room or not tags:
            break
        tags.pop(0)
    return (title[:room].rstrip() + tail).strip()


def already(posts, text):
    head = text.split("\n\n")[0].strip()
    return next((p for p in posts if (p.get("text") or "").split("\n\n")[0].strip() == head), None)


def create(key, chan, text, url):
    q = """mutation($t: String!, $c: ChannelId!, $u: String!) {
      createPost(input: {text: $t, channelId: $c, schedulingType: automatic, mode: shareNow, assets: [{video: {url: $u}}]}) {
        ... on PostActionSuccess { post { id status } }
        ... on MutationError { message } } }"""
    r = gql(key, q, {"t": text, "c": chan, "u": url}, retries=0)["createPost"]
    if not r.get("post"):
        raise RuntimeError(f"Buffer refused the post: {r.get('message', r)}"[:300])
    return r["post"]["id"]


def wait_sent(key, pid, tries=30, wait=10):
    for _ in range(tries):
        p = gql(key, "query($i: PostId!) { post(input: {id: $i}) { status externalLink error { message } } }", {"i": pid})["post"]
        if p["status"] == "sent":
            return p.get("externalLink") or ""
        if p["status"] == "error":
            raise RuntimeError(f"Buffer could not post to X: {(p.get('error') or {}).get('message', '?')}"[:300])
        time.sleep(wait)
    return None                  # still sending: Buffer owns it now, and the text check stops a repost


def tell_done(it, pid, link):
    for n in range(3):
        try:
            common.worker("/clipbot/xdone", {"id": it["id"], "post_id": pid, "url": link})
            return True
        except Exception as e:
            print(f"{it['id']}: xdone try {n + 1} failed: {str(e)[:200]}")
            time.sleep(5)
    return False


def main():
    dry = os.environ.get("X_DRY") == "1"
    key = os.environ.get("BUFFER_API_KEY", "")
    items = common.worker("/clipbot/xqueue").get("items", [])
    now = datetime.now(common.UTC)
    at = lambda it: datetime.fromisoformat(it["at"].replace("Z", "+00:00"))
    due = sorted((it for it in items if at(it) <= now), key=at)
    print(f"{len(items)} waiting for X, {len(due)} past their slot")
    if not key:
        print("no Buffer login saved yet (owner: copy the Buffer API key, then double-click 'Set Buffer key from clipboard')")
        return
    if not due:
        return
    try:
        org, chan = x_channel(key)
        if chan.get("isQueuePaused"):
            raise LoginNeeded("the X queue is paused in Buffer")
        posts = recent_posts(key, org, chan["id"])
    except (LoginNeeded, RuntimeError) as e:
        common.worker("/clipbot/failed", {"id": "X", "error": f"{str(e)[:220]}. Nothing was posted to X."})
        raise SystemExit(1)
    work = os.path.abspath("work")
    os.makedirs(work, exist_ok=True)
    posted = failed = 0
    for it in due:
        if posted >= MAX_PER_RUN:
            break
        parked = None
        try:
            if now - at(it) > STALE:
                print(f"{it['id']}: slot {it['at']} is over {STALE.days} days old, skipped")
                if not dry:
                    common.worker("/clipbot/xdone", {"id": it["id"], "dup": True})
                continue
            text = x_text(it)
            legacy.check(text, where=it["id"])
            same = already(posts, text)
            if same:
                print(f"{it['id']}: already in Buffer for X ({same['status']}) {same.get('externalLink') or same['id']}")
                if not dry:
                    common.worker("/clipbot/xdone", {"id": it["id"], "post_id": same["id"], "dup": True})
                continue
            path = instagram.small_enough(publish.video_file(it, work), work)
            if dry:
                print(f"{it['id']}: DRY, not posted. {os.path.getsize(path) >> 20} MB, text={json.dumps(text, ensure_ascii=False)}")
                continue
            parked = instagram.park(path)
            pid = create(key, chan["id"], text, parked["url"])
            posts.insert(0, {"id": pid, "text": text, "status": "sending"})
            posted += 1
            link = wait_sent(key, pid)
            if link is None:
                parked = None    # Buffer may still need the file; the worker drops it within a day
            print(f"{it['id']}: X post {pid} {'(still sending in Buffer)' if link is None else link or 'sent'}")
            if not tell_done(it, pid, link):
                raise RuntimeError("posted to X but the worker was not told; Buffer's own list stops a repost next run")
        except Exception as e:
            failed += 1
            print(f"{it['id']}: FAILED {str(e)[:300]}")
            try:
                common.worker("/clipbot/failed", {"id": f"X {it['id']}", "error": str(e)[:300]})
            except Exception:
                pass
        finally:
            if parked and not dry:
                instagram.unpark([parked])
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())

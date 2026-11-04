"""Our own first comment on every ClipBot Short once it is public on YouTube.

    python -m clipbot.yt_comments        (YTC_DRY=1: everything except posting)

Owner 2026-10-31: "make it so on the automatic shorts ... it makes its own pinned comment on each auto upload,
something to do with the video and then come check out the streams + the link". A comment can only go on a
PUBLIC video (scheduled Shorts refuse it), so this runs just after every slot (instagram.yml). The YouTube API
has no pin, so the comment is posted unpinned.

Login: its own refresh token (YT_COMMENT_REFRESH_TOKEN, youtube.force-ssl, minted by yt_comment_login.py on
the owner's laptop) on the same OAuth client as the uploads; the upload token is left alone.
Idempotent: YouTube's own comment list is checked for a comment by the channel before posting.
Stdlib only, so the workflow runs it without installing anything.
"""
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta

from . import common, legacy

WINDOW = timedelta(hours=12)       # only Shorts that went public this recently (covers late cron runs)
LINK = f"https://www.twitch.tv/{common.LOGIN}"
_tok = {}


def token():
    if "t" not in _tok or _tok["exp"] < time.time() + 120:
        q = urllib.parse.urlencode({"client_id": common.env("YT_CLIENT_ID"), "client_secret": common.env("YT_CLIENT_SECRET"),
                                    "refresh_token": common.env("YT_COMMENT_REFRESH_TOKEN"), "grant_type": "refresh_token"}).encode()
        r = common.http("https://oauth2.googleapis.com/token", q)
        _tok["t"], _tok["exp"] = r["access_token"], time.time() + r.get("expires_in", 3600)
    return _tok["t"]


def api(path, body=None, **params):
    url = "https://www.googleapis.com/youtube/v3/" + path + "?" + urllib.parse.urlencode(params)
    h = {"Authorization": "Bearer " + token()}
    if body is None:
        return common.http(url, headers=h)
    return common.http(url, json.dumps(body).encode(), headers={**h, "Content-Type": "application/json"}, retries=1)


def clip_title(t):
    t = re.sub(r"\s*\|\s*" + re.escape(common.BRAND) + r"\s*$", "", legacy.fix(t or ""), flags=re.I)
    return re.sub(r"#\S+", "", t).strip()


def ours_already(vid):
    r = api("commentThreads", part="snippet", videoId=vid, maxResults=50, order="time")
    return any(c["snippet"]["topLevelComment"]["snippet"].get("authorChannelId", {}).get("value") == common.CHANNEL_ID
               for c in r.get("items", []))


def main():
    dry = os.environ.get("YTC_DRY") == "1"
    if not os.environ.get("YT_COMMENT_REFRESH_TOKEN"):
        print("no YouTube comment login saved yet (run py -m clipbot.yt_comment_login on the owner's laptop)")
        return
    me = api("channels", part="id", mine="true").get("items", [])
    if not me or me[0]["id"] != common.CHANNEL_ID:
        raise SystemExit(f"WRONG CHANNEL on the comment token ({me[0]['id'] if me else 'none'}), expected {common.CHANNEL_ID}. Nothing posted.")
    pl = "UU" + common.CHANNEL_ID[2:]
    ids = [i["contentDetails"]["videoId"] for i in api("playlistItems", part="contentDetails", playlistId=pl, maxResults=25).get("items", [])]
    vids = api("videos", part="snippet,status", id=",".join(ids)).get("items", []) if ids else []
    now = datetime.now(common.UTC)
    due = []
    for v in vids:
        m = re.search(r"^clipbot:(\S+)$", v["snippet"].get("description", ""), re.M)
        live = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
        if m and v["status"].get("privacyStatus") == "public" and now - live <= WINDOW:
            due.append((v, m.group(1)))
    print(f"{len(vids)} recent uploads, {len(due)} ClipBot Shorts public in the last {WINDOW.seconds // 3600} h")
    failed = []
    for v, key in due:
        vid = v["id"]
        try:
            if ours_already(vid):
                print(f"{vid}: already has our comment")
                continue
            text = common.promo_comment(clip_title(v["snippet"]["title"]), key, link=LINK)
            legacy.check(text, where=key)
            if dry:
                print(f"{vid}: DRY, not posted: {json.dumps(text, ensure_ascii=False)}")
                continue
            r = api("commentThreads", {"snippet": {"videoId": vid, "topLevelComment": {"snippet": {"textOriginal": text}}}}, part="snippet")
            print(f"{vid}: comment {r.get('id')}")
        except Exception as e:
            print(f"{vid}: FAILED {str(e)[:300]}")
            failed.append(f"{vid}: {str(e)[:150]}")
    if failed:
        try:
            common.worker("/clipbot/failed", {"id": "YouTube comments", "error": "; ".join(failed)[:300]})
        except Exception:
            pass
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())

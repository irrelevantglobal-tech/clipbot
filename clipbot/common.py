"""Shared plumbing: Twitch Helix, YouTube upload + scheduling, the Forge worker.

Every secret comes from the environment (GitHub Actions secrets). Nothing here prints one.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
UTC = ZoneInfo("UTC")
# People (2026-10-29: Titan gets his own lane). CLIPBOT_WHO picks one; absent = leon, so every Leon run
# is exactly what it was. A person's YouTube token comes ONLY from his own env names (yt_env): there is no
# fallback to anyone else's, and channel_gate() refuses any token that is not on his channel.
PEOPLE = {
    "leon": {"channel": "UCu6aZl_RZJAeIYJ3QgLAs8A", "twitch_uid": "1409819988", "login": "twinkiesghost",
             "brand": "TwinkiesGhost", "yt_env": "YT_"},
    "glatiar": {"channel": "UC0y2IwGQUSMhsAN1i6BJR4A", "twitch_uid": "1119913225", "login": "glatiar",
                "brand": "Glatiar", "yt_env": "GLAT_YT_"},
}
WHO = os.environ.get("CLIPBOT_WHO") or "leon"
if WHO not in PEOPLE:
    raise SystemExit(f"unknown CLIPBOT_WHO {WHO!r}")
P = PEOPLE[WHO]
CHANNEL_ID = P["channel"]                          # TwinkiesGhost for Leon. Every upload is gated on it.
TWITCH_UID = P["twitch_uid"]                       # twinkiesghost for Leon
LOGIN = P["login"]
BRAND = P["brand"]
WORKER = os.environ.get("CLIPBOT_WORKER", "https://irrelevant-forge.irrelevantforge.workers.dev")

# When things go out (UK local). Leon 2026-10-19: "only 1-2 clips a day... if there's already
# 2 clips scheduled for that day move them onto the next day". So: at most MAX_PER_DAY Shorts on
# any UK day, counting EVERY Short on the channel (scheduled or already published that day, from
# ClipBot or not), filling 18:00 first, then 13:00.
DAY_SLOTS = ["18:00", "13:00"]
MAX_PER_DAY = 2
# Marathon mode (Leon 2026-10-28: "4 a day during marathons"): with a 10 h+ stream live now or in the
# last 72 h, up to MARATHON_PER_DAY Shorts a day, the extra two at 21:00 then 10:00. Back to 2 after.
MARATHON_SLOTS = ["18:00", "13:00", "21:00", "10:00"]
MARATHON_PER_DAY = 4
MARATHON_MIN_S = 10 * 3600


def env(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"missing env {name}")
    return v


def http(url, data=None, headers=None, method=None, timeout=60, retries=3):
    """JSON in, JSON out. Retries 429/5xx and network drops; raises on anything else."""
    headers = {"User-Agent": "TwinkiesGhost-ClipBot/1.0", **(headers or {})}   # CF 1010 blocks Python-urllib
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode()
        headers.setdefault("Content-Type", "application/json")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt * 3)
                continue
            detail = e.read()[:300].decode("utf-8", "replace")
            raise RuntimeError(f"{method or ('POST' if data else 'GET')} {url.split('?')[0]} -> {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < retries:
                time.sleep(2 ** attempt * 3)
                continue
            raise


# ---------------- Twitch ----------------
_tw = {}


def helix(path):
    if "tok" not in _tw:
        q = urllib.parse.urlencode({"client_id": env("TWITCH_CLIENT_ID"),
                                    "client_secret": env("TWITCH_CLIENT_SECRET"),
                                    "grant_type": "client_credentials"}).encode()
        _tw["tok"] = http("https://id.twitch.tv/oauth2/token", q)["access_token"]
    return http("https://api.twitch.tv/helix/" + path,
                headers={"Client-Id": env("TWITCH_CLIENT_ID"), "Authorization": "Bearer " + _tw["tok"]})


def is_live():
    return bool(helix(f"streams?user_id={TWITCH_UID}")["data"])


def vod_clips(vod_id):
    """Twitch clips cut from this VOD, as (start, end) VOD seconds. vod_offset can be null
    for a fresh clip, so those are matched by creation time instead (best effort)."""
    out = []
    v = helix(f"videos?id={vod_id}")["data"][0]
    start = datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
    after = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = ""
    for _ in range(10):
        j = helix(f"clips?broadcaster_id={TWITCH_UID}&started_at={after}&first=100" + (f"&after={cur}" if cur else ""))
        for c in j["data"]:
            if c.get("video_id") not in (vod_id, "", None):
                continue
            off = c.get("vod_offset")
            if off is None:
                made = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
                off = (made - start).total_seconds() - c["duration"]
            out.append((max(0.0, off), off + c["duration"]))
        cur = j.get("pagination", {}).get("cursor")
        if not cur:
            break
    return out


# ---------------- YouTube ----------------
_yt = {}


def yt_token():
    if "tok" not in _yt or _yt["exp"] < time.time() + 120:
        e = P["yt_env"]
        q = urllib.parse.urlencode({"client_id": env(e + "CLIENT_ID"), "client_secret": env(e + "CLIENT_SECRET"),
                                    "refresh_token": env(e + "REFRESH_TOKEN"), "grant_type": "refresh_token"}).encode()
        r = http("https://oauth2.googleapis.com/token", q)
        _yt["tok"], _yt["exp"] = r["access_token"], time.time() + r.get("expires_in", 3600)
    return _yt["tok"]


def yt(path, **params):
    return http("https://www.googleapis.com/youtube/v3/" + path + "?" + urllib.parse.urlencode(params),
                headers={"Authorization": "Bearer " + yt_token()})


def channel_gate():
    """TwinkiesGhost is a Brand Account and the consent picker has confused channels
    before. Refuse to upload anywhere but this person's own channel."""
    items = yt("channels", part="id,snippet", mine="true").get("items", [])
    if not items or items[0]["id"] != CHANNEL_ID:
        got = items[0]["id"] if items else "none"
        raise SystemExit(f"WRONG CHANNEL on the YouTube token ({got}), expected {CHANNEL_ID}. Nothing uploaded.")


def recent_uploads(n=50):
    """Our recent uploads with status. The uploads playlist can omit videos, so callers
    also union this with the worker's own ledger before trusting it."""
    pl = "UU" + CHANNEL_ID[2:]
    ids = [i["contentDetails"]["videoId"] for i in
           yt("playlistItems", part="contentDetails", playlistId=pl, maxResults=n).get("items", [])]
    if not ids:
        return []
    return yt("videos", part="snippet,status,contentDetails", id=",".join(ids)).get("items", [])


def find_marker(marker, uploads):
    for v in uploads:
        if marker in v["snippet"].get("description", ""):
            return v["id"]
    return None


def _iso_dur(s):
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    return sum(int(x or 0) * k for x, k in zip(m.groups(), (3600, 60, 1))) if m else 0


def marathon_mode():
    """True while a marathon is on or just ended: a 10 h+ archive VOD in the last 72 h (an in-progress
    stream's archive VOD keeps growing, so a live marathon counts once it passes 10 h). Any Twitch
    error -> False (the normal 2 a day). Leon only: everyone else is always 2 a day."""
    if WHO != "leon":
        return False
    try:
        vods = helix(f"videos?user_id={TWITCH_UID}&type=archive&first=5")["data"]
    except Exception as e:
        print(f"marathon check failed ({str(e)[:80]}): 2 a day")
        return False
    now = datetime.now(UTC)
    for v in vods:
        d = _iso_dur("PT" + v["duration"].upper())
        end = datetime.fromisoformat(v["created_at"].replace("Z", "+00:00")) + timedelta(seconds=d)
        if d >= MARATHON_MIN_S and now - end < timedelta(hours=72):
            return True
    return False


def next_slot(taken_utc, uploads, min_lead_h=3, marathon=False):
    """Next free slot: first UK day with fewer than MAX_PER_DAY Shorts (MARATHON_PER_DAY in
    marathon mode), at the first free slot time (nothing within 90 min) at least min_lead_h away."""
    cap, slots = (MARATHON_PER_DAY, MARATHON_SLOTS) if marathon else (MAX_PER_DAY, DAY_SLOTS)
    now = datetime.now(UTC)
    busy = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in taken_utc]
    per_day = {}
    for t in busy:
        d = t.astimezone(UK).date()
        per_day[d] = per_day.get(d, 0) + 1
    for v in uploads:
        if _iso_dur(v["contentDetails"].get("duration")) > 180:
            continue                                     # long-form never counts toward the Shorts cap
        st = v["status"]
        if st.get("publishAt") and st.get("privacyStatus") == "private":
            t = datetime.fromisoformat(st["publishAt"].replace("Z", "+00:00"))
        elif st.get("privacyStatus") == "public":
            t = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
        else:
            continue
        if any(abs((t - b).total_seconds()) < 60 for b in busy):
            continue                                     # same video already counted via the ledger
        busy.append(t)
        d = t.astimezone(UK).date()
        per_day[d] = per_day.get(d, 0) + 1
    day = now.astimezone(UK).date()
    for _ in range(180):
        if per_day.get(day, 0) < cap:
            for hm in slots:
                h, m = map(int, hm.split(":"))
                utc = datetime(day.year, day.month, day.day, h, m, tzinfo=UK).astimezone(UTC)
                if utc < now + timedelta(hours=min_lead_h):
                    continue
                if any(abs((utc - b).total_seconds()) < 90 * 60 for b in busy):
                    continue
                return utc
        day += timedelta(days=1)
    raise SystemExit("no free slot in the next 180 days")


def upload(path, title, description, publish_at_utc, tags=()):
    """Resumable upload as private + publishAt (= Scheduled). Returns the video id."""
    if publish_at_utc <= datetime.now(UTC):
        raise SystemExit("publishAt is in the past")
    title = title.strip()[:100]
    body = {
        "snippet": {"title": title, "description": description[:5000], "tags": list(tags)[:30],
                    "categoryId": "20"},
        "status": {"privacyStatus": "private", "publishAt": publish_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "selfDeclaredMadeForKids": False},
    }
    size = os.path.getsize(path)
    init = urllib.request.Request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + yt_token(), "Content-Type": "application/json",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
    with urllib.request.urlopen(init, timeout=60) as r:
        session = r.headers["Location"]
    return upload_session(session, path, size)


def upload_session(session, path, size):
    """Send the file to an open resumable-upload session, riding out drops. Returns the video id."""
    sent, fails, chunk = 0, 0, 8 * 1024 * 1024
    with open(path, "rb") as f:
        while True:
            f.seek(sent)
            data = f.read(chunk)
            end = sent + len(data) - 1
            req = urllib.request.Request(session, data=data, method="PUT", headers={
                "Authorization": "Bearer " + yt_token(), "Content-Length": str(len(data)),
                "Content-Range": f"bytes {sent}-{end}/{size}"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read())["id"]          # 200/201 = done
            except urllib.error.HTTPError as e:
                if e.code == 308:                               # chunk accepted, keep going
                    rng = e.headers.get("Range")
                    sent = int(rng.split("-")[1]) + 1 if rng else 0
                    fails = 0
                    continue
                if e.code in (500, 502, 503, 504) and fails < 8:
                    fails += 1
                    time.sleep(min(60, 2 ** fails))
                    sent = _resume_offset(session, size)
                    continue
                raise RuntimeError(f"upload -> {e.code}: {e.read()[:300]!r}")
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if fails >= 8:
                    raise
                fails += 1
                time.sleep(min(60, 2 ** fails))
                sent = _resume_offset(session, size)


def _resume_offset(session, size):
    req = urllib.request.Request(session, data=b"", method="PUT", headers={
        "Authorization": "Bearer " + yt_token(), "Content-Range": f"bytes */{size}", "Content-Length": "0"})
    try:
        urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 308:
            rng = e.headers.get("Range")
            return int(rng.split("-")[1]) + 1 if rng else 0
        raise
    return size


# ---------------- Forge worker ----------------
def who_fields():
    """`who` for worker payloads: nothing for Leon (his payloads stay exactly as they were)."""
    return {} if WHO == "leon" else {"who": WHO}


def manifest_clip(it, work):
    """The clip's manifest entry (title, transcript quote, game) + the stream title, from its release as
    unpacked by publish.video_file into work/art-<batch>/."""
    batch = it.get("batch") or it["vod"]
    try:
        with open(os.path.join(work, f"art-{batch}", "manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return {}, ""
    c = next((c for c in m.get("clips", []) if (it.get("slug") and c.get("slug") == it["slug"]) or str(c.get("n")) == str(it["n"])), {})
    return c, m.get("vod_title", "")


def release_name(batch):
    """GitHub Release holding a batch's clips. Leon's names are unchanged (clips-<batch>); anyone else's
    carry his name, so a Twitch-clip batch (a unix second) can never land on Leon's release."""
    return f"clips-{batch}" if WHO == "leon" else f"clips-{WHO}-{batch}"


def check_worker_knows_who():
    """Before anything for a person other than Leon reaches the worker: the worker must echo his `who` on
    the queue route. An older worker ignores ?who and would treat his clips as LEON's (Leon's DMs, Leon's
    queue, Leon's channel), so refuse rather than act blind."""
    if WHO == "leon":
        return
    got = worker("/clipbot/queue?who=" + WHO).get("who")
    if got != WHO:
        raise SystemExit(f"the Forge worker does not know {WHO!r} yet (queue answered who={got!r}): deploy the Forge first. Nothing sent.")


def worker(path, payload=None, method=None):
    return http(WORKER + path, payload, method=method,
                headers={"Authorization": "Bearer " + env("CLIPBOT_TOKEN")}, timeout=120)


def uk(dt):
    return dt.astimezone(UK).strftime("%a %d %b %H:%M UK")


# broad filler every gaming clip carries; it adds no reach (Leon 10-29: "not the same generic ones")
GENERIC_TAGS = {"#fyp", "#foryou", "#foryoupage", "#fy", "#foryourpage", "#viral", "#viralvideo", "#trending", "#explore",
                "#explorepage", "#reels", "#reel", "#reelsinstagram", "#instagram", "#tiktok", "#gaming", "#gamer", "#gamers",
                "#gamingclips", "#gamingcommunity", "#funny", "#funnymoments", "#funnyvideos", "#lol", "#memes", "#meme",
                "#videogames", "#videogame", "#game", "#games", "#gameplay", "#clips", "#clip", "#streamer", "#twitch",
                "#twitchstreamer", "#shortsvideo", "#youtubeshorts"}


def _tags(raw, n, keep_shorts=False):
    import re
    tags = [t.lower() for t in (("#" + str(x).lstrip("#")) for x in (raw if isinstance(raw, list) else []))
            if re.fullmatch(r"#[A-Za-z0-9_]{2,40}", t)]
    return [t for t in dict.fromkeys(tags) if t not in GENERIC_TAGS and (keep_shorts or t != "#shorts")][:n]


def clip_copy(title, game="", stream_title="", transcript=""):
    """AI-written description + hashtags + tags for one clip (worker /clipbot/copy, Workers AI).
    Validated hard; any problem -> a plain fallback so a clip is never blocked on it. Leon
    sees and can edit all of it in Discord before anything uploads."""
    import re
    out = {"description": "", "hashtags": [], "tags": [], "game": game or "", "tiktok_hook": "", "tiktok_hashtags": [],
           "ig_hook": "", "ig_hashtags": []}
    try:
        r = worker("/clipbot/copy", {"title": title, "game": game, "stream_title": stream_title,
                                     "transcript": transcript[:1200], **who_fields()})
    except Exception as e:
        print(f"copy unavailable ({str(e)[:100]})")
        r = {}
    d = r.get("description")
    if isinstance(d, str) and 20 <= len(d.strip()) <= 600 and "kick.com" not in d.lower():
        out["description"] = re.sub(r"\s+\n", "\n", d.strip())
    out["hashtags"] = _tags(r.get("hashtags"), 5, keep_shorts=True)
    yt_tags = r.get("tags") if isinstance(r.get("tags"), list) else []
    out["tags"] = [str(t).strip()[:60] for t in yt_tags if isinstance(t, str) and t.strip()][:10]
    # TikTok gets its own caption line + hashtags (Leon 10-23: "best hashtags for tiktok and youtube per clip")
    hook = r.get("tiktok_hook")
    out["tiktok_hook"] = re.sub(r"#\S+", "", hook).strip()[:120] if isinstance(hook, str) else ""
    own = "#" + LOGIN
    tt = _tags(r.get("tiktok_hashtags"), 5)
    out["tiktok_hashtags"] = tt + [own] if own not in tt else tt
    # Instagram: its own opener + tags (Leon 10-29), at most 5 tags including ours
    hook = r.get("ig_hook")
    out["ig_hook"] = re.sub(r"#\S+", "", hook).strip()[:140] if isinstance(hook, str) else ""
    ig = _tags(r.get("ig_hashtags"), 4)
    out["ig_hashtags"] = ig + [own] if own not in ig else ig
    if isinstance(r.get("game"), str) and r["game"].strip() and not out["game"]:
        out["game"] = r["game"].strip()[:60]
    if not out["description"]:
        out["description"] = f"{title}. Catch the chaos live at twitch.tv/{LOGIN}"
    for k in ("description", "tiktok_hook", "ig_hook"):          # no em dashes in anything public
        out[k] = re.sub(r"\s*[\u2014\u2013]\s*", ", ", out[k]).strip(", ")
    if "#shorts" not in out["hashtags"]:
        out["hashtags"].append("#shorts")
    return out

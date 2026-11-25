"""Shared plumbing: Twitch Helix, YouTube upload + scheduling, the worker.

Every secret comes from the environment (GitHub Actions secrets). Nothing here prints one.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# ---------------- log hygiene ----------------
# This repo is public and so are its Actions logs. Nothing identifying may reach them: channel names (from the
# identity), and every title / game / channel name that comes back from Twitch, YouTube or the worker is registered
# here the moment it arrives. Registered strings are masked by Actions itself (::add-mask::) and, belt and braces,
# scrubbed from our own stdout/stderr. Short strings (< 5 chars) are not registered: masking them garbles the log.
_SENS = set()
_SENS_KEYS = {"title", "channelTitle", "game_name", "game", "user_name", "user_login", "broadcaster_name",
              "broadcaster_login", "creator_name", "display_name", "vod_title", "quote", "username", "login", "brand"}
_REAL_STDOUT = sys.__stdout__


def sensitive(*vals):
    for v in vals:
        if isinstance(v, str):
            v = v.strip()
            if len(v) >= 5 and "\n" not in v and "\r" not in v and v not in _SENS:
                _SENS.add(v)
                if os.environ.get("GITHUB_ACTIONS"):
                    _REAL_STDOUT.write(f"::add-mask::{v}\n")
                    _REAL_STDOUT.flush()


def _walk(o, depth=0):
    if depth > 8:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            if k in _SENS_KEYS and isinstance(v, str):
                sensitive(v)
            elif isinstance(v, (dict, list)):
                _walk(v, depth + 1)
    elif isinstance(o, list):
        for x in o:
            _walk(x, depth + 1)


class _Redact:
    def __init__(self, stream):
        self._s = stream

    def write(self, text):
        if _SENS and isinstance(text, str):
            for v in sorted(_SENS, key=len, reverse=True):
                text = re.sub(re.escape(v), "***", text, flags=re.I)
        return self._s.write(text)

    def __getattr__(self, name):
        return getattr(self._s, name)


def install_redaction():
    if not isinstance(sys.stdout, _Redact):
        sys.stdout = _Redact(sys.stdout)
        sys.stderr = _Redact(sys.stderr)


if os.environ.get("GITHUB_ACTIONS") or os.environ.get("CLIPBOT_REDACT"):
    install_redaction()

UK = ZoneInfo("Europe/London")
UTC = ZoneInfo("UTC")
# People (2026-10-29: Titan gets his own lane). CLIPBOT_WHO picks one; absent = leon, so every run of the owner's
# is exactly what it was. A person's YouTube token comes ONLY from his own env names (yt_env): there is no
# fallback to anyone else's, and channel_gate() refuses any token that is not on his channel.
#
# 2026-11-04 rebrand: this repo is PUBLIC, so the owner's channel identity is NOT written in it. It comes from the
# CLIPBOT_ID secret (JSON: login, brand, twitch_uid, channel, ig, tiktok, legacy) or, on his laptop, from the
# gitignored .secrets/identity.json. Values are masked in Actions logs. Glatiar's lane never reads it.
def _leon_identity():
    raw = os.environ.get("CLIPBOT_ID")
    if not raw:
        path = os.environ.get("CLIPBOT_ID_FILE") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".secrets", "identity.json")
        if os.path.exists(path):
            raw = open(path, encoding="utf-8").read()
    if not raw:
        raise SystemExit("no channel identity: set the CLIPBOT_ID secret (or .secrets/identity.json). Nothing done.")
    ident = json.loads(raw)
    for k in ("login", "brand", "twitch_uid", "channel"):
        if not ident.get(k):
            raise SystemExit(f"CLIPBOT_ID is missing {k!r}. Nothing done.")
    sensitive(ident["login"], ident["brand"], ident.get("ig"), ident.get("yt"), ident.get("tiktok"), *ident.get("legacy", []))
    return ident


PEOPLE = {
    "leon": {},                                   # filled from the identity below, only when his lane runs
    "glatiar": {"channel": "UC0y2IwGQUSMhsAN1i6BJR4A", "twitch_uid": "1119913225", "login": "glatiar",
                "brand": "Glatiar", "yt_env": "GLAT_YT_"},
}
WHO = os.environ.get("CLIPBOT_WHO") or "leon"
if WHO not in PEOPLE:
    raise SystemExit(f"unknown CLIPBOT_WHO {WHO!r}")
IDENT = {}
if WHO == "leon":
    IDENT = _leon_identity()
    PEOPLE["leon"] = {"channel": IDENT["channel"], "twitch_uid": str(IDENT["twitch_uid"]), "login": IDENT["login"],
                      "brand": IDENT["brand"], "yt_env": "YT_"}
P = PEOPLE[WHO]
CHANNEL_ID = P["channel"]                          # Every upload is gated on it.
TWITCH_UID = P["twitch_uid"]
LOGIN = P["login"]
BRAND = P["brand"]
# Kick pivot (owner 17-18 Nov: "every single system we had for twitch needs to be transfared to kick"): Leon's public
# copy says Kick, never Twitch. The address comes from the KICK_SLUG secret and goes in once KICK_SLUG_PUBLIC=1 (repo
# variable: the boss confirmed the slug is his and live); without it the copy names the brand on Kick, never a link.
# Every other person's lane (Glatiar) stays on Twitch.
KICK_LIVE = WHO == "leon"


def live_url():
    """'kick.com/<slug>' (Leon) | 'twitch.tv/<login>' (anyone else) | '' (Leon, address not cleared for public copy)."""
    if not KICK_LIVE:
        return f"twitch.tv/{LOGIN}"
    slug = (os.environ.get("KICK_SLUG") or "").strip()
    return f"kick.com/{slug}" if slug and os.environ.get("KICK_SLUG_PUBLIC") == "1" else ""


def live_platform():
    return "Kick" if KICK_LIVE else "Twitch"


def live_text():
    """A 'watch me live' tail that is always safe to print: the address, or '<Brand> on Kick'."""
    return live_url() or f"{BRAND} on Kick"


WORKER = os.environ.get("CLIPBOT_WORKER", "")        # the approval/queue worker's base URL: a secret, never in the repo

# When things go out (UK local). Owner 2026-10-19: "only 1-2 clips a day... if there's already
# 2 clips scheduled for that day move them onto the next day". So: at most MAX_PER_DAY Shorts on
# any UK day, counting EVERY Short on the channel (scheduled or already published that day, from
# ClipBot or not), filling 18:00 first, then 13:00.
DAY_SLOTS = ["18:00", "13:00"]
MAX_PER_DAY = 2
# Marathon mode, with a 10 h+ stream live now or in the last 72 h. Owner 2026-11-14 (GTA 6 marathon): "I dont
# want just 4 a day... no limit", approved clips go out "hourly, no daily cap": the next free hour 09:00-23:00 UK,
# at least MARATHON_GAP_MIN apart. Back to 2 a day after.
MARATHON_SLOTS = [f"{h:02d}:00" for h in range(9, 24)]
MARATHON_PER_DAY = len(MARATHON_SLOTS)
MARATHON_GAP_MIN = 50
# Leon 19 Nov: YouTube = a fixed few Shorts a day (quota: ~1,600 an upload, one episode a day comes first). The repo variable
# CLIPBOT_SHORT_SLOTS ("11:00,14:00,17:00,20:00") replaces the hourly marathon slots; the gap follows the slots (>= 2 h).
if os.environ.get("CLIPBOT_SHORT_SLOTS", "").strip():
    MARATHON_SLOTS = [s.strip() for s in os.environ["CLIPBOT_SHORT_SLOTS"].split(",") if re.fullmatch(r"\d{2}:\d{2}", s.strip())]
    MARATHON_PER_DAY = len(MARATHON_SLOTS)
    MARATHON_GAP_MIN = 120
# Catch-up pace for a missed or clashing TikTok slot outside a marathon (Owner 2026-10-29: up to 4 a day).
CATCHUP_SLOTS = ["18:00", "13:00", "21:00", "10:00"]
CATCHUP_PER_DAY = 4
MARATHON_MIN_S = 10 * 3600


def env(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"missing env {name}")
    return v


def http(url, data=None, headers=None, method=None, timeout=60, retries=3):
    """JSON in, JSON out. Retries 429/5xx and network drops; raises on anything else."""
    headers = {"User-Agent": "ClipBot/1.0", **(headers or {})}   # CF 1010 blocks Python-urllib
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode()
        headers.setdefault("Content-Type", "application/json")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                out = json.loads(body) if body.strip() else {}
                _walk(out)
                return out
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
    """A Brand Account and the consent picker has confused channels
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


def marker_tag(marker):
    """The idempotency marker as a hidden YouTube tag (19 Nov copy standard: no internal codes in public text). Tags are
    not shown on the watch page; videos.list returns them to us, so "is it already up?" is still asked of YouTube."""
    import hashlib
    return "cb" + hashlib.sha1(marker.encode()).hexdigest()[:12]


def has_marker(video, marker):
    sn = video.get("snippet") or {}
    return marker in (sn.get("description") or "") or marker_tag(marker) in (sn.get("tags") or [])


def find_marker(marker, uploads):
    for v in uploads:
        if has_marker(v, marker):
            return v["id"]
    return None


def _iso_dur(s):
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    return sum(int(x or 0) * k for x, k in zip(m.groups(), (3600, 60, 1))) if m else 0


def marathon_mode():
    """True while a marathon is on or just ended: a 10 h+ stream live now or ended in the last 72 h (a live
    marathon counts once it passes 10 h). Leon streams on Kick only (18 Nov), so this reads his Kick video list
    (polished/source.py: KICK_SLUG, checked against KICK_CHANNEL_ID); a stream ON AIR has duration 0 there and counts
    by its time since start. Any error -> False (the normal 2 a day). Owner only: everyone else is always 2 a day."""
    if WHO != "leon":
        return False
    # 25 Nov (Leon: "we need them more spread out"): his NORMAL streams run 10 h+, so the automatic marathon pace kept
    # TikTok booking Shorts one an HOUR. Marathon pace is now opt-in only: repo variable CLIPBOT_MARATHON=1.
    if os.environ.get("CLIPBOT_MARATHON") != "1":
        return False
    try:
        from polished import source
        vids = source.kick_videos()[:5]
    except Exception as e:
        print(f"marathon check failed ({str(e)[:80]}): 2 a day")
        return False
    now = time.time()
    for v in vids:
        start = source._unix(v.get("start_time") or v.get("created_at"))
        if not start:
            continue
        d = now - start if v.get("is_live") else float(v.get("duration") or 0) / 1000.0   # Kick: ms
        if d >= MARATHON_MIN_S and now - (start + d) < 72 * 3600:
            return True
    return False


def next_slot(taken_utc, uploads, min_lead_h=3, marathon=False, catchup=False):
    """Next free slot: first UK day with fewer than MAX_PER_DAY Shorts, at the first free slot time (nothing within
    90 min) at least min_lead_h away. Marathon: the earliest free hour 09:00-23:00 UK, no daily cap, 50 min apart.
    catchup: TikTok's 4-a-day catch-up pace."""
    cap, slots = (MARATHON_PER_DAY, MARATHON_SLOTS) if marathon else (CATCHUP_PER_DAY, CATCHUP_SLOTS) if catchup         else (MAX_PER_DAY, DAY_SLOTS)
    gap = MARATHON_GAP_MIN if marathon else 90
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
                if any(abs((utc - b).total_seconds()) < gap * 60 for b in busy):
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


# ---------------- worker ----------------
def who_fields():
    """`who` for worker payloads: nothing for the owner (his payloads stay exactly as they were)."""
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
    """GitHub Release holding a batch's clips. The owner's names are unchanged (clips-<batch>); anyone else's
    carry his name, so a Twitch-clip batch (a unix second) can never land on the owner's release."""
    return f"clips-{batch}" if WHO == "leon" else f"clips-{WHO}-{batch}"


def check_worker_knows_who():
    """Before anything for a person other than the owner reaches the worker: the worker must echo his `who` on
    the queue route. An older worker ignores ?who and would treat his clips as the owner's (the owner's DMs, the owner's
    queue, the owner's channel), so refuse rather than act blind."""
    if WHO == "leon":
        return
    got = worker("/clipbot/queue?who=" + WHO).get("who")
    if got != WHO:
        raise SystemExit(f"the worker does not know {WHO!r} yet (queue answered who={got!r}): deploy the worker first. Nothing sent.")


def worker(path, payload=None, method=None):
    if not WORKER:
        raise RuntimeError("missing env CLIPBOT_WORKER")
    return http(WORKER + path, payload, method=method,
                headers={"Authorization": "Bearer " + env("CLIPBOT_TOKEN")}, timeout=120)


# Boss 19 Nov (two Reels with old, wrong-game copy went out 5 min apart): (1) only copy written by the vision writer
# (clipbot/vision.py: it watched the clip, Leon's 19 Nov standard) may be posted on ANY platform; (2) a lane posts at
# most ONE item a run and never within PACE_H of its own last post, whatever backlog it has.
PACE_H = 3.0                     # Leon 24 Nov: spread out (was 2.0)
DAILY_CAP = 4                    # at most this many posts a day per lane (UTC day; the worker counts them)
STAGGER_MIN = 60                 # a lane also waits this long after ANY lane's post, so one clip never hits every platform at once


_OTHER_GAME = re.compile(r"(?i)(?<![A-Za-z0-9])#?(gtav|gta5|gtaonline|seaofthieves|sea of thieves|ark|ark survival\w*|cyberpunk\w*|"
                         r"watch ?dogs\w*|assassin'?s ?creed\w*|acrogue|bg3|baldur'?s ?gate\w*|minecraft|fortnite|warzone|valorant|"
                         r"farcry\w*)(?![A-Za-z0-9])")
_OLD_LIVE = re.compile(r"(?i)live on twitch|twinkiesghost|twitch\.tv")


def copy_problems(it):
    """Leon's 19 Nov copy rules, checked on the FINISHED item (boss 23 Nov: 18 queued rows all failed them). [] = clean.
    Never on an item that has no copy_std: that alone is a refusal."""
    out = []
    texts = [str(it.get(k) or "") for k in ("title", "description", "tiktok_caption", "ig_caption", "tiktok_hook", "ig_hook", "x_text")]
    tags = [str(t) for k in ("hashtags", "tiktok_hashtags", "ig_hashtags") for t in (it.get(k) or [])]
    blob = " ".join(texts + tags)
    if _OTHER_GAME.search(blob):
        out.append("another game's name or tag")
    if _OLD_LIVE.search(blob):
        out.append("old Twitch line or handle")
    if os.environ.get("CLIPBOT_GTA6_ONLY") == "1":   # set in the posting workflows: every caption says GTA 6
        for k in ("hashtags", "tiktok_hashtags", "ig_hashtags"):   # each platform's own list, when it has one
            if it.get(k) and not {"#gta6", "#gtavi"} <= {str(t).lower() for t in it[k]}:
                out.append(f"{k}: missing #GTA6 #GTAVI")
        if not any(it.get(k) for k in ("hashtags", "tiktok_hashtags", "ig_hashtags")):
            out.append("no hashtags")
        if not re.match(r"(?i)\s*GTA ?(6|VI)(?![A-Za-z0-9])", str(it.get("title") or "")):
            out.append("title does not say GTA 6")
    return out


def copy_ok(it):
    from .vision import COPY_STD
    return str(it.get("copy_std") or "") == COPY_STD and not copy_problems(it)


def pace_ok(lane, platform_last=None, now=None):
    """True when this lane's last post is PACE_H or more ago. Two sources: the worker's record of our last post on the
    lane (set when the post is reported) and, if given, the platform's own newest post time. Unreadable = False."""
    now = now or datetime.now(UTC)
    try:
        got = worker(f"/clipbot/lastpost?lane={lane}")
        rec, rec_today = got.get("at"), got.get("today", 0)
    except Exception as e:   # noqa: BLE001 - fail closed: no proof of the gap, no post
        print(f"{lane}: could not read the last post time ({str(e)[:120]}); nothing posted this run")
        return False
    lasts = []
    for x in (rec, platform_last):
        if x:
            try:
                lasts.append(datetime.fromisoformat(str(x).replace("Z", "+00:00").replace("+0000", "+00:00")))
            except ValueError:
                print(f"{lane}: unreadable last post time {str(x)[:40]!r}; nothing posted this run")
                return False
    if lasts and now - max(lasts) < timedelta(hours=PACE_H):
        print(f"{lane}: last post {max(lasts).strftime('%H:%M')}Z is under {PACE_H:g} h ago; the next one waits for a later run")
        return False
    if int(rec_today or 0) >= DAILY_CAP:
        print(f"{lane}: {rec_today} posts today already (cap {DAILY_CAP}); the next one waits for tomorrow")
        return False
    # stagger: a nicety, so an unreadable answer never blocks a post. 25 Nov: ONLY between Reels and X. TikTok's lane runs
    # hourly at :40 and refreshes its record, so "wait for TikTok" kept IG/X (hourly at :06) waiting forever: 0 posts.
    for other in {"ig": ("x",), "x": ("ig",)}.get(lane, ()):
        try:
            o = worker(f"/clipbot/lastpost?lane={other}").get("at")
            if o and now - datetime.fromisoformat(str(o).replace("Z", "+00:00")) < timedelta(minutes=STAGGER_MIN):
                print(f"{lane}: {other} posted under {STAGGER_MIN} min ago; this lane waits so the platforms are spread out")
                return False
        except Exception:   # noqa: BLE001
            pass
    return True


def uk(dt):
    return dt.astimezone(UK).strftime("%a %d %b %H:%M UK")


# broad filler every gaming clip carries; it adds no reach (owner 10-29: "not the same generic ones")
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
    Validated hard; any problem -> a plain fallback so a clip is never blocked on it. the owner
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
    # TikTok gets its own caption line + hashtags (owner 10-23: "best hashtags for tiktok and youtube per clip")
    hook = r.get("tiktok_hook")
    out["tiktok_hook"] = re.sub(r"#\S+", "", hook).strip()[:120] if isinstance(hook, str) else ""
    own = "#" + LOGIN
    tt = _tags(r.get("tiktok_hashtags"), 5)
    out["tiktok_hashtags"] = tt + [own] if own not in tt else tt
    # Instagram: its own opener + tags (owner 10-29), at most 5 tags including ours
    hook = r.get("ig_hook")
    out["ig_hook"] = re.sub(r"#\S+", "", hook).strip()[:140] if isinstance(hook, str) else ""
    ig = _tags(r.get("ig_hashtags"), 4)
    out["ig_hashtags"] = ig + [own] if own not in ig else ig
    if isinstance(r.get("game"), str) and r["game"].strip() and not out["game"]:
        out["game"] = r["game"].strip()[:60]
    if not out["description"]:
        out["description"] = f"{title}. Catch the chaos live at {live_text()}"
    for k in ("description", "tiktok_hook", "ig_hook"):          # no em dashes in anything public
        out[k] = re.sub(r"\s*[\u2014\u2013]\s*", ", ", out[k]).strip(", ")
    if "#shorts" not in out["hashtags"]:
        out["hashtags"].append("#shorts")
    return out


# Owner 2026-10-31: every auto upload gets its own first comment: a line about the clip, then the streams.
# Picked per clip from a fixed set (no AI: nothing off-brand reaches a public comment), no em dashes.
PROMO = [
    "{t} 😭 this is what my streams are like every night. Come hang out live: {link}",
    "{t} 💀 and that was not even the worst bit of the stream. Catch the rest live: {link}",
    "{t}... I promise I am good at games sometimes 😂 Come see for yourself: {link}",
    "Still not over \"{t}\" 😂 Stuff like this happens live all the time: {link}",
    "{t} 👀 want more like this? I stream it all live, come say hi: {link}",
]


def promo_comment(title, key, link=None):
    import re
    import zlib
    t = re.sub(r"#\S+", "", title or "").strip().rstrip(".!?,")
    t = t[:1].upper() + t[1:]
    if not t:
        return f"Still not over this one 😂 Stuff like this happens live all the time: {link or live_text()}"
    line = PROMO[zlib.crc32(key.encode()) % len(PROMO)]
    return line.format(t=t, link=link or live_text())

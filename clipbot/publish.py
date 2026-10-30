"""Drain the approval queue held by the Forge worker: put each clip Leon approved on YouTube as
Scheduled at the next free slot (max 2 Shorts per UK day), then tell the worker (which edits the
Discord message). Only approved items are ever in the queue.

    python -m clipbot.publish

Queue item (written by the worker on approval):
  {"id": "vod:<vod>:<n>" | "clip:<slug>", "kind": "vod"|"clip", "vod"/"batch", "n", "title",
   "retitled", "description", "hashtags", "tags", "run_id", ...}

Idempotent: every upload carries the marker line `clipbot:<id>` in its description. If the
marker is already on YouTube:
  * private with no publishAt (uploaded but never scheduled) -> just schedule it, with the
    approved title/description/tags (no second upload)
  * scheduled or public -> mark done, skip
If YouTube cannot be read, nothing is uploaded.

Drip, not dump (Leon 2026-10-21: "stop it uploading the whole backlog to YouTube at once"): an
approved clip stays in the queue until its slot is within HORIZON_H, so YouTube only ever holds the
next day or so of Shorts. The twice-daily cron releases the rest, oldest approval batch first
(clip releases are deleted after 30 days, like the Discord offer).
"""
import glob
import io
import os
import re
import sys
import time
import urllib.error
import urllib.request
import subprocess
import zipfile

from . import common, render

SUFFIX = f" | {common.BRAND}"                      # " | TwinkiesGhost" for Leon
LIVE = f"Catch the chaos live: https://twitch.tv/{common.LOGIN}"
HORIZON_H = 24


def yt_title(t):
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*\|\s*" + re.escape(common.BRAND) + r"\s*$", "", t, flags=re.I)
    room = 100 - len(SUFFIX)
    return t[:room].rstrip() if len(t) > room else t + SUFFIX


def mask(text):
    return re.sub(r"[A-Za-z']+", lambda m: render.mask_word(m.group(0)), text)


def build_description(it, marker):
    """Approved description + approved hashtags + the live link + marker. Brand rules: says the
    person's own handle (twinkiesghost for Leon), no Kick link, no swearing."""
    desc = (it.get("description") or "").strip()
    tags = [h if h.startswith("#") else "#" + h for h in (it.get("hashtags") or [])]
    if not desc:
        copy = fetch_copy(it)
        desc, tags = copy.get("description", ""), tags or copy.get("hashtags", [])
    desc = mask(re.sub(r"https?://(www\.)?kick\.com\S*", "", desc)).strip()
    if common.LOGIN not in desc.lower():
        desc = (desc + "\n\n" + LIVE).strip()
    tags = [t for t in dict.fromkeys(t.lower() for t in tags) if re.fullmatch(r"#[a-z0-9_]{2,40}", t)]
    if "#shorts" not in tags:
        tags.append("#shorts")
    return f"{desc}\n\n{' '.join(tags[:12])}\n\n{marker}"


def fetch_copy(it):
    try:
        r = common.worker("/clipbot/copy", {"title": it["title"], "game": it.get("game", ""), "transcript": "", **common.who_fields()})
        return r if isinstance(r.get("description"), str) else {}
    except Exception:
        return {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def decrypt(blob):
    """Clip zips are AES-encrypted before upload (public repo, 2026-10-30); key = CLIPBOT_ZIP_KEY secret."""
    r = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", "env:CLIPBOT_ZIP_KEY"],
                       input=blob, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("could not decrypt the clips (CLIPBOT_ZIP_KEY wrong or missing)")
    return r.stdout


def fetch_artifact(run_id, name, dest):
    """Download a batch's clips. Since 2026-10-28 they live as a GitHub Release asset `<name>.zip`
    (tag = name), because artifacts hit the 0.5 GB Actions storage quota and broke every job; older
    batches fall back to the run's artifact. Either way GitHub answers with a 302 to blob storage,
    and that storage REJECTS our GitHub Authorization header (the 401 of 2026-10-19), so follow the
    redirect by hand without it."""
    repo, tok = common.env("GITHUB_REPOSITORY"), common.env("GITHUB_TOKEN")
    gh = {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json", "User-Agent": "TwinkiesGhost-ClipBot/1.0"}
    url = None
    enc = False
    try:
        rel = common.http(f"https://api.github.com/repos/{repo}/releases/tags/{name}", headers=gh)
        assets = {x["name"]: x for x in rel.get("assets", [])}
        asset = assets.get(name + ".zip.enc") or assets.get(name + ".zip")
        if asset:
            url, enc = asset["url"], asset["name"].endswith(".enc")
            gh = {**gh, "Accept": "application/octet-stream"}
    except RuntimeError as e:
        if "-> 404" not in str(e):
            raise
    if not url:
        arts = common.http(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts", headers=gh)["artifacts"]
        art = next((x for x in arts if x["name"] == name and not x["expired"]), None)
        if not art:
            raise RuntimeError(f"clips {name} (run {run_id}) are gone: no release asset and no artifact")
        url = art["archive_download_url"]
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(urllib.request.Request(url, headers=gh), timeout=60)
        raise RuntimeError("expected a redirect for the clips download")
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        loc = e.headers["Location"]
    with urllib.request.urlopen(urllib.request.Request(loc, headers={"User-Agent": "TwinkiesGhost-ClipBot/1.0"}), timeout=600) as r:
        blob = r.read()
    zipfile.ZipFile(io.BytesIO(decrypt(blob) if enc else blob)).extractall(dest)


def video_file(it, work):
    batch = it.get("batch") or it["vod"]
    d = os.path.join(work, f"art-{batch}")
    if not os.path.isdir(d):
        fetch_artifact(it["run_id"], common.release_name(batch), d)
    path = os.path.join(d, f"clip{it['n']}.mp4")
    if not os.path.exists(path):
        raise RuntimeError(f"clip{it['n']}.mp4 missing from artifact")
    if it.get("retitled"):
        # Leon changed the title in Discord: the hook burned on screen must match it
        raw = os.path.join(d, f"raw{it['n']}.mp4")
        path = os.path.join(d, f"clip{it['n']}-retitled.mp4")
        render.render(raw, path, os.path.join(d, f"prev{it['n']}-retitled.mp4"), hook=it["title"])
    return path


def schedule_existing(vid, title, desc, tags, at):
    """Video already uploaded but never scheduled: one videos.update (50 units) carrying every
    field, because update REPLACES snippet and status."""
    cur = common.yt("videos", part="snippet,status", id=vid)["items"][0]
    st = cur["status"]
    body = {"id": vid,
            "snippet": {"title": title, "description": desc, "tags": tags, "categoryId": cur["snippet"].get("categoryId", "20")},
            "status": {"privacyStatus": "private", "publishAt": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "selfDeclaredMadeForKids": False, "embeddable": st.get("embeddable", True),
                       "license": st.get("license", "youtube"), "publicStatsViewable": st.get("publicStatsViewable", True)}}
    common.http("https://www.googleapis.com/youtube/v3/videos?part=snippet,status", body, method="PUT",
                headers={"Authorization": "Bearer " + common.yt_token()})


def main():
    q = common.worker("/clipbot/queue" + ("" if common.WHO == "leon" else "?who=" + common.WHO))
    if q.get("who", "leon") != common.WHO:
        # an older worker ignores ?who and answers with LEON's queue: never put that on someone else's channel
        raise SystemExit(f"queue is {q.get('who', 'leon')!r}'s, not {common.WHO!r}'s (deploy the Forge first). Nothing uploaded.")
    items, ledger = q.get("items", []), q.get("ledger", [])
    mine = [x for x in items if x.get("who", "leon") == common.WHO]
    if len(mine) != len(items):
        print(f"ignoring {len(items) - len(mine)} queue item(s) that belong to someone else")
    items = mine
    if not items:
        print("queue empty")
        return
    common.channel_gate()
    uploads = common.recent_uploads(50)            # raises if YouTube cannot be read -> nothing uploaded
    marathon = common.marathon_mode()
    print("marathon mode: up to 4 Shorts a day" if marathon else "normal: up to 2 Shorts a day")
    taken = [x["at"] for x in ledger if x.get("at")]
    work = os.path.abspath("work")
    os.makedirs(work, exist_ok=True)
    order = sorted(items, key=lambda x: (int(x.get("run_id") or 0), x.get("n", 0)))
    for i, it in enumerate(order):
        marker = f"clipbot:{it['id']}"
        try:
            title = yt_title(it["title"])
            desc = build_description(it, marker)
            tags = [t for t in (it.get("tags") or []) if isinstance(t, str)][:15] + [common.LOGIN, "shorts"]
            tags = list(dict.fromkeys(t.strip() for t in tags if t.strip()))
            matches = [v for v in uploads if marker in v["snippet"].get("description", "")]
            # a done copy wins over a stuck private one (a once-public video keeps its marker after its fresh re-upload)
            existing = next((v for v in matches if v["status"].get("publishAt") or v["status"]["privacyStatus"] != "private"),
                            matches[0] if matches else None)
            if existing and (existing["status"].get("publishAt") or existing["status"]["privacyStatus"] != "private"):
                print(f"{it['id']}: already scheduled/public as {existing['id']}, marking done")
                common.worker("/clipbot/published", {"id": it["id"], "video": existing["id"], "at": existing["status"].get("publishAt"), "dup": True, **common.who_fields()})
                continue
            at = common.next_slot(taken, uploads, marathon=marathon)
            if at > common.datetime.now(common.UTC) + common.timedelta(hours=HORIZON_H):
                print(f"next slot {common.uk(at)} is over {HORIZON_H}h away: holding {len(order) - i} approved clip(s) for a later run")
                break
            vid = None
            if existing:
                try:
                    schedule_existing(existing["id"], title, desc, tags, at)
                    vid = existing["id"]
                except RuntimeError as e:
                    if "invalid scheduled publishing time" not in str(e):
                        raise
                    # YouTube never schedules a video that was ever public (60SBrYojK1I, 10-19). Leon chose:
                    # upload a fresh copy into the slot, leave the old one private.
                    print(f"{it['id']}: {existing['id']} was public once and cannot be scheduled -> fresh upload")
            if not vid:
                vid = common.upload(video_file(it, work), title, desc, at, tags=tags)
            taken.append(at.strftime("%Y-%m-%dT%H:%M:%SZ"))
            for _ in range(8):                       # YouTube shows publishAt seconds late: 3 false "failed" DMs on 10-19
                got = common.yt("videos", part="status", id=vid)["items"][0]["status"]
                if got.get("publishAt"):
                    break
                time.sleep(5)
            if got.get("publishAt") is None:
                raise RuntimeError(f"{vid} has no publishAt after scheduling (status {got})")
            print(f"{it['id']}: {vid} {got.get('privacyStatus')} until {got.get('publishAt')} ({common.uk(at)})")
            common.worker("/clipbot/published", {"id": it["id"], "video": vid, "at": got.get("publishAt"),
                                                 "when": common.uk(at), "title": title, **common.who_fields()})
        except Exception as e:
            print(f"{it['id']}: FAILED {str(e)[:300]}")
            try:
                common.worker("/clipbot/failed", {"id": it["id"], "error": str(e)[:300], **common.who_fields()})
            except Exception:
                pass
    for f in glob.glob(os.path.join(work, "**", "*"), recursive=True):
        if os.path.isfile(f):
            os.remove(f)


if __name__ == "__main__":
    sys.exit(main())

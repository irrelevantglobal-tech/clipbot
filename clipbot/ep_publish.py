"""Schedule approved episodes (GTA 6 marathon lane). Episodes are already on YouTube, PRIVATE and
unscheduled (episodes.py); approval in Discord only marks them approved in the worker. This gives each
one its Part number, title, date and playlist.

    python -m clipbot.ep_publish [--dry-run]

Rules (Leon 2026-10-22, PLAN.md):
  * Part numbers follow VOD order. A skipped episode gets no number. An episode still waiting for a
    decision holds back every later one, so Part 5 can never be dated before Part 4.
  * Slots: 12:00 + 18:00 UK during the launch fortnight (19 Nov - 2 Dec 2026), then 18:00 only.
    No two long-form videos within 90 min of each other, each Part after the one before.
  * Drip, not dump: only episodes whose slot is within HORIZON_H are written to YouTube; the rest wait
    for the next run (twice daily + on every approval).
  * One videos.update per episode carrying every field (update REPLACES snippet and status), then the
    playlist (EP_PLAYLIST, skipped if not set). The video must carry this episode's marker, be ours and
    still be private; anything else stops that episode and tells Leon.
  * Quota error = stop the run, never retry in a loop.
"""
import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta

from . import common, episodes

LAUNCH = (date(2026, 11, 19), date(2026, 12, 2))
LAUNCH_SLOTS, NORMAL_SLOTS = ["12:00", "18:00"], ["18:00"]
HORIZON_H = 48
GAP_MIN = 90


def slots_for(day):
    return LAUNCH_SLOTS if LAUNCH[0] <= day <= LAUNCH[1] else NORMAL_SLOTS


def next_ep_slot(after_utc, busy_utc, now=None, min_lead_h=3):
    now = now or datetime.now(common.UTC)
    floor = max(now + timedelta(hours=min_lead_h), after_utc + timedelta(minutes=1) if after_utc else now)
    day = floor.astimezone(common.UK).date()
    for _ in range(400):
        for hm in slots_for(day):
            h, m = map(int, hm.split(":"))
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=common.UK).astimezone(common.UTC)
            if t < floor:
                continue
            if any(abs((t - b).total_seconds()) < GAP_MIN * 60 for b in busy_utc):
                continue
            return t
        day += timedelta(days=1)
    raise SystemExit("no free episode slot in 400 days")


def parse(t):
    return datetime.fromisoformat(t.replace("Z", "+00:00")) if t else None


def long_form_busy(uploads):
    out = []
    for v in uploads:
        if common._iso_dur(v["contentDetails"].get("duration")) <= 180:
            continue
        st = v["status"]
        if st.get("publishAt") and st.get("privacyStatus") == "private":
            out.append(parse(st["publishAt"]))
    return out


def order(items):
    by = {}
    for it in items:
        by.setdefault(it.get("series") or "GTA 6", []).append(it)
    for s in by.values():
        s.sort(key=lambda x: (x.get("vod_created") or "", int(x.get("vod") or 0), float(x.get("start") or 0)))
    return by


def plan(items, uploads, now=None):
    """What to do now: [(item, part, slot_utc)]. Pure: no writes."""
    now = now or datetime.now(common.UTC)
    busy = long_form_busy(uploads) + [parse(i["at"]) for i in items if i.get("status") == "scheduled" and i.get("at")]
    todo = []
    for series, eps in order(items).items():
        n, last = 0, None
        for it in eps:
            st = it.get("status")
            if st == "skipped":
                continue
            if st == "scheduled":
                n, last = int(it.get("part") or n + 1), parse(it.get("at")) or last
                continue
            if st != "approved":
                print(f"{series}: {it['key']} is still waiting for a decision; later episodes wait too")
                break
            slot = next_ep_slot(last, busy, now)
            if slot - now > timedelta(hours=HORIZON_H):
                print(f"{series}: Part {n + 1} would go out {common.uk(slot)}; held until it is within {HORIZON_H} h")
                break
            n += 1
            todo.append((it, n, slot))
            busy.append(slot)
            last = slot
    return todo


def schedule(it, part, slot, playlist, dry):
    vid = it["video"]
    cur = common.yt("videos", part="snippet,status,contentDetails", id=vid).get("items", [])
    if not cur:
        raise RuntimeError(f"{vid} is not on YouTube any more")
    cur = cur[0]
    if cur["snippet"].get("channelId") != common.CHANNEL_ID:
        raise RuntimeError(f"{vid} is not on TwinkiesGhost")
    if it["marker"] not in cur["snippet"].get("description", ""):
        raise RuntimeError(f"{vid} does not carry {it['marker']}; refusing to touch it")
    st = cur["status"]
    if st.get("privacyStatus") != "private":
        raise RuntimeError(f"{vid} is {st.get('privacyStatus')}, not private; refusing to schedule it")
    if st.get("publishAt"):
        print(f"  {vid} already has publishAt {st['publishAt']}; reporting it, not changing it")
        return parse(st["publishAt"]), cur["snippet"]["title"]
    series = it.get("series") or "GTA 6"
    title = episodes.title_for(series, part, it["hook"])
    desc = episodes.description_for(it.get("description") or "", it.get("hashtags") or [], it["marker"])
    probs = episodes.check_meta(title, desc)
    if probs:
        raise RuntimeError(f"{it['key']}: metadata breaks the rules {probs}")
    tags = episodes.tags_for(series, part)
    print(f"  Part {part}: {vid} -> '{title}' at {common.uk(slot)}")
    if dry:
        return slot, title
    body = {"id": vid,
            "snippet": {"title": title, "description": desc, "tags": tags, "categoryId": cur["snippet"].get("categoryId", "20")},
            "status": {"privacyStatus": "private", "publishAt": slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "selfDeclaredMadeForKids": False, "embeddable": st.get("embeddable", True),
                       "license": st.get("license", "youtube"), "publicStatsViewable": st.get("publicStatsViewable", True)}}
    common.http("https://www.googleapis.com/youtube/v3/videos?part=snippet,status", body, method="PUT",
                headers={"Authorization": "Bearer " + common.yt_token()})
    for _ in range(8):                               # publishAt reads back late (10-19)
        got = common.yt("videos", part="status", id=vid)["items"][0]["status"]
        if got.get("publishAt"):
            break
        time.sleep(5)
    else:
        raise RuntimeError(f"{vid} shows no publishAt after scheduling; check it before rerunning")
    if playlist:
        have = common.yt("playlistItems", part="id", playlistId=playlist, videoId=vid).get("items", [])
        if not have:
            common.http("https://www.googleapis.com/youtube/v3/playlistItems?part=snippet",
                        {"snippet": {"playlistId": playlist, "resourceId": {"kind": "youtube#video", "videoId": vid}}},
                        headers={"Authorization": "Bearer " + common.yt_token()})
    else:
        print("  EP_PLAYLIST not set: playlist step skipped")
    thumbnail(it, part, series, vid)
    return slot, title


def thumbnail(it, part, series, vid):
    """Approved calm sunset thumbnail (Leon 2026-10-22). Never fails the scheduling: on any problem
    the episode keeps YouTube's own frame and Leon gets one line."""
    import tempfile
    import urllib.request
    from . import thumbs
    try:
        spans, cur = [], float(it["start"])
        for a, b in sorted(it.get("holes") or []):
            if a > cur:
                spans.append((cur, a))
            cur = max(cur, b)
        if float(it["end"]) > cur:
            spans.append((cur, float(it["end"])))
        work = tempfile.mkdtemp(prefix=f"thumb_{vid}_")
        path = thumbs.make(it["vod"], spans, it["hook"], part, series, os.path.join(work, "thumb.png"), work)
        if not path:
            raise RuntimeError("no usable frame")
        data = open(path, "rb").read()
        req = urllib.request.Request(f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={vid}", data=data,
                                     method="POST", headers={"Authorization": "Bearer " + common.yt_token(),
                                                             "Content-Type": "image/jpeg" if path.endswith(".jpg") else "image/png",
                                                             "User-Agent": "TwinkiesGhost-ClipBot/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
        print(f"  thumbnail set on {vid}")
    except Exception as e:
        print(f"  thumbnail skipped for {vid}: {str(e)[:200]}")
        try:
            common.worker("/clipbot/failed", {"id": f"thumbnail {it['key']}", "error": f"Part {part} is scheduled fine but kept YouTube's own thumbnail: {str(e)[:200]}"})
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    items = common.worker("/clipbot/eplist").get("items", [])
    if not any(i.get("status") == "approved" for i in items):
        print("no approved episodes")
        return
    common.channel_gate()
    todo = plan(items, common.recent_uploads(50))
    playlist = os.environ.get("EP_PLAYLIST", "").strip()
    for it, part, slot in todo:
        try:
            at, title = schedule(it, part, slot, playlist, a.dry_run)
        except RuntimeError as e:
            msg = str(e)
            common.worker("/clipbot/failed", {"id": f"episode {it['key']}", "error": msg})
            if "quota" in msg.lower() or "403" in msg:
                raise SystemExit(f"stopping: {msg}")
            print(f"  {it['key']}: {msg}; later episodes wait")
            break                                    # never date a later Part past a failed one
        if not a.dry_run:
            common.worker("/clipbot/epdone", {"key": it["key"], "video": it["video"], "part": part, "title": title,
                                              "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "when": common.uk(at)})


if __name__ == "__main__":
    sys.exit(main())

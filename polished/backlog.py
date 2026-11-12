"""Lane 3: keep every marathon hour that the cloud lanes did not polish, before Twitch deletes it.

    py -m polished.backlog status [--channel <login>]          VODs, age, days left, archived?, polished up to
    py -m polished.backlog pull <vod> [--dest F:\\gta6-backlog] [--max-mbps 40] [--from S --to S]
    py -m polished.backlog queue                                windows still to polish (cloud reach -> VOD end)

`pull` saves the VOD's own 1080p segments + playlist (fetch.py archive mode), resumable: a segment
already on disk at the right size is skipped, each write goes temp -> rename. It refuses unless the
rendition is 1920x1080 and the destination drive keeps 20 GB free after the pull. While OBS is running
(the owner is live) it is throttled to --max-mbps so the stream's own connection is left alone.

Retention seen 2026-11-12: VODs from 11-04 still up 8 days later, so the channel keeps more than the 7 days
non-Affiliates get; 14 days is assumed but unproven, so pull within 7 days of a stream to be safe.

Processing the archive later = the same conductor with POLISHED_ARCHIVE set and --lane local (laptop GPU,
never while OBS is live unless the Elgato-day load test passed AND the owner OKs it) or any cloud lane
(Modal's December credit): fetch() then reads from disk, nothing else changes.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polished import fetch  # noqa: E402

DEFAULT_DEST = r"F:\gta6-backlog"
FREE_KEEP_GB = 20
ASSUMED_KEEP_DAYS = 14
SAFE_DAYS = 7


def obs_live():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe"], capture_output=True, text=True).stdout
        return "obs64.exe" in out
    except OSError:
        return False


def vods(channel):
    out = subprocess.run(["yt-dlp", "--flat-playlist", "-J", "--no-warnings",
                          f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time"],
                         capture_output=True, text=True, encoding="utf-8")
    ents = json.loads(out.stdout or "{}").get("entries", [])
    res = []
    for e in ents:
        vid = str(e.get("id", "")).lstrip("v")
        info = json.loads(subprocess.run(["yt-dlp", "-J", "--no-warnings", f"https://www.twitch.tv/videos/{vid}"],
                                         capture_output=True, text=True, encoding="utf-8").stdout or "{}")
        ts = info.get("timestamp")
        res.append({"vod": vid, "created": dt.datetime.fromtimestamp(ts, dt.timezone.utc) if ts else None,
                    "hours": round((info.get("duration") or 0) / 3600, 2)})
    return res


def status(channel, dest):
    now = dt.datetime.now(dt.timezone.utc)
    for v in vods(channel):
        age = (now - v["created"]).total_seconds() / 86400 if v["created"] else -1
        d = os.path.join(dest, v["vod"])
        have = 0
        if os.path.exists(os.path.join(d, "playlist.json")):
            n = len(json.load(open(os.path.join(d, "playlist.json")))["segments"])
            have = sum(1 for i in range(n) if os.path.exists(os.path.join(d, "seg_%06d.ts" % i)))
            have = f"{have}/{n} segs"
        print(f"{v['vod']}  {v['created']:%d %b %H:%M}  {v['hours']:5.1f} h  age {age:4.1f} d  "
              f"safe-pull left {SAFE_DAYS - age:4.1f} d (assumed expiry {ASSUMED_KEEP_DAYS - age:4.1f} d)  archived: {have or 'no'}")


def pull(vod, dest, max_mbps, lo=0.0, hi=0.0):
    url, fps, total = fetch.rendition(vod)        # raises unless 1920x1080
    segs, recording = fetch.playlist(url)
    if recording:
        print("note: the VOD is still recording; pulling what exists, run again later for the rest")
    d = os.path.join(dest, str(vod))
    os.makedirs(d, exist_ok=True)
    pick = [i for i, (s, du, _) in enumerate(segs) if (s + du > lo) and (not hi or s < hi)]
    if 0 not in pick:
        pick = [0] + pick                          # segment 0 = the VOD's time origin, always kept
    est_gb = len(pick) * 10 * 8e6 / 8 / 1e9 * 1.1
    free_gb = shutil.disk_usage(os.path.splitdrive(os.path.abspath(d))[0] + os.sep).free / 1e9
    if free_gb - est_gb < FREE_KEEP_GB:
        raise SystemExit(f"{dest}: {free_gb:.0f} GB free, this pull needs ~{est_gb:.0f} GB + {FREE_KEEP_GB} GB kept free")
    json.dump({"vod": str(vod), "fps": fps, "segments": [[s, du] for s, du, _ in segs], "recording": recording,
               "pulled_at": dt.datetime.now(dt.timezone.utc).isoformat()},
              open(os.path.join(d, "playlist.json.part"), "w"))
    os.replace(os.path.join(d, "playlist.json.part"), os.path.join(d, "playlist.json"))
    t0, got_b = time.time(), 0
    for n, i in enumerate(pick):
        p = os.path.join(d, "seg_%06d.ts" % i)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            continue
        data = fetch._get(segs[i][2])
        open(p + ".part", "wb").write(data)
        os.replace(p + ".part", p)
        got_b += len(data)
        if max_mbps and obs_live():                # live: keep the average under max_mbps
            want = got_b * 8 / (max_mbps * 1e6)
            if want > time.time() - t0:
                time.sleep(want - (time.time() - t0))
        if n % 60 == 0:
            print(f"  {n + 1}/{len(pick)} segments, {got_b / 1e9:.1f} GB this run", flush=True)
    head = fetch.probe(os.path.join(d, "seg_000000.ts"))
    v = [s for s in head.get("streams", []) if s.get("codec_type") == "video"]
    if not v or (v[0].get("width"), v[0].get("height")) != (1920, 1080):
        raise SystemExit("archived segment 0 is not 1920x1080")
    print(f"done: {len(pick)} segments of VOD {vod} in {d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["status", "pull", "queue"])
    ap.add_argument("vod", nargs="?")
    ap.add_argument("--channel", default="")
    ap.add_argument("--dest", default=os.environ.get("POLISHED_ARCHIVE") or DEFAULT_DEST)
    ap.add_argument("--max-mbps", type=float, default=40.0)
    ap.add_argument("--from", dest="lo", type=float, default=0.0)
    ap.add_argument("--to", dest="hi", type=float, default=0.0)
    a = ap.parse_args()
    if a.what == "status":
        if not a.channel:
            from clipbot import common
            a.channel = common.LOGIN
        status(a.channel, a.dest)
    elif a.what == "pull":
        pull(a.vod, a.dest, a.max_mbps, a.lo, a.hi)
    else:
        print("queue = for each VOD: from the worker's clipbot:ep:cur:<vod> (where the cloud lanes reached) to the "
              "VOD end, plus any window the worker stepped over (its alert names it). Read the worker with "
              "GET /clipbot/eplist; polish each with: py -m polished.conductor --vod V --from S --to E --lane local "
              "(POLISHED_ARCHIVE set) or dispatch polished.yml on a cloud lane.")


if __name__ == "__main__":
    main()

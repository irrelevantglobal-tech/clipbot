"""Existing YouTube descriptions on Leon's channel: the Twitch link -> his Kick address (owner 18 Nov: "clips,
episodes, descriptions, pinned comments, all links").

    python -m clipbot.desc_sweep --out out                       dry run: what would change, what is skipped (~30 units)
    python -m clipbot.desc_sweep --out out --apply --max-units 3000

Rules (boss 18 Nov): EXACT-match replacements only. Each pair below may hit a text at most once (twice = the video is
skipped and listed); after the pairs, any "twitch" left in that text skips the video too (a human reads those). The
snippet description AND its localizations (every video mirrors an `en` one) change in ONE videos.update (50 units:
it REPLACES the snippet, so every snippet field is sent back as read). A pristine copy of each video's snippet +
localizations is written to <out>/backup/<id>.json BEFORE its update. Newest first; idempotent (a fixed video has no
Twitch left, so the next run skips it); stops before --max-units (the 10k/day pool is shared by every lane: leave
headroom for the marathon's uploads). Leon's channel only: the token's channel is gated, and every video's own
channelId is checked. Titles are never touched (a title with Twitch in it is only listed).
"""
import argparse
import json
import os
import re
import sys

from . import common

LIST_COST, UPDATE_COST = 1, 50SNIPPET_KEYS = ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")


def _names():
    """Leon's current handle AND the retired ones (CLIPBOT_ID "legacy": old links still point at the old handle)."""
    legacy = [n for n in common.IDENT.get("legacy", []) if n]
    return list(dict.fromkeys([common.LOGIN, common.LOGIN.lower(), common.BRAND, *legacy, *(n.lower() for n in legacy)]))


def pairs():
    """(old, new) exact strings, longest first. Built from the channel identity + the Kick address (no names in code)."""
    url = common.live_url()
    if not common.KICK_LIVE or not url:
        raise SystemExit("the Kick address is not cleared (KICK_SLUG + KICK_SLUG_PUBLIC=1), or this is not Leon's lane. "
                         "Nothing changed.")
    names = _names()
    out = [("WATCH ME LIVE ON TWITCH", "WATCH ME LIVE ON KICK"),
           ("check out the streams on Twitch!", "check out the streams on Kick!"),
           ("#TwitchStreamer", "#KickStreamer"), ("#twitchstreamer", "#kickstreamer"), ("#twitchclips", "#kickclips"),
           ("#Twitch ", "#Kick "), ("#twitch ", "#kick "), ("#Twitch\n", "#Kick\n"), ("#twitch\n", "#kick\n")]
    for n in names:
        for sp in ("", "  ", "   "):
            out += [(f"Twitch:{sp} https://www.twitch.tv/{n}", f"Kick:{sp} https://{url}"),
                    (f"Twitch:{sp} https://twitch.tv/{n}", f"Kick:{sp} https://{url}"),
                    (f"Twitch:{sp} twitch.tv/{n}", f"Kick:{sp} {url}")]
        out += [(f"Twitch:   / {n}", f"Kick:   {url}"), (f"Twitch: / {n}", f"Kick: {url}"),   # a link YouTube shows as "/ name"
                (f"live on Twitch: https://www.twitch.tv/{n}", f"live on Kick: https://{url}"),
                (f"live on Twitch: https://twitch.tv/{n}", f"live on Kick: https://{url}"),
                (f"WATCH ME LIVE\nTwitch: twitch.tv/{n}", f"WATCH ME LIVE\nKick: {url}"),
                (f"Live on Twitch: https://www.twitch.tv/{n}", f"Live on Kick: https://{url}"),
                (f"Live on Twitch: https://twitch.tv/{n}", f"Live on Kick: https://{url}"),
                (f"Live on Twitch: twitch.tv/{n}", f"Live on Kick: {url}"),
                (f"Live on Twitch: {n}", f"Live on Kick: {url}"),
                (f"https://www.twitch.tv/{n}", f"https://{url}"),
                (f"https://twitch.tv/{n}", f"https://{url}"),
                (f"www.twitch.tv/{n}", url),
                (f"twitch.tv/{n}", url)]
    return sorted(dict.fromkeys(out), key=lambda p: -len(p[0]))


def convert(text, prs):
    """-> (new_text, [old strings replaced]) or (None, why). Unchanged text -> (text, [])."""
    out, used = text or "", []
    for a, b in prs:
        n = out.count(a)
        if not n:
            continue
        if n > 1:
            return None, f"{a!r} appears {n} times"
        out = out.replace(a, b)
        used.append(a)
    # another streamer's own Twitch link (Glatiar, a friend in a co-op) is a credit, not Leon's channel: it stays.
    # Leon's own handles (current + retired) were all turned into Kick above, so any twitch.tv/<name> left is someone else.
    mine = {n.lower() for n in _names()}
    rest = re.sub(r"(?i)(?:https?://)?(?:www\.)?twitch\.tv/([A-Za-z0-9_]+)",
                  lambda m: m.group(0) if m.group(1).lower() in mine else "", out)
    m = re.search(r"(?i).{0,40}twitch.{0,40}", rest)
    if m:
        return None, "other Twitch text left: " + m.group(0).replace("\n", " / ")
    return out, used


def uploads():
    ch = common.yt("channels", part="contentDetails", id=common.CHANNEL_ID)["items"][0]
    pl = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, tok, units = [], "", LIST_COST
    while True:
        r = common.yt("playlistItems", part="contentDetails", playlistId=pl, maxResults=50, **({"pageToken": tok} if tok else {}))
        units += LIST_COST
        ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        tok = r.get("nextPageToken")
        if not tok:
            return list(dict.fromkeys(ids)), units


def videos(ids):
    out, units = [], 0
    for k in range(0, len(ids), 50):
        r = common.yt("videos", part="snippet,localizations", id=",".join(ids[k:k + 50]), maxResults=50)
        units += LIST_COST
        out += r.get("items", [])
    return out, units


def plan(items, prs):
    """-> (todo [(video, new_snippet, new_localizations, used)], skipped [(id, why)], titles_with_twitch [id])."""
    todo, skipped, titles = [], [], []
    for v in sorted(items, key=lambda v: v["snippet"].get("publishedAt", ""), reverse=True):
        sn, loc = v["snippet"], v.get("localizations") or {}
        if sn.get("channelId") != common.CHANNEL_ID:
            skipped.append((v["id"], "not this channel"))
            continue
        if re.search(r"(?i)twitch", sn.get("title", "")):
            titles.append(v["id"])
        texts = [sn.get("description", "")] + [l.get("description", "") for l in loc.values()]
        if not any(re.search(r"(?i)twitch", t or "") for t in texts):
            continue
        new_desc, used = convert(sn.get("description", ""), prs)
        if new_desc is None:
            skipped.append((v["id"], "description: " + used))
            continue
        new_loc, bad = {}, None
        for lang, l in loc.items():
            d, u = convert(l.get("description", ""), prs)
            if d is None:
                bad = f"localization {lang}: {u}"
                break
            new_loc[lang] = dict(l, description=d)
        if bad:
            skipped.append((v["id"], bad))
            continue
        snip = {k: sn[k] for k in SNIPPET_KEYS if k in sn}
        snip["description"] = new_desc
        todo.append((v, snip, new_loc, used))
    return todo, skipped, titles


def update(v, snip, loc):
    body = {"id": v["id"], "snippet": snip, **({"localizations": loc} if loc else {})}
    part = "snippet,localizations" if loc else "snippet"
    common.http(f"https://www.googleapis.com/youtube/v3/videos?part={part}", body, method="PUT",
                headers={"Authorization": "Bearer " + common.yt_token()})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max-units", type=int, default=3000)
    a = ap.parse_args(argv)
    if common.WHO != "leon":
        raise SystemExit("Leon's channel only. Nothing changed.")
    prs = pairs()
    common.channel_gate()
    os.makedirs(os.path.join(a.out, "backup"), exist_ok=True)
    ids, u1 = uploads()
    items, u2 = videos(ids)
    units = 1 + u1 + u2
    todo, skipped, titles = plan(items, prs)
    print(f"{len(ids)} uploads read ({units} units); {len(todo)} description(s) to change, {len(skipped)} skipped, "
          f"{len(titles)} title(s) mention Twitch (not touched)")
    done, failed = [], []
    for v, snip, loc, used in todo:
        if not a.apply:
            break
        if units + UPDATE_COST > a.max_units:
            print(f"stopping at the {a.max_units}-unit cap: {len(todo) - len(done) - len(failed)} left for the next run")
            break
        with open(os.path.join(a.out, "backup", v["id"] + ".json"), "w", encoding="utf-8") as f:
            json.dump({"id": v["id"], "snippet": v["snippet"], "localizations": v.get("localizations") or {}}, f, ensure_ascii=False, indent=1)
        try:
            update(v, snip, loc)
            units += UPDATE_COST
            done.append(v["id"])
        except Exception as e:   # noqa: BLE001 - quota or one bad video: record, and stop on quota
            failed.append((v["id"], str(e)[:200]))
            if "quota" in str(e).lower():
                print("YouTube quota exceeded: stopping")
                break
    rep = {"apply": a.apply, "units": units, "to_change": len(todo), "changed": done, "failed": failed,
           "skipped": [{"id": i, "why": w} for i, w in skipped], "titles_with_twitch": titles,
           "pending": [v["id"] for v, *_ in todo if v["id"] not in done]}
    with open(os.path.join(a.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f"{'changed' if a.apply else 'would change'} {len(done) if a.apply else len(todo)}; failed {len(failed)}; "
          f"skipped {len(skipped)}; units {units}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

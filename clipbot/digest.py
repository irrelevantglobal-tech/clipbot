"""Weekly "what worked" DM (owner 2026-11-14): the week's clips and episodes ranked by what people watched.

    python -m clipbot.digest           (DIGEST_DRY=1: print the message, send nothing)

YouTube views (2 quota units), Instagram Reel views (insights; likes if insights are refused). X: only how many
posts went out, because the Buffer key has no insights scope. Sent Sundays by digest.yml (+ the worker's backup timer).
"""
import os
import sys
from datetime import datetime, timedelta

from . import common, instagram

WEEK = timedelta(days=7)


def youtube(now):
    ups = common.recent_uploads(50)
    week = [v for v in ups if v["status"].get("privacyStatus") == "public"
            and now - datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00")) <= WEEK]
    if not week:
        return [], []
    stats = {v["id"]: v["statistics"] for v in common.yt("videos", part="statistics", id=",".join(v["id"] for v in week)).get("items", [])}
    rows = [(int(stats.get(v["id"], {}).get("viewCount", 0)), v["snippet"]["title"], v["id"],
             common._iso_dur(v["contentDetails"].get("duration")) > 180) for v in week]
    rows.sort(reverse=True)
    return [r for r in rows if not r[3]], [r for r in rows if r[3]]


def instagram_reels(now):
    tok = instagram.login()
    media = instagram.graph(f"/{tok['user_id']}/media", tok["token"],
                            {"fields": "id,caption,permalink,timestamp,media_type,like_count", "limit": 40}).get("data", [])
    out = []
    for m in media:
        t = datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00"))
        if now - t > WEEK or m.get("media_type") != "VIDEO":
            continue
        views = None
        try:
            ins = instagram.graph(f"/{m['id']}/insights", tok["token"], {"metric": "views"}, retries=0).get("data", [])
            views = ins[0]["values"][0]["value"] if ins else None
        except Exception:
            pass
        out.append((views, m.get("like_count", 0), (m.get("caption") or "").split("\n")[0][:60], m.get("permalink", "")))
    out.sort(key=lambda r: (r[0] or 0, r[1]), reverse=True)
    return out


def x_count(now):
    key = os.environ.get("BUFFER_API_KEY", "")
    if not key:
        return None
    from . import x
    org, chan = x.x_channel(key)
    q = """query($o: OrganizationId!, $c: ChannelId!) {
      posts(first: 100, input: {organizationId: $o, filter: {channelIds: [$c], status: [sent]},
             sort: [{field: dueAt, direction: desc}]}) { edges { node { id sentAt dueAt } } } }"""
    nodes = [e["node"] for e in x.gql(key, q, {"o": org, "c": chan["id"]})["posts"]["edges"]]
    when = lambda p: datetime.fromisoformat((p.get("sentAt") or p.get("dueAt") or "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
    print(f"X: {len(nodes)} sent posts read")
    return sum(1 for p in nodes if now - when(p) <= WEEK)


def message(shorts, eps, reels, xs):
    n = lambda v: f"{v:,}"
    lines = ["📊 **ClipBot week**"]
    if shorts:
        lines.append(f"\n**YouTube Shorts** ({len(shorts)} new, {n(sum(r[0] for r in shorts))} views)")
        lines += [f"{i}. {r[1][:70]} - {n(r[0])} <https://youtu.be/{r[2]}>" for i, r in enumerate(shorts[:5], 1)]
    if eps:
        lines.append(f"\n**Episodes** ({len(eps)} new)")
        lines += [f"{i}. {r[1][:70]} - {n(r[0])} <https://youtu.be/{r[2]}>" for i, r in enumerate(eps[:3], 1)]
    if reels:
        lines.append(f"\n**Instagram Reels** ({len(reels)} new)")
        lines += [f"{i}. {r[2] or 'Reel'} - " + (f"{n(r[0])} views" if r[0] is not None else f"{r[1]} likes") + f" <{r[3]}>"
                  for i, r in enumerate(reels[:5], 1)]
    if xs is not None:
        lines.append(f"\n**X**: {xs} posts went out (views need a Buffer key with insights)")
    if len(lines) == 1:
        lines.append("Nothing new went out this week.")
    return "\n".join(lines)[:1900]


def main():
    if common.WHO != "leon":
        return
    now = datetime.now(common.UTC)
    parts, errs = {}, []
    for name, fn in (("yt", youtube), ("ig", instagram_reels), ("x", x_count)):
        try:
            parts[name] = fn(now)
        except Exception as e:
            errs.append(f"{name}: {str(e)[:120]}")
            parts[name] = None
    shorts, eps = parts["yt"] or ([], [])
    text = message(shorts, eps, parts["ig"] or [], parts["x"])
    if errs:
        text = (text + "\n\n(could not read: " + "; ".join(errs) + ")")[:1900]
    print(text)
    if os.environ.get("DIGEST_DRY") != "1":
        common.worker("/clipbot/digest", {"content": text})


if __name__ == "__main__":
    sys.exit(main())

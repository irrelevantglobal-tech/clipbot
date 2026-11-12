"""The stream's own chat as a ranking signal: when chat reacts, something happened.

Calibration 2026-11-12 (6 recent VODs, picks the owner had already decided): approved picks had 0.83 chat
messages in the 30 s after them on average and 2 of 6 had a reaction (LUL, KEKW, lol, emotes...); the 41 he
skipped had 0.22 and none. Chat on the channel is thin today (25-40 messages an hour), so this only BOOSTS a
candidate's rank, it never gates one, and any failure here means no boost, never a failed run.

Source: Twitch's public web GraphQL (the same call the VOD player makes for chat replay). Paging by cursor is
refused ("failed integrity check"); paging by time offset works, so we step through the window by offset,
~3 requests a second. Only each message's time and a reaction flag are kept: no names, no text.
"""
import json
import re
import time
import urllib.request

GQL = "https://gql.twitch.tv/gql"
CLIENT = "kimne78kx3ncx6brgo4mv6wki5h1ko"    # Twitch's public web player client id (not a secret)
HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
REACT = re.compile(r"(?i)\b(lul|lmao+|lmf+ao|kekw|omegalul|lol+|haha\w*|xd|icant|pepelaugh|dead|bruh|wtf|omg|no way|"
                   r"wait what)\b|\U0001F602|\U0001F923|\U0001F480|\?\?+|!!+")
LAG = 3.0           # chat reacts a few seconds after the moment
AFTER = 30.0        # and keeps reacting for a while


def _page(vod, offset):
    body = json.dumps([{"operationName": "VideoCommentsByOffsetOrCursor",
                        "variables": {"videoID": str(vod), "contentOffsetSeconds": int(offset)},
                        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": HASH}}}]).encode()
    req = urllib.request.Request(GQL, data=body, method="POST", headers={
        "Client-Id": CLIENT, "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return ((json.loads(r.read())[0].get("data") or {}).get("video") or {}).get("comments") or {}


def timeline(vod, lo, hi, page=_page, pause=0.3, max_pages=600):
    """[(vod_second, reacted)] for messages in [lo, hi]. Empty on any failure (logged)."""
    seen, out, t, pages = set(), [], int(lo), 0
    try:
        while t < hi and pages < max_pages:
            edges = page(vod, t).get("edges") or []
            pages += 1
            top = t
            for e in edges:
                n = e.get("node") or {}
                off = n.get("contentOffsetSeconds")
                if n.get("id") in seen or off is None:
                    continue
                seen.add(n.get("id"))
                frags = (n.get("message") or {}).get("fragments") or []
                text = "".join(f.get("text") or "" for f in frags)
                if lo <= off <= hi:
                    out.append((off, bool(REACT.search(text)) or any(f.get("emote") for f in frags)))
                top = max(top, off)
            t = top + 1 if top > t else t + 30
            if pause:
                time.sleep(pause)
    except Exception as e:
        print(f"chat unavailable ({type(e).__name__}: {str(e)[:120]}): no chat boost this run")
        return []
    out.sort()
    return out


def around(tl, s, e):
    """(messages, reactions) from the moment's start + LAG to AFTER s past its end. tl, s, e in the same seconds."""
    hits = [r for t, r in tl if s + LAG <= t <= e + AFTER]
    return len(hits), sum(hits)


def boost(msgs, reacts):
    """Rank bonus: each message 0.4, each reaction 0.8 more, at most 2 (a strong judge verdict is worth 2.5)."""
    return min(2.0, 0.4 * msgs + 0.8 * reacts)

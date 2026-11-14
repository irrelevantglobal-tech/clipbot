"""'I'm live' promo: an X post and an Instagram Story the moment the stream starts.

    python -m clipbot.golive          (GOLIVE_DRY=1: render + Instagram processes the Story, nothing published)

Owner 2026-11-14 ("build it all"): the Forge worker dispatches golive.yml on each claimed go-live edge, at most once
per 3 h (marathon reconnects). The card is the stream's own Twitch preview frame + LIVE NOW + its title. Each lane is
tried once and reported on failure; one failing never stops the other.
"""
import os
import re
import sys
import time
import urllib.request

from . import common, ep_story, instagram, legacy, x

PREVIEW = "https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-1280x720.jpg"


def short_title(t):
    """Stream titles carry commands, separators and emoji: keep the words people read first."""
    t = re.sub(r"![\w-]+", "", t or "")
    t = re.sub(r"[^\w\s'&?.,:#-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -:,")
    return t[:80].rstrip(" -:,")


def preview(out, tries=12, wait=30):
    """Twitch makes the live preview a minute or two after go-live; until then the URL redirects to a 404 image."""
    url = PREVIEW.format(login=common.LOGIN.lower())
    for _ in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url + f"?t={int(time.time())}", headers={"User-Agent": "ClipBot/1.0"}), timeout=30) as r:
                if "404_preview" not in r.geturl():
                    open(out, "wb").write(r.read())
                    return out
        except Exception as e:
            print(f"preview not ready ({str(e)[:80]})")
        time.sleep(wait)
    return None


def card(title, work):
    from PIL import Image
    from .style import gta_style
    src = preview(os.path.join(work, "preview.jpg"))
    if not src:
        print("no live preview yet: card uses the camp scene")
        src = gta_style.sunset_scene(1280, 720, 8, seed=3).convert("RGB")
    png = os.path.join(work, "live.png")
    gta_style.live_story(src, short_title(title), out=png)
    jpg = os.path.join(work, "live.jpg")
    Image.open(png).convert("RGB").save(jpg, "JPEG", quality=90)
    return jpg


def x_text(title):
    t = short_title(title)
    return (f"LIVE NOW: {t}\n\n" if t else "LIVE NOW\n\n") + f"https://twitch.tv/{common.LOGIN.lower()}"


def post_x(path, title, dry):
    key = os.environ.get("BUFFER_API_KEY", "")
    if not key:
        print("X: no Buffer key, skipped")
        return
    _, chan = x.x_channel(key)
    text = x_text(title)
    legacy.check(text, where="go-live X")
    if dry:
        print(f"X: DRY, not posted: {text!r}")
        return
    parked = instagram.park(path)
    try:
        pid = x.create(key, chan["id"], text, parked["url"] + ".jpg", kind="image")
        link = x.wait_sent(key, pid)
        print(f"X: posted {pid} {link or '(still sending)'}")
        if link is None:
            parked = None
    finally:
        if parked:
            instagram.unpark([parked])


def main():
    if common.WHO != "leon":
        return
    dry = os.environ.get("GOLIVE_DRY") == "1"
    s = common.helix(f"streams?user_id={common.TWITCH_UID}").get("data", [])
    if not s and not dry:
        print("not live (a false go-live edge): nothing posted")
        return
    title = s[0]["title"] if s else "GTA 6 launch marathon"
    legacy.check(title, where="go-live title")
    work = os.path.abspath("work")
    os.makedirs(work, exist_ok=True)
    path = card(title, work)
    failed = []
    for name, fn in (("Instagram Story", lambda: print("Instagram:", ep_story.post_story(instagram.login(), path, dry) or "DRY, container FINISHED")),
                     ("X post", lambda: post_x(path, title, dry))):
        try:
            fn()
        except Exception as e:
            print(f"{name} FAILED {str(e)[:300]}")
            failed.append(name)
            try:
                common.worker("/clipbot/failed", {"id": f"Go-live {name}", "error": str(e)[:300]})
            except Exception:
                pass
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())

"""Schedule approved Shorts on TikTok too, natively in TikTok Studio, from a headless browser.

    python -m clipbot.tiktok            (TT_DRY=1: everything except the final Schedule request)

Leon 2026-10-23: "do the same thing with tiktok, schedule 2 a day, the same ones approved in discord
for youtube". The TikTok API cannot schedule (and an unaudited app posts private only), but TikTok
Studio's upload page can. Recipe proven by hand on 2026-08-12 (brain: tiktok-posting-lane):
  * the caption box is Draft.js; what reaches the publish request is only what was really typed,
    so the request itself is the source of truth, checked field by field before it is sent
  * the schedule picker ignores scripted input, so the request's schedule_time is set directly
    (epoch seconds; body rewriting is not signature-protected)
  * wait for the content check to finish first, or a "Continue to post?" dialog blocks the submit

Work list: the worker's /clipbot/ttqueue (every Short publish.py scheduled on YouTube). Each one goes
to TikTok at the SAME slot once it is within HORIZON_H; a slot already missed gets the next free one.
Login: the TIKTOK_STATE secret (cookies + user agent) written by tiktok_login.py on Leon's laptop.
A login page, captcha or security check stops the run and tells Leon. It is never worked around.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

from . import common, publish

HORIZON_H = 26
MIN_LEAD = timedelta(minutes=20)          # TikTok refuses anything under 15 min ahead
STUDIO = "https://www.tiktok.com/tiktokstudio"
POST_RE = re.compile(r"/tiktok/web/project/post/v1/")
LIST_RE = re.compile(r"/tiktok/creator/manage/item_list/v1/")
FALLBACK_TAGS = "#gaming #fyp #twinkiesghost"


class LoginNeeded(Exception):
    pass


def caption_for(it, work=None):
    cap = (it.get("tiktok_caption") or "").strip()
    if it.get("backfill") or not cap or cap.endswith(FALLBACK_TAGS):
        # approved before TikTok copy existed: write it now, from what is said in the clip when we have it
        m, vod_title = common.manifest_clip(it, work) if work else ({}, "")
        c = common.clip_copy(it["title"], game=it.get("game") or m.get("game", ""), stream_title=vod_title, transcript=m.get("quote", ""))
        hook = c["tiktok_hook"] or it["title"].lower()
        cap = f"{hook}\n\n{' '.join(c['tiktok_hashtags'] or FALLBACK_TAGS.split())}"
    cap = re.sub(r"https?://(www\.)?kick\.com\S*", "", cap)
    return publish.mask(cap).strip()[:2000]


def check_login(page):
    url = page.url.lower()
    text = page.evaluate("document.body ? document.body.innerText.slice(0, 4000) : ''").lower()
    if "/login" in url or "captcha" in url or "verify to continue" in text or "drag the slider" in text \
            or "log in to tiktok" in text or page.locator("[id*=captcha], [class*=captcha]").count():
        raise LoginNeeded(f"TikTok wants a login or security check ({page.url[:80]})")


def open_browser(p):
    st = json.loads(common.env("TIKTOK_STATE"))
    b = p.chromium.launch(headless=os.environ.get("TT_HEADED") != "1",
                          args=["--disable-blink-features=AutomationControlled"])
    ctx = b.new_context(user_agent=st["ua"], locale="en-GB", timezone_id="Europe/London",
                        viewport={"width": 1440, "height": 900})
    ctx.add_cookies(st["cookies"])
    return b, ctx


def studio_posts(page):
    """TikTok's own list of his posts (scheduled ones carry schedule_time)."""
    seen = []

    def grab(resp):
        if LIST_RE.search(resp.url):
            try:
                seen.extend(resp.json().get("item_list") or [])
            except Exception:
                pass
    page.on("response", grab)
    page.goto(STUDIO + "/content", wait_until="domcontentloaded")
    page.wait_for_timeout(8000)
    check_login(page)
    for _ in range(3):                      # newest first; scheduled posts sit at the top
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(2500)
    page.remove_listener("response", grab)
    if not seen:
        raise RuntimeError("could not read TikTok Studio's post list")
    return seen


def wait_text(page, needles, timeout_s):
    end = time.time() + timeout_s
    while time.time() < end:
        t = page.evaluate("document.body.innerText")
        for n in needles:
            if n.lower() in t.lower():
                return n
        page.wait_for_timeout(3000)
    return None


DISMISS = ["Got it", "Decline optional cookies", "Not now", "Skip"]   # never Cancel/Discard: those drop the upload


def dismiss_popups(page):
    """TikTok stacks feature cards, tooltips and a cookie banner over the uploader; the mask they bring
    swallows clicks on the caption box (2026-10-29: 'New editing features added' + cookies)."""
    for _ in range(4):
        hit = False
        for label in DISMISS:
            btn = page.get_by_role("button", name=label, exact=True)
            for i in range(btn.count()):
                try:
                    if btn.nth(i).is_visible():
                        btn.nth(i).click(timeout=3000)
                        hit = True
                        page.wait_for_timeout(800)
                except Exception:
                    pass
        if not hit:
            return


def schedule_one(page, path, caption, epoch, dry):
    state = {}

    def handle(route):
        try:
            body = json.loads(route.request.post_data)
            info = body["single_post_req_list"][0]["single_post_feature_info"]
            state["typed_ok"] = info.get("text") == caption
            if not state["typed_ok"]:
                # typing did not land verbatim: write the caption into the request ourselves
                info["text"] = info["markup_text"] = caption
                info["text_extra"] = []
            body["feature_common_info_list"][0]["schedule_time"] = epoch
            # fail-closed guard on the PARSED final request (substring checks break on quotes)
            ok = info["text"] == caption and body["feature_common_info_list"][0]["schedule_time"] == epoch
            if dry or not ok:
                state["blocked"] = "dry run" if ok else "guard: final request did not match"
                state["preview"] = {"text": info["text"], "text_extra": len(info.get("text_extra") or []),
                                    "schedule_time": body["feature_common_info_list"][0]["schedule_time"]}
                route.abort()
                return
            resp = route.fetch(post_data=json.dumps(body, ensure_ascii=False))
            state["resp"] = resp.json()
            route.fulfill(response=resp)
        except Exception as e:
            state["blocked"] = f"hook error {e}"
            route.abort()

    page.route(POST_RE, handle)
    try:
        page.goto(STUDIO + "/upload", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        check_login(page)
        page.set_input_files("input[type=file]", path)
        if not wait_text(page, ["Uploaded"], 600):
            raise RuntimeError("video never finished uploading to TikTok")
        dismiss_popups(page)
        box = page.locator("div[contenteditable='true']").first
        box.scroll_into_view_if_needed()
        box.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        for i, line in enumerate(caption.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")
            page.keyboard.type(line, delay=25)
        page.keyboard.type(" ", delay=25)       # closes a hashtag suggestion left open by the last tag
        page.mouse.click(5, 5)
        # the content check must finish (or be over its daily limit) before submitting
        got = wait_text(page, ["No issues found", "check limit", "Issues found", "violat"], 600)
        if got in ("Issues found", "violat"):
            raise RuntimeError("TikTok's content check flagged this video; left for Leon")
        page.locator("input[value='schedule']").first.click(force=True)   # default now+15/20: valid, overwritten
        page.wait_for_timeout(1500)
        page.locator("[data-e2e='post_video_button']").first.click()
        end = time.time() + 90
        while time.time() < end and "resp" not in state and "blocked" not in state:
            if page.get_by_text("Continue to post?").count():
                why = page.evaluate(r"""() => { const d = [...document.querySelectorAll('[role=dialog], [class*=modal], [class*=Modal]')]
                    .map(e => e.innerText).find(t => t && t.includes('Continue to post')); return (d || '').replace(/\s+/g, ' ').slice(0, 220) }""")
                raise RuntimeError(f"TikTok asked 'Continue to post?' (check said: {got or 'not finished'}; {why}); nothing sent")
            if page.get_by_text("at least 15 minutes").count():
                raise RuntimeError("TikTok refused the schedule time (15-minute floor); nothing sent")
            page.wait_for_timeout(1000)
    finally:
        page.unroute(POST_RE)
    if "blocked" in state:
        return None, state
    if "resp" not in state:
        raise RuntimeError("the Schedule click sent nothing")
    r = state["resp"]
    if r.get("status_code") != 0:
        raise RuntimeError(f"TikTok refused: {r.get('status_code')} {str(r.get('status_msg'))[:120]}")
    return str(r["single_post_resp_list"][0]["item_id"]), state


def main():
    dry = os.environ.get("TT_DRY") == "1"
    if not os.environ.get("TIKTOK_STATE"):
        print("no TikTok login saved yet (run tiktok_login.py on Leon's laptop)")
        return
    items = common.worker("/clipbot/ttqueue").get("items", [])
    now = datetime.now(common.UTC)
    due = sorted((it for it in items if datetime.fromisoformat(it["at"].replace("Z", "+00:00")) <= now + timedelta(hours=HORIZON_H)),
                 key=lambda it: it["at"])[:int(os.environ.get("TT_MAX") or 999)]
    print(f"{len(items)} waiting for TikTok, {len(due)} due within {HORIZON_H}h")
    if not due:
        return
    from playwright.sync_api import sync_playwright
    work = os.path.abspath("work")
    os.makedirs(work, exist_ok=True)
    failed = 0
    done = []
    with sync_playwright() as p:
        b, ctx = open_browser(p)
        page = ctx.new_page()
        try:
            posts = studio_posts(page)
        except LoginNeeded as e:
            page.screenshot(path=os.path.join(work, "login.png"))
            common.worker("/clipbot/failed", {"id": "TikTok", "error": f"{e}. Nothing was posted. Log in again on your laptop: "
                                              "py -m clipbot.tiktok_login in Documents\\ClipBot"})
            raise SystemExit(1)
        taken = {int(x.get("schedule_time") or 0): (x.get("desc") or "") for x in posts if int(x.get("schedule_time") or 0) > now.timestamp()}
        print(f"TikTok Studio: {len(posts)} posts read, {len(taken)} scheduled")
        for it in due:
            try:
                path = publish.video_file(it, work)
                caption = caption_for(it, work)
                at = datetime.fromisoformat(it["at"].replace("Z", "+00:00"))
                dup = next((e for e, d in taken.items() if d[:40] == caption[:40]), None)
                # exact: a hand-booked post (the GTA 6 trailer, 1 Nov 09:00) keeps its time even beside another
                crowded = not it.get("exact") and any(abs(e - at.timestamp()) < 90 * 60 for e in taken)
                if dup is not None:
                    at = datetime.fromtimestamp(dup, common.UTC)
                elif at < datetime.now(common.UTC) + MIN_LEAD or crowded:
                    # missed or clashing slot: catch up at up to 4 a day (10:00/13:00/18:00/21:00 UK, 90+ min apart)
                    # (Leon 2026-10-29: "up how many we post per day till it catches up")
                    at = common.next_slot([datetime.fromtimestamp(e, common.UTC).strftime("%Y-%m-%dT%H:%M:%SZ") for e in taken], [],
                                          min_lead_h=1, marathon=True)
                epoch = int(at.timestamp())
                if dup is not None:
                    print(f"{it['id']}: already scheduled on TikTok {common.uk(at)}")
                    if not dry:
                        common.worker("/clipbot/ttdone", {"id": it["id"], "at": it["at"], "dup": True})
                    continue
                item_id, st = schedule_one(page, path, caption, epoch, dry)
                if item_id is None:
                    print(f"{it['id']}: NOT sent ({st['blocked']}) {json.dumps(st.get('preview'), ensure_ascii=False)[:400]} typed_ok={st.get('typed_ok')}")
                    if not dry:
                        raise RuntimeError(st["blocked"])
                    continue
                taken[epoch] = caption
                done.append(epoch)
                print(f"{it['id']}: TikTok {item_id} scheduled {common.uk(at)} (caption typed verbatim: {st.get('typed_ok')})")
                common.worker("/clipbot/ttdone", {"id": it["id"], "item_id": item_id, "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "when": common.uk(at)})
            except LoginNeeded as e:
                page.screenshot(path=os.path.join(work, "login.png"))
                common.worker("/clipbot/failed", {"id": "TikTok", "error": f"{e}. Stopped; log in again: py -m clipbot.tiktok_login"})
                raise SystemExit(1)
            except Exception as e:
                failed += 1
                print(f"{it['id']}: FAILED {str(e)[:300]}")
                try:
                    page.screenshot(path=os.path.join(work, f"fail-{re.sub(r'[^A-Za-z0-9]', '_', it['id'])}.png"))
                    common.worker("/clipbot/failed", {"id": f"TikTok {it['id']}", "error": str(e)[:300]})
                except Exception:
                    pass
        if done:
            # read back: TikTok's own list must show what we scheduled
            try:
                mine = {int(x.get("schedule_time") or 0) for x in studio_posts(page)}
                print(f"read-back: {len(mine & set(done))} of {len(done)} new TikTok slots visible in Studio")
            except Exception as e:
                print(f"read-back skipped: {str(e)[:120]}")
        b.close()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())

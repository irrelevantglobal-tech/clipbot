"""Leon logs in to TikTok himself; the session goes straight into the cloud bot's secrets.

    py -m clipbot.tiktok_login          (on Leon's laptop, from Documents\\ClipBot)

Opens a browser window on TikTok's login page. Leon logs in (and does any captcha himself). When
TikTok Studio opens logged in, the cookies + user agent are written to the GitHub secret
TIKTOK_STATE (via `gh secret set`, stdin, never printed) and to .secrets/tiktok_state.json
(gitignored) for local test runs. Nothing about the login is shown on screen or in a log.
Run it again whenever ClipBot says the TikTok login expired.
"""
import json
import os
import subprocess
import sys
import time

REPO = "irrelevantglobal-tech/clipbot"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale="en-GB", timezone_id="Europe/London", viewport={"width": 1280, "height": 860})
        page = ctx.new_page()
        page.goto("https://www.tiktok.com/login")
        print("Log in to TikTok in the window that opened (TwinkiesGhost). Waiting up to 15 minutes...")
        end = time.time() + 900
        while time.time() < end:
            names = {c["name"] for c in ctx.cookies("https://www.tiktok.com")}
            if "sessionid" in names and "/login" not in page.url:
                break
            page.wait_for_timeout(2000)
        else:
            raise SystemExit("No login seen in 15 minutes. Nothing saved.")
        page.goto("https://www.tiktok.com/tiktokstudio/upload")
        page.wait_for_timeout(6000)
        if "/login" in page.url:
            raise SystemExit("TikTok Studio still asks for a login. Nothing saved.")
        ua = page.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
        cookies = [c for c in ctx.cookies() if "tiktok" in c["domain"]]
        b.close()
    state = json.dumps({"ua": ua, "cookies": cookies, "saved": time.strftime("%Y-%m-%d %H:%M")})
    if len(state) > 45000:
        raise SystemExit("Session too big for a GitHub secret. Nothing saved.")
    os.makedirs(os.path.join(HERE, ".secrets"), exist_ok=True)
    with open(os.path.join(HERE, ".secrets", "tiktok_state.json"), "w", encoding="utf-8") as f:
        f.write(state)
    r = subprocess.run(["gh", "secret", "set", "TIKTOK_STATE", "-R", REPO], input=state.encode(), capture_output=True)
    if r.returncode:
        raise SystemExit("Saved locally, but could not save it to GitHub (gh secret set failed).")
    print(f"Done: TikTok login saved for ClipBot ({len(cookies)} cookies). You can close this.")


if __name__ == "__main__":
    sys.exit(main())

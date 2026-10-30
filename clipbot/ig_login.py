"""Leon saves the Instagram login for the Reels lane. The token goes straight to the worker's KV.

    py -m clipbot.ig_login                   paste the token at a hidden prompt
    py -m clipbot.ig_login --clipboard       take it from the clipboard (then the clipboard is cleared)

The token comes from the Meta developer dashboard: app "TwinkiesGhost ClipBot" -> Use cases ->
Instagram API -> API setup with Unified login -> 2. Generate access tokens -> twinkiesghost.
Since 10-2026 new apps only get Unified login, whose tokens are system-user tokens that never expire
and talk to graph.facebook.com. Older Instagram-login tokens (graph.instagram.com) still work too:
the script finds which host accepts the token and stores that with it. Never printed or written to disk.
"""
import getpass
import os
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IG_USER_ID = "17841465546315945"       # @twinkiesghost, as listed on the dashboard (checked below)


def probe(common, token):
    """(host, user_id, username) for whichever Graph host accepts this token."""
    errors = []
    try:
        me = common.http("https://graph.instagram.com/v26.0/me?" + urllib.parse.urlencode(
            {"fields": "user_id,username", "access_token": token}), retries=1)
        return "https://graph.instagram.com/v26.0", str(me["user_id"]), me.get("username", "")
    except RuntimeError as e:
        errors.append(f"instagram: {str(e)[:120]}")
    try:
        me = common.http(f"https://graph.facebook.com/v26.0/{IG_USER_ID}?" + urllib.parse.urlencode(
            {"fields": "id,username", "access_token": token}), retries=1)
        return "https://graph.facebook.com/v26.0", str(me["id"]), me.get("username", "")
    except RuntimeError as e:
        errors.append(f"facebook: {str(e)[:120]}")
    raise SystemExit("Neither Graph host accepted that token. Nothing saved.\n" + "\n".join(errors))


def main():
    if not os.environ.get("CLIPBOT_TOKEN"):
        with open(os.path.join(HERE, ".secrets", "clipbot_token"), encoding="utf-8") as f:
            os.environ["CLIPBOT_TOKEN"] = f.read().strip()
    from . import common
    if "--clipboard" in sys.argv:
        token = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                               capture_output=True, text=True).stdout.strip()
        subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ' '"], capture_output=True)
    else:
        token = getpass.getpass("Paste the Instagram access token (hidden), then Enter: ").strip()
    if not token or " " in token or len(token) < 40:
        raise SystemExit("That does not look like a token. Nothing saved.")
    host, user_id, username = probe(common, token)
    print(f"Token works for @{username} via {host.split('/')[2]}.")
    if username.lower() != "twinkiesghost":
        raise SystemExit("That token is not for @twinkiesghost. Nothing saved.")
    common.worker("/clipbot/igtoken", {"token": token, "user_id": user_id, "username": username, "host": host})
    print("Saved with ClipBot. Reels will post from the next slot onwards.")


if __name__ == "__main__":
    sys.exit(main())

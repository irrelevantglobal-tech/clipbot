"""One-time: let ClipBot comment on the channel's Shorts (owner runs this on his laptop and clicks Allow).

    py -m clipbot.yt_comment_login

Opens Google's consent page for the existing ClipBot OAuth client with the youtube.force-ssl scope (needed to
post comments), checks the token belongs to the channel, then stores it in .secrets/yt_comment_token and the
repo secret YT_COMMENT_REFRESH_TOKEN. The token is never printed. Pick the main channel in the picker.
"""
import http.server
import json
import os
import secrets
import subprocess
import sys
import urllib.parse
import webbrowser

from . import common

CLIENT = os.path.expanduser(r"~\Documents\AC4-Bestof\.secrets\yt_token.json")   # same client as the uploads
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
OUT = os.path.join(".secrets", "yt_comment_token")


def main():
    c = json.load(open(CLIENT))
    state = secrets.token_urlsafe(16)
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:       # favicon and the like
                self.send_response(404)
                self.end_headers()
                return
            if q.get("state", [""])[0] == state and "code" in q:
                got["code"] = q["code"][0]
                msg = "Done. You can close this tab and go back to Claude."
            else:
                got["error"] = q.get("error", ["state mismatch"])[0]
                msg = "That did not work: " + got["error"]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}/"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent select_account", "state": state})
    print("Opening Google in your browser. Pick the main channel, then Allow.")
    webbrowser.open(url)
    srv.timeout = 600
    for _ in range(20):                     # each wait is up to 10 min; a page reload or favicon counts as one
        if got:
            break
        srv.handle_request()
    if "code" not in got:
        raise SystemExit("Google said: " + got["error"])
    r = common.http("https://oauth2.googleapis.com/token", urllib.parse.urlencode({
        "code": got["code"], "client_id": c["client_id"], "client_secret": c["client_secret"],
        "redirect_uri": redirect, "grant_type": "authorization_code"}).encode())
    if not r.get("refresh_token"):
        raise SystemExit("Google gave no refresh token; run it again.")
    me = common.http("https://www.googleapis.com/youtube/v3/channels?part=id,snippet&mine=true",
                     headers={"Authorization": "Bearer " + r["access_token"]}).get("items", [])
    if not me or me[0]["id"] != common.CHANNEL_ID:
        raise SystemExit(f"That login is the wrong channel ({me[0]['snippet']['title'] if me else 'none'}). "
                         "Run it again and pick the main channel. Nothing was saved.")
    os.makedirs(".secrets", exist_ok=True)
    with open(OUT, "w") as f:
        f.write(r["refresh_token"])
    subprocess.run(["gh", "secret", "set", "YT_COMMENT_REFRESH_TOKEN", *(["-R", os.environ["CLIPBOT_REPO"]] if os.environ.get("CLIPBOT_REPO") else [])],
                   input=r["refresh_token"].encode(), check=True, stdout=subprocess.DEVNULL)
    print(f"Saved for {me[0]['snippet']['title']}: ClipBot can now comment on new Shorts.")


if __name__ == "__main__":
    sys.exit(main())

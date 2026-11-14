"""Owner's laptop: save the Buffer API key (copied to the clipboard) as the BUFFER_API_KEY Actions secret.

    py -m clipbot.x_login        (or double-click "Set Buffer key from clipboard.cmd" on the Desktop)

The key is read from the clipboard, checked with Buffer (it must see exactly one connected X account), then
handed to `gh secret set` on stdin. It is never printed or written to disk.
"""
import subprocess
import sys

from . import x

REPO = "irrelevantglobal-tech/clipbot"


def clipboard():
    r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], capture_output=True, text=True)
    return (r.stdout or "").strip()


def main():
    key = clipboard()
    if not key or len(key) < 20 or any(c.isspace() for c in key):
        print("RED: the clipboard does not hold an API key. In Buffer: Settings > API > create key > Copy, then run this again.")
        return 1
    try:
        _, chan = x.x_channel(key)
    except x.LoginNeeded as e:
        print(f"RED: {e}")
        return 1
    except Exception as e:
        print(f"RED: Buffer did not accept that key ({str(e)[:160]})")
        return 1
    r = subprocess.run(["gh", "secret", "set", "BUFFER_API_KEY", "-R", REPO], input=key, text=True, capture_output=True)
    if r.returncode:
        print(f"RED: key works, but GitHub would not save it ({(r.stderr or '').strip()[:200]}). Is `gh` logged in?")
        return 1
    print(f"GREEN: Buffer key saved. ClipBot will post to X as '{chan.get('name') or chan['id']}'"
          + (" (WARNING: that queue is PAUSED in Buffer)" if chan.get("isQueuePaused") else "") + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())

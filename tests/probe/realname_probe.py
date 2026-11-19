"""Runner probe for clipbot/realname.py with a made-up name (see realname-probe.yml)."""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from clipbot import realname as rn  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def make(path, secs, show=None):
    """1080p clip: a moving test pattern, plus (show=(a, b)) a PS5-style name card between a and b seconds."""
    vf = "format=yuv420p"
    if show:
        a, b = show
        vf = (f"drawbox=x=760:y=380:w=400:h=320:color=black@0.85:t=fill:enable='between(t,{a},{b})',"
              f"drawtext=fontfile={FONT}:text='Jane Doelittle':fontsize=38:fontcolor=white:x=(w-tw)/2:y=600:enable='between(t,{a},{b})',"
              f"drawtext=fontfile={FONT}:text='Who is using the controller?':fontsize=30:fontcolor=white:x=(w-tw)/2:y=420:enable='between(t,{a},{b})',"
              + vf)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={secs}",
                    "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", path], check=True)


make("with.mp4", 20, (8, 11))
make("without.mp4", 20)
make("long.mp4", 600)
ok = True
for name, want_ok in (("with.mp4", False), ("without.mp4", True)):
    r = rn.check(name)
    print(name, rn.line(r))
    ok &= r["ok"] == want_ok and not (want_ok is False and r["hits"] == 0)
t = time.time()
r = rn.check("long.mp4")
dt = time.time() - t
print("long.mp4 (10 min)", rn.line(r), f"{dt:.1f}s = {dt / max(r['frames'], 1):.3f}s/frame on {os.cpu_count()} CPUs;",
      f"a 55 min episode ~ {dt * 5.5 / 60:.1f} min")
ok &= r["ok"]
print("PROBE", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

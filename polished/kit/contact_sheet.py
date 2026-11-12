"""Build a labelled contact sheet so a human can JUDGE spans by eye.

    py contact_sheet.py <source> <out.jpg> --spans a,b a,b ...
    py contact_sheet.py <source> <out.jpg> --spans-json cuts.json --key cuts --top 12
    py contact_sheet.py <source> <out.jpg> --times 120 900 1830 ...

WHY THIS IS THE MOST IMPORTANT TOOL IN THE KIT. Every automated gate in this pipeline
passes files that are visibly wrong: an episode opening on a loading screen, a seam
landing on a menu, a two-minute BE RIGHT BACK card sitting inside kept footage. Every one
of those was caught by looking at a grid of frames, and none by a number. Duration, CFR,
decode integrity and loudness cannot see the picture.

Sample points are snapped to the frame that actually exists and each tile is labelled with
its real timestamp, so what you judge is provably what is in the file.
"""
import argparse
import json
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ffbin import FF, FP  # noqa: E402

TW, TH, LABEL, COLS = 480, 270, 26, 3


def duration(src):
    out = subprocess.run([FP, "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", src],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def font():
    for p in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.isfile(p):
            return ImageFont.truetype(p, 15)
    return ImageFont.load_default()


def hms(t):
    return "%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60)


ap = argparse.ArgumentParser()
ap.add_argument("source")
ap.add_argument("out")
ap.add_argument("--spans", nargs="*", default=[], help='pairs like 120,180')
ap.add_argument("--spans-json", default=None)
ap.add_argument("--key", default="cuts", help="key in the json holding [start,end,...] rows")
ap.add_argument("--match", default=None, help="only rows whose text contains this")
ap.add_argument("--top", type=int, default=12, help="keep the N longest spans")
ap.add_argument("--times", nargs="*", type=float, default=[], help="explicit timestamps")
ap.add_argument("--edges", action="store_true",
                help="sample the START and END of each span, not the middle "
                     "(what you want when checking whether a cut lands on a menu)")
ap.add_argument("--tmpdir", default=None)
a = ap.parse_args()

dur = duration(a.source)
points = []

for s in a.spans:
    x, y = [float(v) for v in s.split(",")]
    points.append((x, y))
if a.spans_json:
    rows = json.load(open(a.spans_json, encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows[a.key]
    got = []
    for r in rows:
        if isinstance(r, dict):
            x, y, txt = float(r.get("start")), float(r.get("end")), json.dumps(r)
        else:
            x, y = float(r[0]), float(r[1])
            txt = " ".join(str(v) for v in r[2:])
        if a.match and a.match not in txt:
            continue
        got.append((x, y))
    got.sort(key=lambda p: -(p[1] - p[0]))
    points += got[:a.top]
for t in a.times:
    points.append((t, t))

if not points:
    raise SystemExit("nothing to sheet - pass --spans, --spans-json or --times")

# Build the sample list. CLAMP EVERY POINT INTO THE FILE: sampling past the end makes
# ffmpeg re-serve the last frame, which has previously invented "static runs" that do
# not exist and got reported as a defect in every episode.
samples = []
for x, y in points:
    if a.edges and y > x:
        samples.append((max(0.0, min(x, dur - 0.05)), "IN  %s" % hms(x)))
        samples.append((max(0.0, min(y, dur - 0.05)), "OUT %s  (%.0fs)" % (hms(y), y - x)))
    else:
        mid = (x + y) / 2.0
        lab = hms(mid) if y <= x else "%s  gap %.0fs" % (hms(mid), y - x)
        samples.append((max(0.0, min(mid, dur - 0.05)), lab))

tmp = a.tmpdir or os.path.join(os.path.dirname(os.path.abspath(a.out)),
                               "_sheet_%s" % os.path.basename(a.out).split(".")[0])
os.makedirs(tmp, exist_ok=True)
if os.listdir(tmp):
    raise SystemExit("scratch dir %s is not empty - it is not yours. Pass --tmpdir." % tmp)

rows = (len(samples) + COLS - 1) // COLS
sheet = Image.new("RGB", (TW * COLS, rows * (TH + LABEL)), (12, 12, 18))
dr = ImageDraw.Draw(sheet)
f = font()

for n, (t, lab) in enumerate(samples):
    p = os.path.join(tmp, "%03d.jpg" % n)
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % t,
                    "-i", a.source, "-frames:v", "1", "-vf", "scale=%d:%d" % (TW, TH),
                    "-y", p], check=True)
    if not os.path.exists(p):
        raise SystemExit("ffmpeg produced no frame at %.2f s" % t)
    col, row = n % COLS, n // COLS
    x, y = col * TW, row * (TH + LABEL)
    sheet.paste(Image.open(p), (x, y + LABEL))
    dr.text((x + 6, y + 5), lab, font=f, fill=(140, 255, 220))

sheet.save(a.out, quality=88)
print("%d tiles -> %s" % (len(samples), a.out))
print("LOOK AT IT. This is the gate the numbers cannot replace.")

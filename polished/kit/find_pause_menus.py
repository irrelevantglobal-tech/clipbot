"""Find EVERY in-game pause menu in a VOD by pixels, not by OCR.

    py -V:3.12 find_pause_menus.py <vod_mp4> <out_json> [--ref <png>] [--thresh 0.6]

WHY THIS EXISTS
The OCR-driven dead-air pass under-reports pause menus, and on VOD 6 it is essentially
blind to them: sampling ocr.json at the four break boundaries returned "(BLANK - no text at
all)" at every one, and across the whole VOD it caught the word RESUME exactly once, as
"ESUME". The reason is structural, not a tuning problem. The OCR crops cover the objective
strip and the banner strip; Watch Dogs draws its pause buttons in a column near the middle
left, which is in NEITHER crop. A detector that cannot see the thing cannot report it, and
it fails SILENTLY - it just says the VOD is clean.

That silence already cost real quality: VOD 5 was planned with a single interior hole (a BRB
card) and its comment claimed it was "by far the cleanest of the three, 3.1% dead". Eyeballing
the boundaries found a pause menu at 466 s sitting 4 seconds inside Part 34.

HOW THIS DETECTS
The pause overlay is a fixed column of buttons (RESUME / SAVE GAME / OPTIONS / TUTORIALS /
UBISOFT CONNECT / QUIT TO MAIN MENU) at a constant position, drawn over a blurred still of
the scene. Correlating that rectangle against one reference separates it cleanly:

    confirmed menus     0.947 - 1.000
    confirmed gameplay -0.075 - 0.087

That is a two-order-of-magnitude gap, so the 0.6 default threshold is nowhere near either
cluster. The rectangle x=115 y=125 w=470 h=430 was measured off a confirmed pause frame.

SPEED
Decoding a 3-hour 1080p60 VOD in full would fight the renders for CPU, so this decodes only
keyframes (-skip_frame nokey). Twitch VODs carry a keyframe about every 2 s, which is ample:
the shortest break worth cutting is several seconds long, and boundaries get padded inward
anyway. Times come from ffprobe on the same -skip_frame pass so the Nth png is the Nth time.

OUTPUT
{"spans": [[start, end], ...]} in source seconds, each span padded OUTWARD so the cut lands
in dead air rather than clipping the first frame of returning gameplay.
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image

RX, RY, RW, RH = 115, 125, 470, 430
PAD = 1.0          # widen each span outward, into the dead air


def keyframe_times(src):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-skip_frame", "nokey",
         "-show_entries", "frame=pts_time", "-of", "csv=p=0", src],
        capture_output=True, text=True, check=True).stdout
    ts = []
    for line in out.splitlines():
        line = line.strip().rstrip(",")
        if line:
            try:
                ts.append(float(line))
            except ValueError:
                pass
    return ts


def corr(a, b):
    x, y = a.ravel() - a.mean(), b.ravel() - b.mean()
    d = np.sqrt((x * x).sum() * (y * y).sum())
    return 0.0 if d == 0 else float((x * y).sum() / d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--ref", help="png of the button-stack crop from a confirmed pause menu")
    ap.add_argument("--thresh", type=float, default=0.6)
    a = ap.parse_args()

    print("probing keyframes ...", flush=True)
    times = keyframe_times(a.src)
    print("%d keyframes, median gap %.2f s"
          % (len(times), float(np.median(np.diff(times))) if len(times) > 1 else 0.0), flush=True)

    # Stream the crops as raw greyscale through a pipe instead of writing ~5900 pngs.
    # Downscaling by 4 keeps the button column perfectly legible to a correlator (it is
    # large-scale structure, not fine text) and cuts the piped data from 1.2 GB to 75 MB.
    SW, SH = RW // 4, RH // 4
    print("decoding keyframes only (crop %dx%d -> %dx%d grey) ..." % (RW, RH, SW, SH),
          flush=True)
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-skip_frame", "nokey", "-i", a.src,
         "-fps_mode", "passthrough",
         "-vf", "crop=%d:%d:%d:%d,scale=%d:%d,format=gray" % (RW, RH, RX, RY, SW, SH),
         "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    ref = None
    if a.ref:
        r = Image.open(a.ref).convert("L").resize((SW, SH), Image.BILINEAR)
        ref = np.asarray(r).astype(float)

    fsz = SW * SH
    hits, i = [], 0
    while True:
        buf = p.stdout.read(fsz)
        if len(buf) < fsz:
            break
        if ref is not None and i < len(times):
            im = np.frombuffer(buf, dtype=np.uint8).reshape(SH, SW).astype(float)
            if corr(ref, im) >= a.thresh:
                hits.append(times[i])
        i += 1
    err = p.stderr.read().decode("utf-8", "replace")
    p.wait()
    if p.returncode not in (0, None) and i == 0:
        raise SystemExit("ffmpeg failed (%s):\n%s" % (p.returncode, err[-1500:]))
    print("scanned %d keyframes (had %d timestamps), %d menu hits"
          % (i, len(times), len(hits)), flush=True)

    gap = float(np.median(np.diff(times))) * 2.5 if len(times) > 1 else 5.0
    spans = []
    for t in hits:
        if spans and t - spans[-1][1] <= gap:
            spans[-1][1] = t
        else:
            spans.append([t, t])
    spans = [[max(0.0, s - PAD), e + PAD] for s, e in spans]

    tot = sum(e - s for s, e in spans)
    json.dump({"rect": [RX, RY, RW, RH], "thresh": a.thresh, "spans": spans},
              io.open(a.out, "w", encoding="utf-8"), indent=1)
    print("\n%d pause spans, %.1f s (%.1f min) total" % (len(spans), tot, tot / 60))
    for s, e in spans:
        print("   %8.1f - %-8.1f  %5.1f s" % (s, e, e - s))
    print("-> %s" % a.out)


if __name__ == "__main__":
    main()

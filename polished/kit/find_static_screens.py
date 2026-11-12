"""Find every sustained STATIC screen in a VOD, whatever kind of screen it is.

    py -V:3.12 find_static_screens.py <vod_mp4> <out_json> [--min 20] [--thresh 1.2]

WHY THIS EXISTS
Dead air in these VODs comes in at least eight flavours: pause menu, missions menu, world
map, tech/shop screen, wardrobe, mission briefing, loading screen, BE RIGHT BACK card, and
a full-screen sponsor break. Every detector written so far recognised ONE of them, so every
detector under-reported, and each miss looked like a clean pass:

  * the OCR pass reads the objective and banner crops, so it cannot see a pause menu at all
    (VOD 6: "BLANK" at all four break edges, and the word RESUME found once in 3 hours)
  * the pause-menu correlator sees pause menus and nothing else, so it walked straight past
    a 4-minute missions-menu-plus-BRB break in VOD 6
  * the sponsor-card matcher sees sponsor cards, and it also fired on a dark gameplay frame

Chasing screen classes one at a time is the wrong shape. What every one of them has in
common is that NOTHING MOVES. Gameplay always moves: wind, NPCs, traffic, camera sway, the
minimap, the HUD clock. So this measures inter-frame change and reports sustained flat runs,
which catches the classes above AND any new one the stream overlay grows later.

WHAT IT DELIBERATELY DOES NOT DO
It does not decide what to cut. A static run can be a genuine story beat (a held cutscene
frame, a photo-mode pause) and, more importantly, he often TALKS over dead screens, which is
real commentary the cut fusion protects on purpose. This only says "the picture stopped
here"; the fusion still decides, and the boundaries still get eyeballed.

METHOD
Keyframes only (-skip_frame nokey, about one every 2 s in these VODs), cropped to drop the
HUD corners, downscaled hard and greyscaled. A run is static while mean |frame - prev| stays
under --thresh. Threshold 1.2/255 sits well clear of both clusters measured on VOD 6:
confirmed menus came in under 0.5, ordinary gameplay above 4.
"""
import argparse
import io
import json
import subprocess

import numpy as np

SW, SH = 160, 90


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--min", type=float, default=20.0, help="shortest run to report (s)")
    ap.add_argument("--thresh", type=float, default=1.2, help="mean abs diff, 0-255")
    a = ap.parse_args()

    times = keyframe_times(a.src)
    print("%d keyframes, median gap %.2f s"
          % (len(times), float(np.median(np.diff(times)))), flush=True)

    # crop 6% off each edge: the HUD clock and minimap animate during genuine dead screens
    # in some classes, and the letterboxing differs between the game and the overlay cards.
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-skip_frame", "nokey", "-i", a.src,
         "-fps_mode", "passthrough",
         "-vf", "crop=iw*0.88:ih*0.88:iw*0.06:ih*0.06,scale=%d:%d,format=gray" % (SW, SH),
         "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    fsz = SW * SH
    diffs, prev, i = [], None, 0
    while True:
        buf = p.stdout.read(fsz)
        if len(buf) < fsz:
            break
        im = np.frombuffer(buf, dtype=np.uint8).reshape(SH, SW).astype(np.int16)
        if prev is not None and i < len(times):
            diffs.append((times[i], float(np.abs(im - prev).mean())))
        prev = im
        i += 1
    err = p.stderr.read().decode("utf-8", "replace")
    p.wait()
    if not diffs:
        raise SystemExit("ffmpeg produced no frames:\n%s" % err[-1200:])
    print("scanned %d keyframes" % i, flush=True)

    runs, cur = [], None
    for t, d in diffs:
        if d < a.thresh:
            cur = [t, t] if cur is None else [cur[0], t]
        else:
            if cur and cur[1] - cur[0] >= a.min:
                runs.append(cur)
            cur = None
    if cur and cur[1] - cur[0] >= a.min:
        runs.append(cur)

    tot = sum(b - s for s, b in runs)
    json.dump({"thresh": a.thresh, "min": a.min, "runs": runs},
              io.open(a.out, "w", encoding="utf-8"), indent=1)
    print("\n%d static runs >= %.0f s, %.1f min total" % (len(runs), a.min, tot / 60))
    for s, b in runs:
        print("   %8.1f - %-8.1f  %6.1f s" % (s, b, b - s))
    print("-> %s" % a.out)


if __name__ == "__main__":
    main()

"""Find a recurring STREAM OVERLAY (BE RIGHT BACK card, sponsor card, starting-soon
screen) in a VOD, by template-matching the picture instead of trusting OCR.

    py overlay_detect.py <workdir> --t <seconds> [--out overlay.json]
                         [--region L,T,R,B] [--thresh 0.80] [--template-dir <other workdir>]

<workdir> is a dir kf_extract_any.py has written into (kf/keyframe_times.txt + kf/th/*.jpg).
--t is a timestamp where YOU HAVE CONFIRMED BY EYE that the overlay is on screen. That frame
becomes the template. There is no default: a template is footage-specific.

⚠️ WHY THIS EXISTS AND WHY OCR DID NOT FIND IT. A hero-frame contact sheet showed a huge
"BE RIGHT BACK" overlay sitting inside a finished episode. Grepping the OCR for "be right
back" returned NOTHING across four VODs - not because it is absent, but because ocr.json
only crops the objective / banner / subtitle regions and that overlay lives TOP-LEFT. The
detector reported an empty list for a thing plainly on screen. Believe the picture, not the
absence of a hit.

⚠️ LOCATOR ONLY. This says WHERE the overlay is, not what to do about it. The gameplay
underneath is often still live, and "there is a card on screen" is not the same claim as
"this is dead air". Decide by looking.

⚠️ THE REGION MATTERS MORE THAN THE THRESHOLD. The default (left 45%, top 30% of frame) is
for a top-left card. A full-screen or centred overlay needs its own --region, given as
fractions of the frame: --region 0,0,0.45,0.30 is the default.
"""
import argparse
import io
import json
import os

import numpy as np
from PIL import Image

TW, TH = 640, 360


def load_thumb(mapdir, i, box):
    p = os.path.join(mapdir, "kf", "th", "%05d.jpg" % (i + 1))
    if not os.path.exists(p):
        return None
    im = Image.open(p).convert("L")
    if im.size != (TW, TH):
        im = im.resize((TW, TH))
    x0, y0, x1, y1 = box
    return np.asarray(im, dtype=np.float32)[y0:y1, x0:x1]


def norm(a):
    a = a - a.mean()
    s = a.std()
    # A FLAT patch has no structure to correlate. Returning it unnormalised would let
    # any two flat patches score ~1.0, so black frames would "match" the overlay.
    return a / s if s > 1e-6 else None


def times(mapdir):
    f = os.path.join(mapdir, "kf", "keyframe_times.txt")
    if not os.path.isfile(f):
        raise SystemExit("no keyframe_times.txt in %s - run kf_extract_any.py first" % mapdir)
    return [float(x) for x in io.open(f) if x.strip()]


ap = argparse.ArgumentParser()
ap.add_argument("workdir")
ap.add_argument("--t", type=float, required=True,
                help="timestamp (s) where the overlay is CONFIRMED on screen by eye")
ap.add_argument("--template-dir", default=None,
                help="workdir the template frame lives in (default: workdir)")
ap.add_argument("--out", default=None)
ap.add_argument("--region", default="0,0,0.45,0.30",
                help="L,T,R,B as fractions of the frame (default top-left card)")
ap.add_argument("--thresh", type=float, default=0.80)
ap.add_argument("--min-run", type=float, default=8.0,
                help="drop runs shorter than this many seconds")
ap.add_argument("--gap", type=float, default=25.0,
                help="merge hits separated by less than this many seconds")
a = ap.parse_args()

MAP = os.path.abspath(a.workdir)
TMAP = os.path.abspath(a.template_dir) if a.template_dir else MAP
OUT = a.out or os.path.join(MAP, "overlay.json")

fl, ft, fr, fb = [float(v) for v in a.region.split(",")]
BOX = (int(fl * TW), int(ft * TH), int(fr * TW), int(fb * TH))
if BOX[2] <= BOX[0] or BOX[3] <= BOX[1]:
    raise SystemExit("empty --region %s -> box %s" % (a.region, BOX))

tt = times(TMAP)
ti = min(range(len(tt)), key=lambda i: abs(tt[i] - a.t))
tpl = load_thumb(TMAP, ti, BOX)
if tpl is None:
    raise SystemExit("could not load the template thumbnail (index %d)" % ti)
tpl_n = norm(tpl)
if tpl_n is None:
    raise SystemExit("template patch is FLAT (no detail) - pick a different --t or --region")
print("template: %s keyframe %d, t=%.1f (asked %.1f), region %s"
      % (os.path.basename(TMAP), ti, tt[ti], a.t, BOX))

ts = times(MAP)
hits, skipped = [], 0
for i, t in enumerate(ts):
    p = load_thumb(MAP, i, BOX)
    if p is None:
        skipped += 1
        continue
    pn = norm(p)
    if pn is None:
        continue
    c = float((pn * tpl_n).mean())
    if c >= a.thresh:
        hits.append((t, round(c, 3)))
    if i and i % 2000 == 0:
        print("  %d/%d" % (i, len(ts)), flush=True)

# A missing-thumb rate that is not tiny means the keyframe extract is incomplete, and a
# HOLE READS AS "NO OVERLAY". Refuse rather than report a clean-looking empty list.
if skipped > len(ts) * 0.05:
    raise SystemExit("ABORT: %d of %d thumbs missing - re-run kf_extract_any.py"
                     % (skipped, len(ts)))

runs, cur = [], None
for t, _ in hits:
    if cur and t - cur[1] <= a.gap:
        cur[1] = t
    else:
        if cur:
            runs.append(tuple(cur))
        cur = [t, t]
if cur:
    runs.append(tuple(cur))
runs = [(x, y) for x, y in runs if y - x >= a.min_run]

json.dump({"source_workdir": MAP, "template_t": tt[ti], "region": BOX,
           "thresh": a.thresh, "hits": hits,
           "runs": [{"start": x, "end": y, "dur": round(y - x, 2)} for x, y in runs]},
          io.open(OUT, "w", encoding="utf-8"), indent=1)

print("\n%d hits over %d keyframes (%.1f%%), %d run(s) >= %.0f s"
      % (len(hits), len(ts), 100.0 * len(hits) / max(1, len(ts)), len(runs), a.min_run))
for x, y in runs:
    print("   %8.1f - %-8.1f  %5.0f s" % (x, y, y - x))
if not hits:
    # An empty list is a CLAIM and it must be justified: the template frame itself must
    # score 1.0 against itself, so if nothing hits, say what was actually tested.
    print("\nNO HITS. Before believing that: check --t really is on the overlay, check\n"
          "--region covers where it draws, and sanity-check by lowering --thresh to see\n"
          "what the best score in the VOD actually was.")
print("-> %s" % OUT)

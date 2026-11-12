"""Mean luma per keyframe, so the cut can tell BLACK from STATIC-BUT-LIT.

    py -V:3.12 wd1_luma.py <3|4|5|6>

visual.json only holds frame-to-frame CHANGE, which cannot separate the two cases that
matter here, and they need opposite treatment:

  * a BLACK screen with him talking over it   -> cut it, there is nothing to watch
  * a STATIC LIT screen with him talking over -> KEEP it, that is the shot

Part 52 proved why this distinction has to exist: 280 seconds of a motionless Chicago
skyline scored identically to 54 seconds of pure black on the change metric, but the
skyline is the stuck end credits the whole episode is about.
"""
import io
import json
import os
import sys

import numpy as np
from PIL import Image

if len(sys.argv) < 2:
    raise SystemExit(
        "usage: luma_map.py <workdir>\n"
        "  <workdir> is the dir kf_extract_any.py wrote into: it must contain\n"
        "  kf/keyframe_times.txt and kf/th/00001.jpg ..."
    )

MAP = os.path.abspath(sys.argv[1])
N = os.path.basename(MAP)
OUT = os.path.join(MAP, "luma.json")

times = [float(x) for x in io.open(os.path.join(MAP, "kf", "keyframe_times.txt")) if x.strip()]
out, missing = [], 0
for i, t in enumerate(times):
    p = os.path.join(MAP, "kf", "th", "%05d.jpg" % (i + 1))
    if not os.path.exists(p):
        missing += 1
        continue
    a = np.asarray(Image.open(p).convert("L").resize((160, 90)), dtype=np.float32)
    out.append({"t": round(t, 2), "y": round(float(a.mean()), 2),
                "p95": round(float(np.percentile(a, 95)), 2)})
    if i % 2000 == 0:
        print("  ep%s luma %d/%d" % (N, i, len(times)), flush=True)

if missing > len(times) * 0.05:
    raise SystemExit("ABORT: %d of %d keyframe thumbs missing - luma map would be full of "
                     "holes and a hole reads as 'not black'." % (missing, len(times)))

json.dump(out, io.open(OUT, "w", encoding="utf-8"))
dark = sum(1 for r in out if r["y"] < 8.0)
print("ep%s: %d keyframes, %d below luma 8 (%.1f%%), %d thumbs missing"
      % (N, len(out), dark, 100.0 * dark / max(1, len(out)), missing))
print("-> %s" % OUT)

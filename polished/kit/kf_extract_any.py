"""Keyframe HUD-crop extractor.

    py kf_extract_any.py <source.mp4> <workdir>
                         [--obj W:H:X:Y] [--ban W:H:X:Y] [--sub W:H:X:Y] [--no-crops]

Writes <workdir>/kf/keyframe_times.txt and <workdir>/kf/{obj,ban,sub,th}/%05d.jpg
`th` is always a 640x360 thumbnail of the WHOLE frame - that is what the static, luma,
overlay and contact-sheet tools read, so `--no-crops` still gives you a usable map.

⚠️ THE DEFAULT CROPS ARE FOR A 1920x1080 SOURCE AND A SPECIFIC GAME'S HUD. They are where
one game draws its objective text, its centre banner and its subtitles. On any other
resolution or any other game they are wrong, and a crop pointed at the wrong place returns
"no text" forever without erroring - which reads as a clean VOD. Find where YOUR game
draws text by looking at a frame first, then pass the crops.

Three hard-won rules are baked in:
1. `-fps_mode passthrough` goes AFTER each `-map`, never before the first one. Put it
   before and it applies only to the FIRST output; the others get default frame-rate
   handling and their numbering silently desynchronises from the keyframe grid. That
   produced a confidently-wrong subtitle mask on a WD1 build.
2. The `sub` band is WIDE (y 900-1080). A narrow cutscene-subtitle band misses in-game
   PHONE-CALL subtitles, which render lower - a 40 s story call read as "empty" and a
   dead-air detector would have deleted it.
3. Every extracted set is audited for duplicate content afterwards. "The files exist and
   the count is right" is not "the files are correct" - hash them and count distinct.
"""
import argparse
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ffbin import FF, FP  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("source")
ap.add_argument("workdir")
ap.add_argument("--obj", default="1150:110:0:0", help="objective/mission-text crop W:H:X:Y")
ap.add_argument("--ban", default="1300:150:310:95", help="centre banner crop W:H:X:Y")
ap.add_argument("--sub", default="1720:180:100:900", help="subtitle band crop W:H:X:Y")
ap.add_argument("--no-crops", action="store_true",
                help="write only the full-frame thumbnails (all the picture-based "
                     "detectors need); skip the HUD crops entirely")
_a = ap.parse_args()
src, work = _a.source, _a.workdir

CROPS = [] if _a.no_crops else [("obj", _a.obj), ("ban", _a.ban), ("sub", _a.sub)]

# Refuse a crop that does not fit the frame, with a message that says what to do.
# ffmpeg's own failure here is a Python traceback around exit code 4294967274, which
# tells you nothing, and a crop silently pointed at the wrong place is worse still.
_probe = subprocess.run([FP, "-v", "error", "-select_streams", "v:0", "-show_entries",
                         "stream=width,height", "-of", "csv=p=0:s=x", src],
                        capture_output=True, text=True, check=True).stdout.strip()
try:
    SW, SH = [int(v) for v in _probe.split("x")[:2]]
except ValueError:
    sys.exit("could not read the video size of %s (got %r)" % (src, _probe))
print("source is %dx%d" % (SW, SH))
for name, spec in CROPS:
    try:
        cw, ch, cx, cy = [int(v) for v in spec.split(":")]
    except ValueError:
        sys.exit("--%s must look like W:H:X:Y, got %r" % (name, spec))
    if cx + cw > SW or cy + ch > SH:
        sys.exit(
            "--%s crop %s does not fit a %dx%d source (needs %dx%d).\n"
            "The defaults are for 1920x1080 and one specific game's HUD. Either pass\n"
            "crops measured off YOUR footage, or use --no-crops: the full-frame\n"
            "thumbnails are what the static / luma / overlay / contact-sheet tools read."
            % (name, spec, SW, SH, cx + cw, cy + ch))

kf = os.path.join(work, "kf")
for d in [c[0] for c in CROPS] + ["th"]:
    os.makedirs(os.path.join(kf, d), exist_ok=True)

times_path = os.path.join(kf, "keyframe_times.txt")
if not os.path.exists(times_path) or os.path.getsize(times_path) == 0:
    print("=== ffprobe keyframe times (packet parse, no decode) ===", flush=True)
    out = subprocess.run(
        [FP, "-v", "error", "-select_streams", "v:0", "-show_packets",
         "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", src],
        capture_output=True, text=True, check=True).stdout
    times = [ln.split(",")[0] for ln in out.splitlines() if ",K" in ln]
    open(times_path, "w").write("\n".join(times))
else:
    times = [l.strip() for l in open(times_path) if l.strip()]
print("keyframes=%d  first=%s last=%s" % (len(times), times[0], times[-1]), flush=True)

OUTS = [c[0] for c in CROPS] + ["th"]

if len(os.listdir(os.path.join(kf, OUTS[0]))) < len(times):
    print("=== keyframe-only decode -> %d output(s) ===" % len(OUTS), flush=True)
    labels = "".join("[s%d]" % i for i in range(len(OUTS)))
    chain = ["[0:v]split=%d%s" % (len(OUTS), labels)]
    for i, (name, spec) in enumerate(CROPS):
        chain.append("[s%d]crop=%s[%s]" % (i, spec, name))
    chain.append("[s%d]scale=640:360[th]" % (len(OUTS) - 1))
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-stats",
           "-skip_frame", "nokey", "-i", src, "-filter_complex", ";".join(chain)]
    for name in OUTS:
        # `-fps_mode passthrough` goes AFTER each -map, never once before the first.
        cmd += ["-map", "[%s]" % name, "-fps_mode", "passthrough",
                "-q:v", "5" if name == "th" else "2",
                os.path.join(kf, name, "%05d.jpg")]
    subprocess.run(cmd, check=True)

print("\n=== DUPLICATE AUDIT (a desynced output shows as long identical runs) ===", flush=True)
ok = True
for d in OUTS:
    p = os.path.join(kf, d)
    files = sorted(os.listdir(p))
    hashes = [hashlib.md5(open(os.path.join(p, f), "rb").read()).hexdigest() for f in files]
    distinct = len(set(hashes))
    run = best = 1
    for i in range(1, len(hashes)):
        run = run + 1 if hashes[i] == hashes[i - 1] else 1
        best = max(best, run)
    flag = ""
    if len(files) != len(times):
        flag += "  ** COUNT MISMATCH vs %d keyframes **" % len(times)
        ok = False
    if best > 60:
        flag += "  ** longest identical run %d - check WHERE before calling it a bug **" % best
    print("%-4s files=%-6d distinct=%-6d longest_identical_run=%-5d%s"
          % (d, len(files), distinct, best, flag), flush=True)
print("\naudit_ok=%s" % ok, flush=True)

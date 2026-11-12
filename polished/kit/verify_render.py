"""The delivery gates: run these on every rendered episode before it goes anywhere.

    py verify_render.py <episode.mp4> [more.mp4 ...] [--expect 1448.95] [--fps 60]
                        [--source raw_vod.mp4 --src-span 6100,7550] [--jobs 4]

FOUR GATES, EACH CATCHING WHAT THE OTHERS CANNOT:

  1 DURATION      unreadable = no moov atom = the encode was killed mid-write.
                  With --expect, this is also the proof the render obeyed the plan.
                  ⚠️ MEASURED: this gate alone does NOT catch a truncated file. With
                  `-movflags +faststart` the moov atom sits at the FRONT, so a file cut
                  off at 60% of its length still reports its full planned duration and
                  passes gate 1 cleanly. Gates 2 and 3 caught it (303 frames of 645, and
                  "corrupt input packet"). This is exactly why there are four.
  2 FRAME COUNT   nb_frames == duration * fps proves TRUE CFR. A file can be the right
                  length and still be variable-frame-rate, which breaks editors and
                  makes every later timestamp a lie.
  3 FULL DECODE   ffmpeg -xerror decodes every frame; this is the corruption check.
  4 LOUDNESS      the episode against the raw source span it came from. Body audio
                  should be UNCHANGED - a difference means something got into the
                  audio path that you did not intend.

SPEED, because slow gates get skipped and skipped gates catch nothing. This used to take
~35 minutes per file by running four full decodes. Three fixes, each verified to give the
same numbers on a control clip first:
  * gates 2 and 3 share ONE decode - `-stats` prints the final frame= on stderr, so the
    count comes free with the integrity check (2.9x faster than a separate ffprobe
    -count_frames, identical count).
  * `-vn` BEFORE `-i` on every loudness pass: identical LUFS, ~40x faster. Audio analysis
    must never decode the picture.
  * files run concurrently. Safe only because each worker just spawns ffmpeg and writes
    no temp files, so there is no shared scratch to collide over.

⚠️ DO NOT move gate 3 onto the GPU. That pass exists to FAIL on a damaged file, and
hardware decoders conceal and report errors differently from the software decoder, so
NVDEC quietly weakens the only gate whose whole job is catching corruption.

⚠️ THESE GATES CANNOT SEE THE PICTURE. Episodes have passed all four while opening on a
loading screen, seaming into a pause menu, or carrying a two-minute BE RIGHT BACK card.
Gate 5 is your eyes on a contact sheet, and it is not optional.
"""
import argparse
import concurrent.futures as cf
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ffbin import FF, FP  # noqa: E402

FRAME_RE = re.compile(r"frame=\s*(\d+)")
LUFS_RE = re.compile(r"I:\s*(-?[\d.]+)\s*LUFS")
LRA_RE = re.compile(r"LRA:\s*(-?[\d.]+)\s*LU")


def probe_duration(path):
    r = subprocess.run([FP, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return -1.0


def decode_and_count(path):
    """Gate 2 + gate 3 in one decode. Returns (ok, frames, stderr_tail)."""
    r = subprocess.run([FF, "-v", "error", "-stats", "-xerror", "-i", path,
                        "-f", "null", "-"], capture_output=True, text=True)
    frames = 0
    for m in FRAME_RE.finditer(r.stderr):
        frames = int(m.group(1))
    tail = "\n".join(l for l in r.stderr.splitlines()
                     if l.strip() and not l.startswith("frame="))[-400:]
    return r.returncode == 0, frames, tail


def loudness(path, ss=None, to=None):
    cmd = [FF, "-hide_banner", "-nostats"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    # -vn BEFORE -i so the video is never decoded at all
    cmd += ["-vn"]
    if to is not None:
        cmd += ["-t", "%.3f" % to]
    cmd += ["-i", path, "-filter:a", "ebur128", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    i = LUFS_RE.findall(r.stderr)
    lra = LRA_RE.findall(r.stderr)
    return (float(i[-1]) if i else None, float(lra[-1]) if lra else None)


def check(path, a):
    res = {"file": os.path.basename(path), "fail": [], "warn": [], "info": []}
    if not os.path.isfile(path):
        res["fail"].append("file does not exist")
        return res

    dur = probe_duration(path)
    if dur <= 0:
        res["fail"].append("GATE 1 duration unreadable - no moov atom, the encode was "
                           "killed mid-write. The file exists; the job did not finish.")
        return res
    res["info"].append("duration %.3f s (%.2f min), %.0f MB"
                       % (dur, dur / 60, os.path.getsize(path) / 1e6))
    if a.expect is not None:
        d = dur - a.expect
        (res["fail"] if abs(d) > a.tolerance else res["info"]).append(
            "GATE 1 duration vs plan: %+.3f s (tolerance %.2f)" % (d, a.tolerance))

    ok, frames, tail = decode_and_count(path)
    if not ok:
        res["fail"].append("GATE 3 decode reported errors: %s" % (tail or "(no detail)"))
    expected_frames = dur * a.fps
    drift = frames - expected_frames
    res["info"].append("GATE 2 frames %d, expected %.0f (%.0f fps), drift %+.0f"
                       % (frames, expected_frames, a.fps, drift))
    if frames == 0:
        res["fail"].append("GATE 2 zero frames counted - the decode produced nothing")
    elif abs(drift) > max(2.0, expected_frames * 0.001):
        res["fail"].append("GATE 2 not true CFR: %+.0f frames off %.0f fps"
                           % (drift, a.fps))

    ep_i, ep_lra = loudness(path)
    if ep_i is None:
        res["fail"].append("GATE 4 could not measure loudness")
    else:
        res["info"].append("GATE 4 episode %.1f LUFS, LRA %.1f" % (ep_i, ep_lra))
        if a.source and a.src_span:
            s, e = [float(v) for v in a.src_span.split(",")]
            sr_i, sr_lra = loudness(a.source, ss=s, to=e - s)
            if sr_i is None:
                res["warn"].append("GATE 4 could not measure the source span")
            else:
                d = ep_i - sr_i
                res["info"].append("GATE 4 source span %.1f LUFS, delta %+.2f LU"
                                   % (sr_i, d))
                if abs(d) > a.lu_tolerance:
                    res["fail"].append(
                        "GATE 4 body audio differs from source by %+.2f LU.\n"
                        "     Before touching the audio: check this verifier is pointed "
                        "at the RIGHT source and the RIGHT span. A verifier hardcoded to "
                        "one VOD once failed every episode of a second one, and nothing "
                        "was wrong with them. If the RENDER numbers stay identical when "
                        "you 'fix' something, you fixed the measurement." % d)

    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--expect", type=float, default=None,
                    help="planned duration in seconds (from render_segments.py)")
    ap.add_argument("--tolerance", type=float, default=1.0)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--source", default=None, help="raw VOD, for the audio A/B")
    ap.add_argument("--src-span", default=None,
                    help="start,end in SOURCE time of a span with NO cuts in it")
    ap.add_argument("--lu-tolerance", type=float, default=1.0)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()

    if bool(a.source) != bool(a.src_span):
        sys.exit("--source and --src-span go together: the A/B needs a CONTIGUOUS UNCUT "
                 "block, mapped source->render, or it measures nothing.")

    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        results = list(ex.map(lambda p: check(p, a), a.files))

    bad = 0
    for r in results:
        head = "FAIL" if r["fail"] else ("WARN" if r["warn"] else "PASS")
        print("\n%s  %s" % (head, r["file"]))
        for line in r["info"]:
            print("   . %s" % line)
        for line in r["warn"]:
            print("   ! %s" % line)
        for line in r["fail"]:
            print("   X %s" % line)
        if r["fail"]:
            bad += 1

    print("\n%d file(s): %d passed, %d FAILED" % (len(results), len(results) - bad, bad))
    print("GATE 5 IS YOUR EYES. Run contact_sheet.py on the intro, the body start, every "
          "seam and the outro before this ships.")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

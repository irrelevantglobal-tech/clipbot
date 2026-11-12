"""Render an episode from a list of KEEP spans: seek each span as its own input,
trim it exactly, concat, and re-encode to true CFR.

    py render_segments.py <plan.json> <out.mp4> [--fps 60] [--encoder x264|nvenc]
                          [--batch 40] [--crf 20] [--dry-run]

plan.json:
    {
      "source": "D:/vods/stream.mp4",
      "keep":   [[120.0, 480.5], [610.2, 1400.0]],      // seconds, source time
      "intro":  "brand/intro.mp4",                       // optional, prepended whole
      "outro":  "brand/outro.mp4"                        // optional, appended whole
    }
"keep" may also hold {"start":..,"end":..} objects. Spans are used IN THE ORDER GIVEN.

WHY IT IS BUILT THIS WAY - each decision here was paid for:

* ONE re-encode, not `-c copy`. Copy cuts land on the nearest keyframe, which causes
  skips and progressive A/V drift. Re-encoding is the only way the delivered duration
  equals the planned duration, and that equality is the single best proof the render
  did what the plan said.

* ONE INPUT PER SEGMENT, seeked. Only footage you keep is ever decoded.

* BATCHING IS NOT OPTIONAL ABOVE ~40 SEGMENTS. Each segment costs roughly 500 characters
  of command line and Windows caps a command line at 32,767, so a 289-segment episode
  fails outright. Batches are encoded to intermediate MPEG-TS and the whole files are
  joined. Joining whole files is a different, safe case from cutting source: no seeking,
  no mid-GOP cut.

* THE CONCAT DEMUXER IS NOT USED FOR CUTTING. Tested and rejected: inpoint/outpoint
  seeking is not frame accurate (36.00 s of source came out as 38.12 s / 2287 frames
  instead of 36.00 s / 2160) and ran 2.4x slower.

* TEMP -> VERIFY -> MOVE. A killed encode leaves a CORRUPT file, not a missing one (an
  interrupted render once wrote a 1 GB mp4 with no moov atom). "The file exists" is not
  "the job finished", and a truncated re-render landing on the good file's path with a
  fresh timestamp is worse than a crash.

* AUDIO IS NOT TOUCHED beyond a transparent safety limiter. No gain, no loudnorm, no
  denoise on the body: a quiet stream mic with a near-silent room floor gets its PC fan
  lifted into audibility by any broad gain stage. If your source needs levelling, do it
  deliberately and A/B it, do not make it a default.

AFTER THIS RUNS, RUN verify_render.py, AND THEN LOOK AT A CONTACT SHEET. This script
proves the file is the length it planned to be. It cannot see that the episode opens on
a loading screen.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ffbin import FF, FP  # noqa: E402

PRE_PAD, POST_PAD = 4.0, 1.0     # decode a little either side so the trim has material
LIMITER = ("alimiter=level_in=1:level_out=1:limit=0.98:attack=5:release=50:"
           "level=disabled")


def probe(path, entries, stream=False):
    cmd = [FP, "-v", "error", "-show_entries", entries, "-of", "default=nw=1:nk=1"]
    if stream:
        cmd += ["-select_streams", "v:0"]
    r = subprocess.run(cmd + [path], capture_output=True, text=True)
    return r.stdout.strip()


def duration(path):
    try:
        return float(probe(path, "format=duration"))
    except ValueError:
        return -1.0


def vfilt(i, w, h, fps, a0=None, a1=None):
    f = "[%d:v]" % i
    if a0 is not None:
        f += "trim=start=%.4f:end=%.4f,setpts=PTS-STARTPTS," % (a0, a1)
    return (f + "fps=%d,scale=%d:%d:force_original_aspect_ratio=decrease,"
                "pad=%d:%d:-1:-1,setsar=1,format=yuv420p[v%d]"
            % (fps, w, h, w, h, i))


def afilt(i, a0=None, a1=None):
    f = "[%d:a]" % i
    if a0 is not None:
        f += "atrim=start=%.4f:end=%.4f,asetpts=PTS-STARTPTS," % (a0, a1)
    return (f + "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
                "[a%d]" % i)


def encoder_args(kind, crf):
    if kind == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr",
                "-cq", str(crf), "-b:v", "12M", "-maxrate", "20M", "-bufsize", "24M",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
            "-profile:v", "high", "-pix_fmt", "yuv420p"]


def build_batch(clips, out_path, fps, w, h, enc, crf, container):
    """clips: list of (path, start|None, end|None). Whole-file clips pass None,None."""
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-stats", "-y"]
    fc, labels = [], []
    for i, (path, s, e) in enumerate(clips):
        if s is None:
            cmd += ["-i", path]
            a0 = a1 = None
        else:
            seek = max(0.0, s - PRE_PAD)
            cmd += ["-ss", "%.3f" % seek, "-t", "%.3f" % ((e - seek) + POST_PAD),
                    "-i", path]
            # after -ss the input restarts at 0, so the trim is relative to the seek
            a0, a1 = s - seek, e - seek
        fc.append(vfilt(i, w, h, fps, a0, a1))
        fc.append(afilt(i, a0, a1))
        labels.append("[v%d][a%d]" % (i, i))
    fc.append("%sconcat=n=%d:v=1:a=1[vout][aout0]" % ("".join(labels), len(clips)))
    fc.append("[aout0]%s[aout]" % LIMITER)
    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]"]
    cmd += encoder_args(enc, crf)
    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-fps_mode", "cfr", "-r", str(fps)]
    if container == "mpegts":
        cmd += ["-f", "mpegts"]
    else:
        cmd += ["-movflags", "+faststart"]
    return cmd + [out_path]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("out")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--encoder", choices=["x264", "nvenc"], default="x264")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--tolerance", type=float, default=1.0,
                    help="max seconds the delivered duration may differ from the plan")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    plan = json.load(open(a.plan, encoding="utf-8"))
    src = plan["source"]
    if not os.path.isfile(src):
        sys.exit("source not found: %s" % src)
    w, h = [int(v) for v in a.size.lower().split("x")]

    spans = []
    for row in plan["keep"]:
        if isinstance(row, dict):
            spans.append((float(row["start"]), float(row["end"])))
        else:
            spans.append((float(row[0]), float(row[1])))

    src_dur = duration(src)
    for s, e in spans:
        if e <= s:
            sys.exit("bad span %.2f-%.2f (end <= start)" % (s, e))
        # A span running past the end makes ffmpeg re-serve the last frame, which has
        # previously invented content that was then reported as a real defect.
        if e > src_dur + 0.5:
            sys.exit("span %.2f-%.2f runs past the source (%.2f s)" % (s, e, src_dur))

    clips = []
    if plan.get("intro"):
        clips.append((plan["intro"], None, None))
    clips += [(src, s, e) for s, e in spans]
    if plan.get("outro"):
        clips.append((plan["outro"], None, None))

    expected = sum(e - s for s, e in spans)
    for k in ("intro", "outro"):
        if plan.get(k):
            d = duration(plan[k])
            if d <= 0:
                sys.exit("could not probe %s: %s" % (k, plan[k]))
            expected += d

    print("%d kept span(s)%s -> %d clip(s), expected %.2f s (%.2f min) @ %dfps"
          % (len(spans), " + intro/outro" if len(clips) > len(spans) else "",
             len(clips), expected, expected / 60.0, a.fps))
    if a.dry_run:
        return

    outdir = os.path.dirname(os.path.abspath(a.out)) or "."
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(a.out))[0]
    tmp_final = os.path.join(outdir, "_%s_tmp.mp4" % stem)

    if len(clips) <= a.batch:
        cmd = build_batch(clips, tmp_final, a.fps, w, h, a.encoder, a.crf, "mp4")
        if subprocess.run(cmd).returncode != 0:
            sys.exit("RENDER FAILED - temp left at %s" % tmp_final)
    else:
        # scratch dir named for THIS run, never for the episode number: two agents
        # deriving a scratch path from the same deliverable letter collided once and
        # silently interleaved each other's frames.
        scratch = os.path.join(outdir, "_%s_batches_%d" % (stem, os.getpid()))
        os.makedirs(scratch, exist_ok=True)
        if os.listdir(scratch):
            sys.exit("scratch dir %s is not empty - it is not yours" % scratch)
        parts, n = [], 0
        for i in range(0, len(clips), a.batch):
            chunk = clips[i:i + a.batch]
            p = os.path.join(scratch, "b%03d.ts" % n)
            print("batch %d: %d clip(s)" % (n, len(chunk)), flush=True)
            if subprocess.run(build_batch(chunk, p, a.fps, w, h, a.encoder, a.crf,
                                          "mpegts")).returncode != 0:
                sys.exit("BATCH %d FAILED - scratch left at %s" % (n, scratch))
            if duration(p) <= 0:
                sys.exit("batch %d produced an unreadable file" % n)
            parts.append(p)
            n += 1
        lst = os.path.join(scratch, "join.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for p in parts:
                fh.write("file '%s'\n" % p.replace("\\", "/").replace("'", "'\\''"))
        # Joining WHOLE already-encoded files is the safe use of the concat demuxer:
        # no seeking, no mid-GOP cut, so -c copy is exact here.
        join = [FF, "-hide_banner", "-loglevel", "error", "-stats", "-y", "-f", "concat",
                "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart",
                tmp_final]
        if subprocess.run(join).returncode != 0:
            sys.exit("JOIN FAILED - batches left at %s" % scratch)
        shutil.rmtree(scratch, ignore_errors=True)

    got = duration(tmp_final)
    print("temp duration %.2f s (expected %.2f, delta %+.2f)"
          % (got, expected, got - expected))
    if got < 0:
        sys.exit("ffprobe cannot read the temp file - refusing to publish it")
    if abs(got - expected) > a.tolerance:
        sys.exit("duration off by %+.2f s (tolerance %.2f) - refusing to publish.\n"
                 "Temp kept at %s so you can inspect it."
                 % (got - expected, a.tolerance, tmp_final))
    shutil.move(tmp_final, a.out)
    print("-> %s  %.0f MB  %.2f min" % (a.out, os.path.getsize(a.out) / 1e6, got / 60))
    print("NOW RUN: verify_render.py %s --expect %.3f" % (a.out, expected))


if __name__ == "__main__":
    main()

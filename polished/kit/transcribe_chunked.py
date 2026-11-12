"""Chunked, timeout-guarded GPU transcription. Replaces the whole-file transcriber.

    py transcribe_chunked.py <source.mp4> <workdir> <tag> [chunk_seconds]

WHY: transcribing a 3 h VOD in one call failed twice.
  * run 1 degraded monotonically 6.2x -> 2.7 -> 2.1 -> 1.9 -> 1.5x realtime
  * run 2 (with condition_on_previous_text=False) wedged completely: 4h46m at 100%
    GPU without emitting a single 5-minute checkpoint
Neither could be interrupted from inside, and both produced NOTHING usable.

So the architecture changes rather than the parameters:
  * split the audio into fixed chunks (default 600 s)
  * transcribe each in its OWN subprocess, with a hard wall-clock TIMEOUT
  * a chunk that exceeds the timeout is killed and retried ONCE at a smaller beam,
    then recorded as failed and skipped - it can never consume the whole run
  * every finished chunk is written to its own json, so progress is never lost and a
    re-run resumes automatically
  * merge at the end into one transcript in SOURCE time

The worst case stops being "lost the night" and becomes "lost one 10-minute chunk",
and that chunk is reported by name instead of failing silently.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ffbin import FF, FP  # noqa: E402
HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "chunk_worker.py")

src, work, tag = sys.argv[1], sys.argv[2], sys.argv[3]
CHUNK = float(sys.argv[4]) if len(sys.argv) > 4 else 600.0
# generous: a healthy chunk runs 3-6x realtime, so 600 s of audio takes 100-200 s.
# 8x the audio length means a chunk must be ~25x slower than healthy to trip it.
TIMEOUT = CHUNK * 8

cdir = os.path.join(work, "chunks")
os.makedirs(cdir, exist_ok=True)

dur = float(subprocess.run([FP, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", src],
                           capture_output=True, text=True).stdout.strip())
n = int(dur // CHUNK) + (1 if dur % CHUNK else 0)
print("source %.0f s -> %d chunks of %.0f s (timeout %.0f s each)"
      % (dur, n, CHUNK, TIMEOUT), flush=True)

failed = []
t_all = time.time()
for i in range(n):
    off = i * CHUNK
    wav = os.path.join(cdir, "%s_%03d.wav" % (tag, i))
    js = os.path.join(cdir, "%s_%03d.json" % (tag, i))
    if os.path.exists(js):
        print("[%03d] already done, skip" % i, flush=True)
        continue

    if not os.path.exists(wav):
        subprocess.run([FF, "-v", "error", "-ss", "%.3f" % off, "-t", "%.3f" % CHUNK,
                        "-i", src, "-ac", "1", "-ar", "16000", "-y", wav], check=True)

    # Two identical attempts. A wedge is not deterministic - the first run of this
    # VOD reached 2172 s before degrading, so a chunk that hangs once may well pass
    # on a fresh process. Anything still stuck after two tries is left as a reported
    # gap rather than allowed to eat the run.
    for attempt in (0, 1):
        t0 = time.time()
        try:
            subprocess.run([sys.executable, WORKER, wav, js, "%.3f" % off],
                           timeout=TIMEOUT, check=True)
            print("[%03d] %5.0f-%5.0f s  ok in %5.0f s (%.1fx)"
                  % (i, off, min(off + CHUNK, dur), time.time() - t0,
                     CHUNK / max(time.time() - t0, 1)), flush=True)
            break
        except subprocess.TimeoutExpired:
            print("[%03d] TIMEOUT after %.0f s%s" % (i, TIMEOUT,
                  " - retrying once" if attempt == 0 else " - giving up"), flush=True)
        except subprocess.CalledProcessError as e:
            print("[%03d] worker failed rc=%s%s" % (i, e.returncode,
                  " - retrying once" if attempt == 0 else " - giving up"), flush=True)
    else:
        failed.append(i)

# merge
segs = []
have = 0
for i in range(n):
    js = os.path.join(cdir, "%s_%03d.json" % (tag, i))
    if os.path.exists(js):
        segs.extend(json.load(open(js, encoding="utf-8"))["segments"])
        have += 1
segs.sort(key=lambda s: s["s"])
out = os.path.join(work, "%s_transcript.json" % tag)
json.dump({"source": src, "duration": dur, "model": "large-v3-cuda-chunked",
           "chunks_done": have, "chunks_total": n, "failed_chunks": failed,
           "complete": have == n, "segments": segs},
          open(out, "w", encoding="utf-8"))

el = time.time() - t_all
print("\nDONE %d/%d chunks, %d segments, %.1f h audio in %.1f min (%.1fx) -> %s"
      % (have, n, len(segs), dur / 3600, el / 60, dur / max(el, 1), out), flush=True)
if failed:
    print("FAILED CHUNKS (gaps in the transcript): %s" % failed, flush=True)

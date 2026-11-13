"""Transcribe ONE audio chunk. Spawned as a subprocess so the parent can kill it.

    py chunk_worker.py <wav> <out.json> <offset_seconds>

Deliberately a separate process: faster-whisper's decode is a C-level call that a
Python-level timeout cannot interrupt. A whole-file transcribe wedged for 4h46m at
100% GPU without emitting a single segment, and nothing inside the process could stop
it. Only an external kill works, and that needs a child process to kill.

Timestamps are written in SOURCE time (chunk offset already added), so the merged
result is directly comparable to a whole-file transcript.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cuda_env  # noqa: F401  MUST precede faster_whisper
from faster_whisper import WhisperModel

wav, out, offset = sys.argv[1], sys.argv[2], float(sys.argv[3])

model = WhisperModel("large-v3", device="cuda", compute_type="float16")
t0 = time.time()
segments, _ = model.transcribe(
    wav,
    # English, always: auto-detect runs per chunk, and a chunk that opens on music or silence came back in
    # Japanese (Vast run, 13 Nov), so its real words were lost.
    language="en",
    beam_size=5,
    word_timestamps=True,
    vad_filter=False,
    condition_on_previous_text=False,
    # A short chunk cannot drift far, but cap the fallback ladder anyway: without a
    # bound, whisper retries a hard window at successively higher temperatures and a
    # single bad window can dominate the runtime.
    temperature=[0.0, 0.2, 0.4],
    no_speech_threshold=0.6,
)

segs = []
for s in segments:
    segs.append({
        "s": round(s.start + offset, 3),
        "e": round(s.end + offset, 3),
        "text": s.text,
        "words": [{"w": w.word, "s": round(w.start + offset, 3),
                   "e": round(w.end + offset, 3), "p": round(w.probability, 3)}
                  for w in (s.words or [])],
    })

tmp = out + ".part"
json.dump({"offset": offset, "elapsed": round(time.time() - t0, 1),
           "segments": segs}, open(tmp, "w", encoding="utf-8"))
os.replace(tmp, out)
print("CHUNK OK %d segs in %.0fs" % (len(segs), time.time() - t0), flush=True)

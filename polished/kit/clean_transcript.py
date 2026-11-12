"""Strip whisper hallucinations from a transcript, using the AUDIO as the arbiter.

    py clean_transcript.py <transcript.json> <source_media> [out.json]

WHY THIS IS NEEDED: whisper large-v3 invents speech over silence. On WDL VOD 2 it
produced a segment of "fuck" repeated 222 times at 7742 s, and a run of "hey" x16-18
across six consecutive segments at 6312-6318 s. Measured, the audio at 7738-7742 s sits
at -66 to -70 dBFS - the streamer's silent room floor. There is no speech there at all.

That is not a cosmetic problem. The dead-air detector finds speech FROM the word
timestamps, so a hallucinated run is PHANTOM SPEECH that stops a legitimate cut and
leaves dead air in the delivered episode. It can also mask a real pause, pushing an
episode boundary onto a spoken word.

THE ARBITER IS THE ENVELOPE, NOT THE TEXT. A suspicious segment is only deleted when
the audio underneath it is actually quiet. A streamer genuinely shouting
"run run run run" during a chase is real and must be kept - and it is, because the
audio there is loud. Never delete on the text pattern alone.
"""
import json
import math
import os
import struct
import subprocess
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ffbin import FF  # noqa: E402

tpath, src = sys.argv[1], sys.argv[2]
out = sys.argv[3] if len(sys.argv) > 3 else tpath.replace(".json", "_clean.json")

MIN_RUN = 6            # >=6 identical consecutive words is suspicious
SPEECH_FLOOR = -55.0   # dBFS RMS below this = no speech present
MAX_WPS = 6.0          # words/sec above which speech is physically impossible

# WORD RATE IS THE STRONGEST DISCRIMINATOR and it is level-independent.
# The worst hallucination on WDL VOD 2 was 222 words in exactly 20.0 s = 11.1 words/sec.
# Human conversational speech runs 2-4 words/sec; even fast shouting does not reach 6.
# An RMS test alone MISSED that segment (-48.6 dBFS) because the 20 s window averaged
# real game audio and later speech together with the silence the loop was invented over.
# Note the suspiciously round 20.0 s span too - whisper filling a decode window.

d = json.load(open(tpath, encoding="utf-8"))
segs = d["segments"]


def longest_run(seg):
    w = [x["w"].strip().lower().strip(".,!?-") for x in (seg.get("words") or [])]
    run = best = 1 if w else 0
    for i in range(1, len(w)):
        run = run + 1 if (w[i] and w[i] == w[i - 1]) else 1
        best = max(best, run)
    return best


def wps(seg):
    w = seg.get("words") or []
    span = max(seg["e"] - seg["s"], 0.001)
    return len(w) / span


# impossible word rate = certain hallucination, no audio check needed.
# TWO tiers. The >=20-word floor exists so that a 3-word segment with sloppy timestamps is
# not deleted on a rate artefact - but it let a REAL hallucination through on VOD 2:
# 16 words in 0.44 s = 36.4 words/sec, which the RMS arbiter then kept because the span sat
# over game audio. Phantom speech stops legitimate cuts, so the extreme tier catches it.
EXTREME_WPS = 15.0     # nothing human or timestamp-jittered reaches this
impossible = {i for i, s in enumerate(segs)
              if (wps(s) > MAX_WPS and len(s.get("words") or []) >= 20)
              or (wps(s) > EXTREME_WPS and len(s.get("words") or []) >= 8)}
if impossible:
    print("IMPOSSIBLE WORD RATE (>%.0f words/sec) - dropping %d segment(s) outright:"
          % (MAX_WPS, len(impossible)))
    for i in sorted(impossible):
        s = segs[i]
        print("   %8.1fs  %5.1f w/s  %3d words over %4.1fs  %r"
              % (s["s"], wps(s), len(s.get("words") or []), s["e"] - s["s"],
                 s["text"].strip()[:44]))
    print()

suspects = [(i, s) for i, s in enumerate(segs)
            if longest_run(s) >= MIN_RUN and i not in impossible]
print("suspicious segments (>=%d identical consecutive words): %d" % (MIN_RUN, len(suspects)))
if not suspects:
    # No repetition suspects to audio-check, but impossible-rate segments STILL get
    # dropped here - the earlier version returned before applying them, so ep4's one
    # 11-words/sec hallucination survived into the "clean" file unchanged.
    clean = [s for i, s in enumerate(segs) if i not in impossible]
    d["segments"] = clean
    d["hallucinations_removed"] = len(impossible)
    d["hallucination_spans"] = sorted(round(segs[i]["s"], 1) for i in impossible)
    json.dump(d, open(out, "w", encoding="utf-8"))
    print("no repetition suspects; %d impossible-rate dropped; %d -> %d segments"
          % (len(impossible), len(segs), len(clean)))
    print("->", out)
    raise SystemExit

# measure the real audio level under each suspect
tmp = os.path.join(os.path.dirname(out), "_halluc_probe.wav")
drop = set()
for i, s in suspects:
    a, b = s["s"], max(s["e"], s["s"] + 0.5)
    subprocess.run([FF, "-v", "error", "-ss", "%.3f" % a, "-t", "%.3f" % (b - a),
                    "-i", src, "-ac", "1", "-ar", "16000", "-y", tmp], check=True)
    w = wave.open(tmp, "rb")
    n = w.getnframes()
    data = struct.unpack("<%dh" % n, w.readframes(n)) if n else ()
    w.close()
    rms = (20 * math.log10(math.sqrt(sum(x * x for x in data) / len(data)) / 32768 + 1e-12)
           if data else -120.0)
    verdict = "HALLUCINATION - dropping" if rms < SPEECH_FLOOR else "real speech - keeping"
    print("  %8.1fs run=%3d  rms=%7.1f dBFS  %s  %r"
          % (s["s"], longest_run(s), rms, verdict, s["text"].strip()[:48]))
    if rms < SPEECH_FLOOR:
        drop.add(i)

drop |= impossible
clean = [s for i, s in enumerate(segs) if i not in drop]
d["segments"] = clean
d["hallucinations_removed"] = len(drop)
d["hallucination_spans"] = sorted(round(segs[i]["s"], 1) for i in drop)
json.dump(d, open(out, "w", encoding="utf-8"))
if os.path.exists(tmp):
    os.remove(tmp)

print("\nremoved %d of %d suspects; %d -> %d segments" % (len(drop), len(suspects), len(segs), len(clean)))
print("->", out)

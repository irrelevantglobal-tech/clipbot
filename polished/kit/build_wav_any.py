"""Rebuild the full-length 16 kHz mono wav a VOD's maps need, from the transcriber's chunks.

    py -V:3.12 build_wav_any.py <workdir> <tag> <source_media>

WHY THIS EXISTS: vod2_maps.py read a whole-file wav that the OLD whole-file transcriber
left in a per-session scratchpad. The chunked transcriber that replaced it never writes
one - it writes per-600 s chunk wavs and deletes nothing - so the maps stage had no
runnable input for any new VOD, and the path it pointed at died with that session.

Concatenating the chunks is right rather than re-decoding: the audio is already demuxed
and resampled, and a 3 h re-decode is minutes of pointless work.

THE TRAP THIS HANDLES: every chunk wav is EXACTLY chunk_seconds long, including the last
one, which ffmpeg pads with silence past the end of the source. 19 chunks x 600 s = 11400 s
against a 10930 s VOD leaves 470 s of invented silence on the end. Left in, the audio map
runs past the source, and because the fusion interpolates with np.interp - which HOLDS THE
LAST VALUE FLAT rather than erroring - the mismatch would never announce itself. So the
output is trimmed to the source's real duration.
"""
import os
import subprocess
import sys
import wave

W, TAG, SRC = sys.argv[1], sys.argv[2], sys.argv[3]
OUT = os.path.join(W, f"{TAG}_16k.wav")
SR = 16000

if os.path.exists(OUT):
    with wave.open(OUT, "rb") as w:
        print("exists: %s  %.1f s" % (OUT, w.getnframes() / w.getframerate()))
    raise SystemExit

dur = float(subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
     "-of", "default=nw=1:nk=1", SRC],
    capture_output=True, text=True).stdout.strip())

chunks = sorted(f for f in os.listdir(os.path.join(W, "chunks"))
                if f.startswith(TAG) and f.endswith(".wav"))
if not chunks:
    raise SystemExit(f"no chunk wavs for tag {TAG} in {os.path.join(W, 'chunks')}")

want = int(dur * SR)          # samples the source actually has
written = 0
with wave.open(OUT, "wb") as out:
    out.setnchannels(1)
    out.setsampwidth(2)
    out.setframerate(SR)
    for c in chunks:
        with wave.open(os.path.join(W, "chunks", c), "rb") as w:
            assert w.getframerate() == SR and w.getnchannels() == 1, f"{c}: unexpected format"
            n = min(w.getnframes(), want - written)
            if n <= 0:
                break
            out.writeframes(w.readframes(n))
            written += n

print("chunks concatenated : %d" % len(chunks))
print("source duration     : %.2f s" % dur)
print("wav written         : %.2f s  (%s)" % (written / SR, OUT))
drift = abs(written / SR - dur)
print("drift vs source     : %.2f s  %s" % (drift, "OK" if drift < 1.0 else "** CHECK **"))

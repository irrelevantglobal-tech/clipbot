"""OCR the keyframe HUD crops with the native Windows OCR engine. Parameterised.

    py -V:3.12 ocr_any.py <workdir> [limit]

⚠️ RUN IT ON PYTHON 3.12, NOT the default `py`. This machine has a split toolchain:
   - OCR      -> py -V:3.12   (winsdk lives here)
   - whisper  -> py           (3.14, has CUDA torch/ctranslate2)
   Plain `py ocr_any.py` dies with ModuleNotFoundError: No module named 'winsdk'.

Reads  <workdir>/kf/keyframe_times.txt and <workdir>/kf/{obj,ban,sub}/%05d.jpg
Writes <workdir>/ocr.json  -> [{i, t, obj, ban, sub}, ...]

obj = top-left objective / mission text  -> the mission timeline
ban = centre banner                      -> MISSION COMPLETE / ARRESTED / etc
sub = WIDE bottom band                   -> dialogue AND in-game phone calls

Writes incrementally every 400 frames so a killed run leaves a usable partial file
and can be resumed instead of restarted.

⚠️ Build any OCR-matching regex against the OCR's ACTUAL output, not against how the
game spells it. Windows OCR reads DISTRICT as "OISTRICT", ACT II as "ACT 11" and
ACCESS as "AccEss". Grep this dump for frames you KNOW carry a card and build the
pattern from those strings.
"""
import asyncio
import json
import os
import sys
import time

from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.globalization import Language
from winsdk.windows.graphics.imaging import BitmapDecoder
from winsdk.windows.storage import StorageFile, FileAccessMode

# NORMALISE TO BACKSLASHES. The WinRT StorageFile API rejects forward-slash paths with
# "FileNotFoundError: [WinError -2147024735] The specified path is invalid", while Python's
# os.path.exists() happily accepts them. So a workdir passed as "C:/Users/..." makes every
# os.path.exists() succeed and every OCR call fail - which, with the old bare `except`, wrote
# a complete but entirely EMPTY ocr.json and printed a normal DONE line. All four Bloodline
# VODs were "OCR'd" that way and produced 0 mission clusters.
W = os.path.abspath(sys.argv[1]).replace("/", os.sep)
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 0
# ⚠️ CONCURRENCY MUST BE 1. This defaulted to 8 and that SILENTLY DESTROYED most of the data.
# The WinRT OCR engine cannot service overlapping recognize_async calls here: measured on 60
# known-text frames,
#     conc=1 -> 60 ok, 0 errors      conc=2 -> 32 ok, 28 "Operation aborted"
#     conc=4 -> 22 ok, 38 errors     conc=8 -> 17 ok, 43 errors
# and the old bare `except: rec[kind] = ""` turned every one of those errors into an empty
# string, which is indistinguishable from a frame that genuinely has no text. Re-OCRing 200
# random VOD 6 frames at conc=1 found text on 84 that the stored conc=8 run had recorded as
# empty, and none the other way: roughly 60% of all text-bearing frames were lost, across
# VODs 3, 4, 5 and 6, with the output still looking healthy at 34-39% text.
# Speed is not a reason to raise it: conc=1 measures ~75 frames/sec, so a 3-hour VOD takes
# under two minutes. The old comment claimed 8 workers were needed to share CPU with whisper;
# they were never needed at all.
CONC = int(sys.argv[3]) if len(sys.argv) > 3 else 1
OUT = os.path.join(W, "ocr.json")

# Collected rather than swallowed. See the handler below for why.
ERRORS = []
MISSING = []

times = [float(x) for x in open(os.path.join(W, "kf", "keyframe_times.txt")) if x.strip()]

try:
    engine = (OcrEngine.try_create_from_language(Language("en-GB"))
              or OcrEngine.try_create_from_language(Language("en-US"))
              or OcrEngine.try_create_from_user_profile_languages())
except Exception:
    engine = OcrEngine.try_create_from_user_profile_languages()
if engine is None:
    sys.exit("no OCR engine available")
print("OCR engine:", engine.recognizer_language.display_name, "| concurrency", CONC, flush=True)

done = {}
if os.path.exists(OUT):
    try:
        for r in json.load(open(OUT)):
            done[r["i"]] = r
        print("resuming, %d frames already done" % len(done), flush=True)
    except Exception:
        done = {}


async def ocr(path):
    f = await StorageFile.get_file_from_path_async(path)
    stream = await f.open_async(FileAccessMode.READ)
    dec = await BitmapDecoder.create_async(stream)
    bmp = await dec.get_software_bitmap_async()
    res = await engine.recognize_async(bmp)
    stream.close()
    return " ".join(l.text for l in res.lines)


async def main():
    n = len(times) if LIMIT == 0 else min(LIMIT, len(times))
    t0 = time.time()
    sem = asyncio.Semaphore(CONC)

    async def one(i):
        if i in done:
            return done[i]
        async with sem:
            rec = {"i": i, "t": round(times[i], 2)}
            for kind in ("obj", "ban", "sub"):
                p = os.path.join(W, "kf", kind, "%05d.jpg" % (i + 1))
                if not os.path.exists(p):
                    MISSING.append(p)
                    continue
                try:
                    rec[kind] = await ocr(p)
                except Exception as e:
                    # NEVER swallow this silently. A bare `except: rec[kind] = ""` turned a
                    # total OCR failure into a clean-looking run: all four Bloodline VODs
                    # reported "DONE 8040 frames, 0 with objective text" at 220 frames/sec
                    # (real OCR runs ~20x slower) and every downstream stage then built on an
                    # empty ocr.json - the mission timeline came out with 0 clusters and would
                    # have been treated as "this VOD has no missions".
                    rec[kind] = ""
                    ERRORS.append("%s: %s" % (type(e).__name__, str(e)[:200]))
            return rec

    out = []
    for base in range(0, n, 200):
        chunk = await asyncio.gather(*[one(i) for i in range(base, min(base + 200, n))])
        out.extend(chunk)
        el = time.time() - t0
        print("%d/%d  %.0fs  (%.0f fr/s)" % (len(out), n, el, len(out) / max(el, 1)), flush=True)
        json.dump(out, open(OUT, "w"))

    json.dump(out, open(OUT, "w"))
    nonempty = sum(1 for r in out if (r.get("obj") or "").strip())
    anytext = sum(1 for r in out
                  if (r.get("obj") or r.get("ban") or r.get("sub") or "").strip())
    print("DONE %d frames, %d with objective text, %d with ANY text -> %s"
          % (len(out), nonempty, anytext, OUT), flush=True)

    if MISSING:
        print("!! %d crop file(s) were MISSING, e.g. %s" % (len(MISSING), MISSING[0]), flush=True)
    if ERRORS:
        from collections import Counter
        print("!! %d OCR CALL(S) RAISED. Most common:" % len(ERRORS), flush=True)
        for msg, c in Counter(ERRORS).most_common(3):
            print("     x%-6d %s" % (c, msg), flush=True)

    # A VOD of real gameplay always carries HUD text on a large share of frames (the Legion
    # VODs run 30-60%). Near-zero means the pass FAILED, so say so instead of writing an
    # empty ocr.json that every downstream stage will quietly trust.
    rate = anytext / max(1, len(out))
    if rate < 0.05:
        raise SystemExit(
            "ABORT: only %.1f%% of frames produced ANY text (%d/%d). That is a FAILED pass, "
            "not a quiet VOD - real gameplay footage is 30-60%%. ocr.json has been written "
            "but must NOT be trusted. Check the errors above before re-running."
            % (100 * rate, anytext, len(out)))
    print("sanity: %.1f%% of frames carried some text" % (100 * rate), flush=True)


asyncio.run(main())

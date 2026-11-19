"""Real-name guard (boss 19 Nov, marathon auto-publish): before anything of Leon's goes public without a human look, read the
picture for his real name. The PS5 user-select / login screen shows it under his avatar, and the capture card records it
(18 Nov), so one of those seconds inside an episode or a Short would publish it.

    check(path) -> {"ok": bool, "frames": n, "text_frames": n, "hits": n, "error": str|None}

* The terms come ONLY from the CLIPBOT_REALNAME secret (comma-separated, e.g. full name and surname): this repo is public,
  so the name is never in the code, the tests or the logs. Matches are COUNTED, never printed.
* Fail-closed: no terms, no tesseract, an unreadable video, or zero frames read = ok False (held for a human), never a
  silent pass.
* Frames every SAMPLE_S seconds, scaled to 1280 wide, read by tesseract in parallel (one process per CPU over a file
  list). OCR confusions (0/O, 1/l/I, 5/S, |) are folded and spaces dropped before matching, so "L E O N" or "Le0n"
  still count.
"""
import os
import re
import shutil
import subprocess
import tempfile

SAMPLE_S = 2.0
WIDTH = 1280
FOLD = str.maketrans({"0": "o", "1": "l", "|": "l", "!": "l", "5": "s", "$": "s", "3": "e", "4": "a", "7": "t", "@": "a"})


def terms(raw=None):
    raw = os.environ.get("CLIPBOT_REALNAME", "") if raw is None else raw
    return [t for t in (fold(x) for x in raw.split(",")) if len(t) >= 4]


def fold(text):
    return re.sub(r"[^a-z]", "", str(text).lower().translate(FOLD))


def names(raw=None):
    """(given, surname) folded, from every multi-word term ("First Last"): used to tell HIS surname from a stranger's."""
    raw = os.environ.get("CLIPBOT_REALNAME", "") if raw is None else raw
    out = []
    for x in raw.split(","):
        parts = [fold(p) for p in x.split() if fold(p)]
        if len(parts) >= 2:
            out.append((parts[0], parts[-1]))
    return out


# Words that can sit right before a surname without being someone's first name ("Mr Smith", "by Smith", "Hello Smith"):
# after one of these the surname still counts as HIS (fail-closed).
NOT_GIVEN = {"mr", "mrs", "ms", "miss", "dr", "sir", "the", "and", "for", "from", "with", "dear", "hello", "hey", "user", "player",
             "account", "signed", "name", "welcome", "back", "profile", "online", "offline", "playing", "who", "by", "to", "of",
             "in", "as", "is", "at", "on", "my", "your", "his", "her", "you", "team", "family"}


def _style(tok):
    s = re.sub(r"[^A-Za-z]", "", tok)
    return "upper" if s.isupper() else "lower" if s.islower() else "title" if s[:1].isupper() and s[1:].islower() else "mixed"


def frame_hit(text, words, nm=()):
    """(hit, skipped) for one frame's text. Boss 19 Nov: a SURNAME-only match is ignored when that surname sits right after a
    DIFFERENT given name (a stranger who shares it: GTA 6's opening credits list one). Every occurrence must be explained
    that way, or it is a hit. Full-name and handle matches are untouched (plain folded substring, fail-closed)."""
    full = fold(text)
    skipped = 0
    for w in words:
        if w not in full:
            continue
        givens = [g for g, s in nm if s == w]
        if givens:   # w is a bare surname of his
            others = 0
            for ln in str(text).splitlines():   # a name pair sits on ONE line, written the same way (credits style)
                raw = [t for t in ln.split() if fold(t)]
                for i, t in enumerate(raw):
                    if fold(t) != w or i == 0:
                        continue
                    p = raw[i - 1]
                    if 3 <= len(fold(p)) <= 12 and fold(p) not in givens and fold(p) not in NOT_GIVEN and _style(p) == _style(t):
                        others += 1
            if others and others == full.count(w):
                skipped += 1
                continue
        return True, skipped
    return False, skipped


def count_hits(texts, words, nm=()):
    """Frames whose text contains any term (after folding)."""
    return sum(1 for t in texts if frame_hit(t, words, nm)[0])


def _frames(path, out_dir, sample_s=SAMPLE_S):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", f"fps=1/{sample_s},scale={WIDTH}:-2",
                    "-q:v", "3", os.path.join(out_dir, "f%06d.jpg")], check=True, capture_output=True)
    return sorted(os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith(".jpg"))


def _ocr(frames):
    """Text per frame, tesseract over N file lists in parallel (one process each; startup is the slow part)."""
    n = max(1, min(os.cpu_count() or 2, 8))
    chunks = [frames[i::n] for i in range(n)]
    procs, lists = [], []
    for ch in chunks:
        if not ch:
            continue
        fd, lst = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(ch) + "\n")
        lists.append(lst)
        procs.append((ch, subprocess.Popen(["tesseract", lst, "stdout", "--psm", "11", "-l", "eng"],
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                           env={**os.environ, "OMP_THREAD_LIMIT": "1"})))
    texts = []
    for ch, p in procs:
        out, _ = p.communicate(timeout=3600)
        if p.returncode != 0:
            raise RuntimeError(f"tesseract exited {p.returncode}")
        pages = out.split("\f")                  # tesseract ends every page with a form feed
        texts += pages[:len(ch)] + [""] * max(0, len(ch) - len(pages))
    for lst in lists:
        os.remove(lst)
    return texts


def check(path, sample_s=SAMPLE_S):
    words = terms()
    res = {"ok": False, "frames": 0, "text_frames": 0, "hits": 0, "error": None}
    if not words:
        res["error"] = "no CLIPBOT_REALNAME terms set"
        return res
    if not shutil.which("tesseract"):
        res["error"] = "tesseract not installed"
        return res
    tmp = tempfile.mkdtemp(prefix="rn_")
    try:
        frames = _frames(path, tmp, sample_s)
        texts = _ocr(frames)
        nm = names()
        per = [frame_hit(t, words, nm) for t in texts]
        res.update(frames=len(frames), text_frames=sum(1 for t in texts if t.strip()), hits=sum(1 for h, _ in per if h))
        # WHERE, never what: the video seconds of each hit frame (+- sample_s), so a hold can be checked by eye
        res["hit_at"] = [round(i * sample_s, 1) for i, (h, _) in enumerate(per) if h]
        res["surname_skips"] = sum(s for _, s in per)   # a count only: frames where his surname was a stranger's
        if not frames:
            res["error"] = "no frames read"
        else:
            res["ok"] = res["hits"] == 0
    except Exception as e:   # noqa: BLE001 - fail closed: anything unexpected holds the video for a human
        res["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def line(res):
    """Log line: counts only, never the matched text or the terms."""
    skips = res.get("surname_skips") or 0
    return (f"real-name check: {res['frames']} frames, {res['text_frames']} with text, {res['hits']} name hit(s)"
            f"{f', {skips} surname-in-another-name skip(s)' if skips else ''}"
            f"{', ERROR ' + res['error'] if res['error'] else ''} -> {'clear' if res['ok'] else 'HELD'}")


def gate(path, item_id, lane):
    """Social lanes (19 Nov, socials on their own clock): True = post it. Leon's lane only; a hit or an unreadable video
    takes the clip out of THIS lane's queue (/clipbot/hold, Leon told once) and returns False."""
    from . import common
    if common.WHO != "leon":
        return True
    res = check(path)
    print(f"{item_id}: {line(res)}")
    if res["ok"]:
        return True
    common.worker("/clipbot/hold", {"id": item_id, "lane": lane, "reason": line(res), **common.who_fields()})
    return False

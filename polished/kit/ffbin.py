"""Resolve ffmpeg/ffprobe once, for every tool in this kit.

Order: $FFMPEG / $FFPROBE env vars, then whatever is on PATH.
Fail LOUDLY here rather than letting a subprocess die with a confusing
"file not found" three layers down.

    from ffbin import FF, FP
"""
import os
import shutil


def _find(env, name):
    p = os.environ.get(env)
    if p and os.path.isfile(p):
        return p
    p = shutil.which(name)
    if p:
        return p
    raise SystemExit(
        "%s not found. Install ffmpeg and put it on PATH, or set %s to the "
        "full path of the executable." % (name, env)
    )


FF = _find("FFMPEG", "ffmpeg")
FP = _find("FFPROBE", "ffprobe")

"""Make ctranslate2 / faster-whisper able to find the pip-installed CUDA runtime.

Import this BEFORE faster_whisper:

    import cuda_env            # noqa: F401
    from faster_whisper import WhisperModel

Why this exists: ctranslate2 ships cudnn64_9.dll inside its own package but NOT cuBLAS.
`pip install nvidia-cublas-cu12` supplies cublas64_12.dll, but ctranslate2 loads it lazily
from native code via plain LoadLibrary, which searches PATH and IGNORES
os.add_dll_directory(). So PATH is the only thing that works here.

This edits PATH for the CURRENT PROCESS ONLY - it never touches the user's environment.
"""
import os
import sys

_SUBDIRS = (
    "nvidia/cublas/bin",
    "nvidia/cuda_nvrtc/bin",
    "nvidia/cudnn/bin",
    "ctranslate2",
)


def enable():
    """Prepend the CUDA runtime dirs to PATH. Returns the list actually found."""
    site = os.path.join(sys.prefix, "Lib", "site-packages")
    dirs = [os.path.join(site, *s.split("/")) for s in _SUBDIRS]
    dirs = [d for d in dirs if os.path.isdir(d)]
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
        for d in dirs:
            try:
                os.add_dll_directory(d)  # harmless belt-and-braces; PATH is what does the work
            except (OSError, AttributeError):
                pass
    return dirs


FOUND = enable()

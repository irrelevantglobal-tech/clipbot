"""Lane 1: the polished recipe as a DEPLOYED Modal app, called from GitHub Actions (conductor.py).

    modal deploy polished/modal_app.py          (the conductor does this at the start of every run)

Deployed, not `modal run`: a function call keeps going and its result stays collectable even if the
caller dies, and no laptop is involved at any point (a Claude-app crash orphaned a container on
10-30 because the client was local). Every call is bounded by `timeout`.

Storage: rendered episodes go to the named Volume `gta6-xfer` only until the conductor has copied
them out (it deletes each one, and sweeps anything older than XFER_TTL_H). Volumes bill
$0.09/GiB-month, so a few GB held for an hour costs nothing; nothing big is ever left there.
Cost note (modal billing rates, 2026-11-12): egress is $0.04/GiB, so every episode copied out costs
~$0.06-0.08 on top of GPU time. ledger.py counts it.
"""
import os
import pathlib
import sys
import time

import modal

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
APP = "gta6-polished"
VOL = "gta6-xfer"


def _download_models():
    from faster_whisper import WhisperModel
    WhisperModel("large-v3", device="cpu", compute_type="int8")   # caches the weights only
    import open_clip
    open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai")


image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "fonts-dejavu-core")
    .pip_install_from_requirements(str(HERE / "requirements-gpu.txt"))
    .env({"LD_LIBRARY_PATH": "/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:"
                             "/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib",
          "PYTHONUNBUFFERED": "1", "POLISHED_KIT": "/root/repo/polished/kit"})
    .run_function(_download_models)
    .add_local_dir(str(HERE), "/root/repo/polished", ignore=["__pycache__", "*.pyc"])
    .add_local_dir(str(ROOT / "clipbot" / "style" / "fonts"), "/root/repo/clipbot/style/fonts")
    .add_local_dir(str(ROOT / "fonts"), "/root/repo/fonts")
)
app = modal.App(APP, image=image)
vol = modal.Volume.from_name(VOL, create_if_missing=True)
GPUS = ["L4", "A10", "T4"]   # L4 first (cheapest, fastest render); L4-only once sat unscheduled 7+ min


def _repo():
    sys.path.insert(0, "/root/repo")


@app.function(gpu=GPUS, cpu=4.0, memory=8192, timeout=3600, retries=0)
def analyze(vod: str, lo: float, hi: float):
    _repo()
    from polished import stages
    t0 = time.time()
    r = stages.analyze_chunk(vod, lo, hi, "/tmp/an")
    r["container_s"] = round(time.time() - t0, 1)
    return r


@app.function(gpu=GPUS, cpu=4.0, memory=8192, timeout=3600, retries=0, volumes={"/xfer": vol})
def render(vod: str, plan: dict, title: str, game: str, brand: str, login: str, key: str):
    """Renders one episode to /xfer/out/<key>.mp4 (+ its report). Re-run with the same key = no-op."""
    _repo()
    import hashlib
    import json
    import shutil
    from polished import stages
    from polished.brand import stings
    t0 = time.time()
    dst = "/xfer/out/%s.mp4" % key
    rep = "/xfer/out/%s.json" % key
    vol.reload()
    if os.path.exists(dst) and os.path.exists(rep):
        r = json.load(open(rep))
        r["reused"] = True
        return r
    os.makedirs("/xfer/out", exist_ok=True)
    os.makedirs("/xfer/brand", exist_ok=True)
    work = "/tmp/ep"
    tag = hashlib.sha1(("%s|%s|%s" % (game, brand, login)).encode()).hexdigest()[:10]
    outro = "/xfer/brand/outro_%s.mp4" % tag
    if not os.path.exists(outro):
        o = stings.outro(game, brand, login, "/tmp/brand")
        shutil.copy(o, outro + ".part")
        os.replace(outro + ".part", outro)
        vol.commit()
    t_s = time.time()
    intro = stings.intro(title, game, brand, login, "/tmp/brand")
    t_sting = time.time() - t_s
    out, r = stages.render_episode(vod, plan, intro, outro, work)
    shutil.copy(out, dst + ".part")
    os.replace(dst + ".part", dst)
    r.update({"key": key, "size": os.path.getsize(dst), "sting_s": round(t_sting, 1),
              "container_s": round(time.time() - t0, 1)})
    json.dump(r, open(rep, "w"))
    vol.commit()
    return r

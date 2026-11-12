"""The polished recipe as a command line, for GPU hosts that are not Modal (lane 2: Vast.ai over SSH).

    python -m polished.worker analyze <vod> <lo> <hi> <out.json>
    python -m polished.worker render <vod> <plan.json> <title> <game> <brand> <login> <out_dir>

Writes temp -> verify -> rename, so a dropped SSH session never leaves a half file that looks done.
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polished import stages  # noqa: E402
from polished.brand import camp as stings  # noqa: E402


def main(argv):
    if argv[0] == "analyze":
        vod, lo, hi, out = argv[1], float(argv[2]), float(argv[3]), argv[4]
        t0 = time.time()
        wd = os.path.splitext(out)[0] + "_work"   # never ".json" in a folder name: the kit rewrites ".json" in paths
        r = stages.analyze_chunk(vod, lo, hi, wd)
        r["container_s"] = round(time.time() - t0, 1)
        json.dump(r, open(out + ".part", "w"))
        os.replace(out + ".part", out)
        shutil.rmtree(wd, ignore_errors=True)
    elif argv[0] == "render":
        vod, plan_f, title, game, brand, login, out_dir = argv[1:8]
        os.makedirs(out_dir, exist_ok=True)
        plan = json.load(open(plan_f, encoding="utf-8"))
        retired = plan.pop("_retired", None)
        index = plan.pop("_index", None)
        t0 = time.time()
        bdir = os.path.join(out_dir, "_brand")
        outro = os.path.join(bdir, "outro.mp4")
        if not os.path.exists(outro):
            os.replace(stings.outro(game, brand, login, bdir, retired), outro)
        intro = stings.intro(title, game, brand, login, bdir, retired, index=index)
        out, r = stages.render_episode(vod, plan, intro, outro, os.path.join(out_dir, "_work"))
        dst = os.path.join(out_dir, "episode.mp4")
        os.replace(out, dst)
        r.update({"size": os.path.getsize(dst), "container_s": round(time.time() - t0, 1)})
        json.dump(r, open(os.path.join(out_dir, "report.json.part"), "w"))
        os.replace(os.path.join(out_dir, "report.json.part"), os.path.join(out_dir, "report.json"))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

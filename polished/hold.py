"""Lane 4: a Claude session on the owner's plan plans/reviews a HELD window when the cloud API
budget is dry (or the reviewer held an episode). Run on the owner's laptop by that session.

    py -m polished.hold pull <vod> <from> <dir>     state of that window -> <dir> (decrypted); lists what is held
    (the session reads <dir>/held.md, answers each prompt: <dir>/answers/plans/<key>.json = the JSON the plan
     prompt asks for; <dir>/answers/reviews/<n>.json = {"marker": ..., "verdict": "pass"|"fail", "issues": [...]}
     after LOOKING at that episode's review_sheet_*.jpg and reading its review_prompt.txt)
    py -m polished.hold push <vod> <from> <dir> [--to S] [--final]   re-runs the window with those answers
                                                                     (upload + Discord cards, as the worker would)

Never post a rough cut: a review answer is only "pass" when the session would pass it on the published
series' bar (PROOF-PLAN.md rubric). The answers go up ENCRYPTED with the repo's zip key (the repo is public).
Key file: Documents\\ClipBot\\.secrets\\zip_key (never printed).
"""
import base64
import glob
import gzip
import json
import os
import subprocess
import sys

REPO = "irrelevantglobal-tech/clipbot"
KEYFILE = os.path.expanduser(r"~\Documents\ClipBot\.secrets\zip_key")


def _openssl(args, data):
    env = dict(os.environ, ZK=open(KEYFILE, encoding="utf-8").read().strip())
    r = subprocess.run(["openssl", "enc", *args, "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", "env:ZK"],
                       input=data, capture_output=True, env=env)
    if r.returncode:
        raise SystemExit("openssl failed: %s" % r.stderr.decode()[-200:])
    return r.stdout


def pull(vod, lo, d):
    os.makedirs(d, exist_ok=True)
    name = "gta6-state-%s-%s" % (vod, lo)
    got = json.loads(subprocess.run(["gh", "api", "repos/%s/actions/artifacts?name=%s&per_page=1" % (REPO, name)],
                                    capture_output=True, text=True, check=True).stdout)
    arts = [x for x in got.get("artifacts", []) if not x.get("expired")]
    if not arts:
        raise SystemExit("no state artifact %s" % name)
    z = os.path.join(d, "state.zip")
    with open(z, "wb") as f:
        f.write(subprocess.run(["gh", "api", "repos/%s/actions/artifacts/%d/zip" % (REPO, arts[0]["id"])],
                               capture_output=True, check=True).stdout)
    import io
    import tarfile
    import zipfile
    zf = zipfile.ZipFile(z)
    tgz = _openssl(["-d"], zf.read("state.tgz.enc"))
    tarfile.open(fileobj=io.BytesIO(tgz)).extractall(os.path.join(d, "state"), filter="data")
    if "manifest.json.enc" in zf.namelist():
        open(os.path.join(d, "manifest.json"), "wb").write(_openssl(["-d"], zf.read("manifest.json.enc")))
    os.remove(z)
    lines = ["# Held window %s from %s" % (vod, lo), ""]
    for h in sorted(glob.glob(os.path.join(d, "state", "plan_*", "hold.json"))):
        ep = os.path.dirname(h)
        key = os.path.basename(ep)[5:].replace("_", ".")
        lines.append("- PLAN %s: %s -> answer %s (prompt: %s)" % (
            key, json.load(open(h, encoding="utf-8")).get("why"), os.path.join("answers", "plans", key + ".json"),
            os.path.join(ep, "plan_prompt.txt")))
    for h in sorted(glob.glob(os.path.join(d, "state", "review_*", "review_hold.json"))):
        rd = os.path.dirname(h)
        lines.append("- REVIEW %s: %s (sheets: %s, text: %s)" % (
            os.path.basename(rd), json.load(open(h, encoding="utf-8")).get("why"),
            ", ".join(sorted(glob.glob(os.path.join(rd, "review_sheet_*.jpg")))), os.path.join(rd, "review_prompt.txt")))
    m = os.path.join(d, "manifest.json")
    if os.path.exists(m):
        man = json.load(open(m, encoding="utf-8"))
        lines += ["", "manifest: held = %s; %d episode(s) before it" % (man.get("held"), len(man.get("episodes", [])))]
        lines.append("review answers need the marker of the held episode: clipbot:ep:%s:<int(start)>; see the plan's "
                     "bounds[0] + window start %s" % (vod, lo))
    open(os.path.join(d, "held.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))


def push(vod, lo, d, to="0", final=False):
    ans = {"plans": {}, "reviews": {}}
    for f in glob.glob(os.path.join(d, "answers", "plans", "*.json")):
        ans["plans"][os.path.basename(f)[:-5]] = open(f, encoding="utf-8").read()
    for f in glob.glob(os.path.join(d, "answers", "reviews", "*.json")):
        r = json.load(open(f, encoding="utf-8"))
        if r.get("verdict") not in ("pass", "fail") or not r.get("marker"):
            raise SystemExit("%s: needs marker + verdict pass|fail" % f)
        ans["reviews"][r["marker"]] = {"verdict": r["verdict"], "issues": r.get("issues", []), "by": "claude-session"}
    blob = base64.b64encode(_openssl(["-salt"], gzip.compress(json.dumps(ans).encode()))).decode()
    if len(blob) > 60000:
        raise SystemExit("answers are %d chars, over the dispatch input limit; push fewer at a time" % len(blob))
    cmd = ["gh", "workflow", "run", "polished.yml", "-R", REPO, "-f", "vod=%s" % vod, "-f", "from=%s" % lo,
           "-f", "to=%s" % to, "-f", "final=%s" % str(bool(final)).lower(), "-f", "upload=true", "-f", "offer=true",
           "-f", "answers=%s" % blob]
    subprocess.run(cmd, check=True)
    print("dispatched polished.yml for %s from %s with %d plan + %d review answer(s)" % (
        vod, lo, len(ans["plans"]), len(ans["reviews"])))


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 4 and a[0] == "pull":
        pull(a[1], a[2], a[3])
    elif len(a) >= 4 and a[0] == "push":
        push(a[1], a[2], a[3], to=(a[a.index("--to") + 1] if "--to" in a else "0"), final="--final" in a)
    else:
        raise SystemExit(__doc__)

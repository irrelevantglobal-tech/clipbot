"""Make the camp-sting media present and EXACT before anything renders.

    python -m polished.brand.camp_fetch            (the workflow runs this before `modal deploy`)

camp_assets/manifest.json lists every media file (camp footage, music) with its sha256 and size. A file that is
missing or does not match is fetched from CAMP_ASSETS_URL and checked again; a bad download is deleted, never kept.
CAMP_ASSETS_URL is either
  modal-volume://<volume>   a PRIVATE Modal Volume, read with the Modal credentials the job already has, or
  https://...               a base URL (file name appended), with CAMP_ASSETS_TOKEN sent as a Bearer header.
  github://<owner>/<repo>@<ref>  a PRIVATE GitHub repo: with CAMP_MEDIA_DEPLOY_KEY (a read-only deploy key, Actions)
                            over SSH, else through the machine's own `gh` login (the owner's laptop).
SEVERAL SOURCES (13 Nov, owner: "we NEEEED to fix this"): CAMP_ASSETS_SOURCES (comma list), else DEFAULT_SOURCES, then
CAMP_ASSETS_URL. Each is tried in turn for whatever is still missing, so a capped or unreachable Modal no longer
stops the backup lanes: the GitHub copy comes first and does not depend on Modal at all.
Credentials are never printed or put in a URL. With everything present and matching, nothing is fetched.
The media is kept out of git history (.gitignore); it rides to Modal inside the deployed image (add_local_dir of
polished/) and to a Vast box inside the same tarball, so no token ever reaches a rented machine.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "camp_assets")
DEFAULT_SOURCES = ["github://irrelevantglobal-tech/clipbot-media@main", "modal-volume://sting-media"]


def sources():
    raw = os.environ.get("CAMP_ASSETS_SOURCES", "")
    out = [x.strip() for x in raw.split(",") if x.strip()] or list(DEFAULT_SOURCES)
    extra = os.environ.get("CAMP_ASSETS_URL", "").strip()
    if extra and extra not in out:
        out.append(extra)
    return out


def _github_clone(spec, dst):
    """github://owner/repo@ref -> shallow clone into dst. Deploy key (Actions) or the machine's gh login."""
    repo, _, ref = spec[len("github://"):].partition("@")
    ref = ref or "main"
    key = os.environ.get("CAMP_MEDIA_DEPLOY_KEY", "")
    if key:
        kf = os.path.join(tempfile.mkdtemp(prefix="ckey_"), "k")
        with open(kf, "w") as f:
            f.write(key.strip() + "\n")
        os.chmod(kf, 0o600)
        env = dict(os.environ, GIT_SSH_COMMAND="ssh -i %s -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" % kf)
        try:
            r = subprocess.run(["git", "clone", "-q", "--depth", "1", "-b", ref, "git@github.com:%s.git" % repo, dst],
                               env=env, capture_output=True, text=True, timeout=300)
        finally:
            shutil.rmtree(os.path.dirname(kf), ignore_errors=True)
    elif shutil.which("gh"):
        r = subprocess.run(["gh", "repo", "clone", repo, dst, "--", "-q", "--depth", "1", "-b", ref],
                           capture_output=True, text=True, timeout=300)
    else:
        raise RuntimeError("no deploy key and no gh login for %s" % repo)
    if r.returncode:
        raise RuntimeError("clone of %s failed (rc %d)" % (repo, r.returncode))


def manifest(assets=ASSETS):
    return json.load(open(os.path.join(assets, "manifest.json"), encoding="utf-8"))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def ok(path, want):
    return os.path.exists(path) and os.path.getsize(path) == want["bytes"] and sha256(path) == want["sha256"]


def missing(assets=ASSETS):
    return [f for f, w in manifest(assets)["files"].items() if not ok(os.path.join(assets, f), w)]


def ensure(assets=ASSETS, base=None, token=None):
    """-> number of files fetched. Tries every source in turn (sources()); SystemExit only if, after all of
    them, a file still cannot be made exact."""
    todo = missing(assets)
    if not todo:
        return 0
    srcs = [base] if base else sources()
    n, errors = 0, []
    for src in srcs:
        todo = missing(assets)
        if not todo:
            break
        try:
            n += _ensure_one(assets, src, token, todo)
            print("camp sting media: %s -> ok" % src.split("://")[0], flush=True)
        except (SystemExit, Exception) as e:   # noqa: BLE001 - next source; never print a credential
            errors.append("%s: %s" % (src.split("://")[0], str(e)[:160]))
            print("camp sting media: %s failed (%s); trying the next source" % (src.split("://")[0], str(e)[:160]), flush=True)
    left = missing(assets)
    if left:
        raise SystemExit("camp sting media: %d file(s) still missing after every source (%s)" % (len(left), "; ".join(errors)))
    return n


def _ensure_one(assets, base, token, todo):
    if base.startswith("github://"):
        want = manifest(assets)["files"]
        tmpd = tempfile.mkdtemp(prefix="cmedia_")
        try:
            _github_clone(base, os.path.join(tmpd, "r"))
            for f in todo:
                src = os.path.join(tmpd, "r", f)
                if not ok(src, want[f]):
                    raise SystemExit("camp sting media: %s missing or wrong in %s" % (f, base.split("@")[0]))
                dst = os.path.join(assets, f)
                shutil.copy(src, dst + ".part")
                os.replace(dst + ".part", dst)
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
        return len(todo)
    token = token if token is not None else os.environ.get("CAMP_ASSETS_TOKEN", "")
    if not base:
        raise SystemExit("camp sting media missing or changed (%d files, e.g. %s) and CAMP_ASSETS_URL is not set"
                         % (len(todo), todo[0]))
    want = manifest(assets)["files"]
    vol = None
    if base.startswith("modal-volume://"):
        import modal
        vol = modal.Volume.from_name(base[len("modal-volume://"):].strip("/"))
    for f in todo:
        dst = os.path.join(assets, f)
        tmp = dst + ".part"
        try:
            if vol is not None:
                with open(tmp, "wb") as out:
                    for b in vol.read_file(f):
                        out.write(b)
            else:
                req = urllib.request.Request(base.rstrip("/") + "/" + f, headers={"User-Agent": "ClipBot/1.0"})
                if token:
                    req.add_header("Authorization", "Bearer " + token)
                with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as out:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        out.write(b)
        except Exception as e:   # noqa: BLE001 - say which file, never the token
            if os.path.exists(tmp):
                os.remove(tmp)
            raise SystemExit("camp sting media: could not fetch %s (%s)" % (f, type(e).__name__))
        if not ok(tmp, want[f]):
            os.remove(tmp)
            raise SystemExit("camp sting media: %s does not match its sha256 in manifest.json; not kept" % f)
        os.replace(tmp, dst)
    return len(todo)


if __name__ == "__main__":
    n = ensure()
    print("camp sting media: %d file(s) fetched, %d present and exact" % (n, len(manifest()["files"])))
    sys.exit(0)

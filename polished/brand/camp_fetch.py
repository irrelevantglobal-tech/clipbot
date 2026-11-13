"""Make the camp-sting media present and EXACT before anything renders.

    python -m polished.brand.camp_fetch            (the workflow runs this before `modal deploy`)

camp_assets/manifest.json lists every media file (camp footage, music) with its sha256 and size. A file that is
missing or does not match is fetched from CAMP_ASSETS_URL and checked again; a bad download is deleted, never kept.
CAMP_ASSETS_URL is either
  modal-volume://<volume>   a PRIVATE Modal Volume, read with the Modal credentials the job already has, or
  https://...               a base URL (file name appended), with CAMP_ASSETS_TOKEN sent as a Bearer header.
Credentials are never printed or put in a URL. With everything present and matching, nothing is fetched.
The media is kept out of git history (.gitignore); it rides to Modal inside the deployed image (add_local_dir of
polished/) and to a Vast box inside the same tarball, so no token ever reaches a rented machine.
"""
import hashlib
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "camp_assets")


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
    """-> number of files fetched. Raises SystemExit if a file cannot be made exact."""
    todo = missing(assets)
    if not todo:
        return 0
    base = base if base is not None else os.environ.get("CAMP_ASSETS_URL", "")
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

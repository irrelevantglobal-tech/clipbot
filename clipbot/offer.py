"""Send the rendered picks to the Forge worker, which DMs them to Leon with buttons (another
person's go to his own approval channel; the worker routes by `who`).

    python -m clipbot.offer --out out --run-id $GITHUB_RUN_ID
"""
import argparse
import json
import os
import sys
import urllib.request
import uuid

from . import common


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    man = json.load(open(os.path.join(a.out, "manifest.json"), encoding="utf-8"))
    man["run_id"] = a.run_id
    man.update(common.who_fields())                  # Leon's manifest is unchanged; anyone else's says who
    common.check_worker_knows_who()
    for c in man["clips"]:
        c.pop("quote", None)                         # transcript text stays out of KV

    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json\r\n\r\n'.encode()
             + json.dumps(man, ensure_ascii=False).encode() + b"\r\n"]
    for c in man["clips"]:
        p = os.path.join(a.out, f"preview{c['n']}.mp4")
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="preview{c["n"]}"; filename="clip{c["n"]}.mp4"\r\n'
                     f'Content-Type: video/mp4\r\n\r\n'.encode() + open(p, "rb").read() + b"\r\n")
    body = b"".join(parts) + f"--{b}--\r\n".encode()
    req = urllib.request.Request(common.WORKER + "/clipbot/offer", data=body, method="POST", headers={
        "User-Agent": "TwinkiesGhost-ClipBot/1.0",
        "Authorization": "Bearer " + common.env("CLIPBOT_TOKEN"), "Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        print(r.status, r.read()[:300].decode())


if __name__ == "__main__":
    sys.exit(main())

"""Lane 2: run the same recipe on a rented Vast.ai GPU, driven over SSH from GitHub Actions.

Owner rules (2026-10-29 / 11-12):
* $10 credit, NO card on file (deleted 10-29): at $0 instances stop. The balance is read before the
  instance is rented and between every step; under MIN_BALANCE the job stops cleanly and HOLDS.
* NO YouTube or Twitch tokens on Vast hosts: the box only fetches public VOD segments and renders;
  uploading stays in Actions. The only credential the box ever sees is a one-job SSH key.
* DESTROY every instance after its job (stopped instances keep billing storage): `finally` destroys
  it, and sweep() destroys any instance whose label starts with LABEL that is not the live job's.

API: https://console.vast.ai/api/v0 with `Authorization: Bearer <VAST_API_KEY>`. Endpoints follow
the vastai CLI (users/current, bundles, asks/<offer>, instances/<id>, instances/<id>/ssh). First
real use = T3 lane-2 run; every response the code depends on is checked and printed (no secrets).
"""
import json
import os
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

API = "https://console.vast.ai/api/v0"
LABEL = "gta6-polished"
IMAGE = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"   # Ubuntu 22.04, Python 3.11, CUDA 12.4, cuDNN 9
MIN_BALANCE = 0.30          # $ left at which a job stops and holds (storage + teardown margin)
SSH_WAIT_S = 12 * 60


class VastError(RuntimeError):
    pass


class Vast:
    def __init__(self, key=None):
        self.key = (key or os.environ.get("VAST_API_KEY", "")).strip()
        if not self.key:
            raise VastError("no VAST_API_KEY")

    def _call(self, method, path, body=None, tries=4):
        data = json.dumps(body).encode() if body is not None else None
        for k in range(tries):
            req = urllib.request.Request(API + path, data=data, method=method, headers={
                "Authorization": "Bearer " + self.key, "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "clipbot-polished"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    txt = r.read().decode() or "{}"
                    return json.loads(txt)
            except urllib.error.HTTPError as e:
                txt = e.read().decode("utf-8", "replace")[:300]
                if e.code in (429, 500, 502, 503) and k < tries - 1:
                    time.sleep(5 * (k + 1))
                    continue
                raise VastError(f"{method} {path.split('?')[0]} -> {e.code}: {txt}")
            except (urllib.error.URLError, TimeoutError) as e:
                if k < tries - 1:
                    time.sleep(5 * (k + 1))
                    continue
                raise VastError(f"{method} {path.split('?')[0]}: {e}")

    def balance(self):
        u = self._call("GET", "/users/current/")
        for k in ("credit", "balance"):
            if isinstance(u.get(k), (int, float)):
                return float(u["credit"]) if k == "credit" else -float(u["balance"])
        raise VastError("could not read the balance (keys: %s)" % sorted(u)[:20])

    def offers(self, limit=20):
        """Cheapest reliable 24 GB-class NVIDIA offers that can hold a window's downloads."""
        q = {"verified": {"eq": True}, "rentable": {"eq": True}, "rented": {"eq": False}, "external": {"eq": False},
             "gpu_name": {"in": ["RTX 3090", "RTX 4090", "RTX A5000", "RTX 3090 Ti", "A10", "L4", "RTX 5060 Ti"]},
             "num_gpus": {"eq": 1}, "reliability2": {"gte": 0.97}, "inet_down": {"gte": 300}, "inet_up": {"gte": 200},
             "disk_space": {"gte": 80}, "cuda_max_good": {"gte": 12.4}, "cpu_cores_effective": {"gte": 4},
             "type": "on-demand", "order": [["dph_total", "asc"]], "limit": limit}
        try:
            got = self._call("POST", "/bundles/", q)
        except VastError:
            got = self._call("GET", "/bundles/?q=" + urllib.request.quote(json.dumps(q)))
        return got.get("offers", got if isinstance(got, list) else [])

    @staticmethod
    def job_cost(o, hours, down_gb, up_gb):
        return (float(o.get("dph_total") or 9) * hours + float(o.get("inet_down_cost") or 0) * down_gb
                + float(o.get("inet_up_cost") or 0) * up_gb)

    def create(self, offer_id, label, disk_gb=90):
        r = self._call("PUT", f"/asks/{int(offer_id)}/", {
            "client_id": "me", "image": IMAGE, "disk": disk_gb, "label": label, "runtype": "ssh_direct ssh_proxy",
            "env": {"NVIDIA_DRIVER_CAPABILITIES": "all"},   # NVENC needs the video capability
            "onstart": "", "python_utf8": True, "lang_utf8": True})
        iid = r.get("new_contract")
        if not r.get("success") or not iid:
            raise VastError(f"rent refused: {str(r)[:200]}")
        return int(iid)

    def attach_key(self, iid, pub):
        self._call("POST", f"/instances/{iid}/ssh/", {"ssh_key": pub})

    def instance(self, iid):
        r = self._call("GET", f"/instances/{iid}/?owner=me")
        return r.get("instances") if isinstance(r.get("instances"), dict) else r

    def destroy(self, iid):
        for k in range(5):
            try:
                self._call("DELETE", f"/instances/{iid}/")
                return True
            except VastError as e:
                print(f"  destroy {iid} attempt {k + 1}: {e}")
                time.sleep(5)
        return False

    def mine(self):
        r = self._call("GET", "/instances/?owner=me")
        return r.get("instances", []) if isinstance(r, dict) else r

    def sweep(self, keep=None):
        """Destroy every instance of ours (label prefix) except `keep`. Returns the ids destroyed."""
        gone = []
        for i in self.mine():
            if str(i.get("label") or "").startswith(LABEL) and i.get("id") != keep:
                if self.destroy(i["id"]):
                    gone.append(i["id"])
        return gone


class Host:
    """An SSH session to the rented box, with a key that exists only for this job."""

    def __init__(self, host, port, keyfile):
        self.base = ["-i", keyfile, "-p", str(port), "-o", "StrictHostKeyChecking=accept-new",
                     "-o", "UserKnownHostsFile=" + keyfile + ".known", "-o", "ServerAliveInterval=30",
                     "-o", "ConnectTimeout=20", "-o", "BatchMode=yes"]
        self.dst = f"root@{host}"
        self.port, self.keyfile = port, keyfile

    def run(self, cmd, timeout=3600, check=True):
        r = subprocess.run(["ssh", *self.base, self.dst, "bash -lc " + shlex.quote(cmd)],
                           capture_output=True, text=True, timeout=timeout)
        if check and r.returncode:
            raise VastError(f"remote `{cmd[:80]}` failed ({r.returncode}): {(r.stdout + r.stderr)[-600:]}")
        return r.stdout

    def put(self, local, remote, timeout=1800):
        b = [x if x != "-p" else "-P" for x in self.base]
        subprocess.run(["scp", *b, "-q", local, f"{self.dst}:{remote}"], check=True, timeout=timeout)

    def get(self, remote, local, timeout=3600):
        b = [x if x != "-p" else "-P" for x in self.base]
        tmp = local + ".part"
        subprocess.run(["scp", *b, "-q", f"{self.dst}:{remote}", tmp], check=True, timeout=timeout)
        os.replace(tmp, local)


def keypair(d):
    k = os.path.join(d, "vast_job_key")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "gta6-job", "-f", k], check=True)
    return k, open(k + ".pub").read().strip()


class Job:
    """with Job(vast, est_hours, down_gb, up_gb) as host: ... -> the instance is destroyed on exit, always."""

    def __init__(self, vast, est_hours, down_gb, up_gb, label_suffix=""):
        self.v, self.h, self.down, self.up = vast, est_hours, down_gb, up_gb
        self.label = LABEL + label_suffix
        self.iid, self.dir = None, tempfile.mkdtemp(prefix="vastjob_")
        self.offer = None
        self.t0 = None

    def check_money(self, need=0.0):
        bal = self.v.balance()
        if bal - need < MIN_BALANCE:
            raise VastError(f"Vast balance ${bal:.2f}; this needs ~${need:.2f} and ${MIN_BALANCE:.2f} must stay. HOLD.")
        return bal

    def __enter__(self):
        gone = self.v.sweep()
        if gone:
            print(f"  destroyed leftover instance(s) {gone}")
        offers = self.v.offers()
        if not offers:
            raise VastError("no Vast offer matches (24 GB GPU, CUDA 12.4+, 80 GB disk)")
        ranked = sorted(offers, key=lambda o: Vast.job_cost(o, self.h, self.down, self.up))
        key, pub = keypair(self.dir)
        last = None
        for o in ranked[:4]:
            est = Vast.job_cost(o, self.h, self.down, self.up)
            self.check_money(est * 1.5)
            print(f"  renting offer {o.get('id')}: {o.get('gpu_name')} ${o.get('dph_total'):.3f}/h, "
                  f"down ${o.get('inet_down_cost', 0)}/GB up ${o.get('inet_up_cost', 0)}/GB, est job ${est:.2f}")
            try:
                self.iid = self.v.create(o["id"], self.label)
                self.offer, self.t0 = o, time.time()
                self.v.attach_key(self.iid, pub)
                host = self._wait(key)
                print(f"  instance {self.iid} up after {time.time() - self.t0:.0f} s")
                return host
            except Exception as e:   # noqa: BLE001 - try the next offer, never leave this one behind
                last = e
                print(f"  offer {o.get('id')} failed: {str(e)[:200]}")
                if self.iid:
                    self.v.destroy(self.iid)
                    self.iid = None
        raise VastError(f"could not get a working instance: {last}")

    def _wait(self, key):
        t_end = time.time() + SSH_WAIT_S
        while time.time() < t_end:
            i = self.v.instance(self.iid) or {}
            st = i.get("actual_status")
            if st == "running" and i.get("ssh_host") and i.get("ssh_port"):
                h = Host(i["ssh_host"], i["ssh_port"], key)
                try:
                    if "ok" in h.run("echo ok", timeout=40):
                        return h
                except Exception:   # noqa: BLE001 - sshd not up yet
                    pass
            elif st in ("exited", "offline") or (i.get("status_msg") or "").lower().startswith("error"):
                raise VastError(f"instance {self.iid}: {st} {i.get('status_msg', '')[:120]}")
            time.sleep(15)
        raise VastError(f"instance {self.iid} not reachable after {SSH_WAIT_S // 60} min")

    def spent(self):
        if not (self.offer and self.t0):
            return 0.0
        return float(self.offer.get("dph_total") or 0) * (time.time() - self.t0) / 3600

    def __exit__(self, *exc):
        if self.iid:
            ok = self.v.destroy(self.iid)
            print(f"  instance {self.iid} {'DESTROYED' if ok else 'COULD NOT BE DESTROYED - check the Vast console'}")
            if not ok:
                raise VastError(f"instance {self.iid} could not be destroyed")
        return False

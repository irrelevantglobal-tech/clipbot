"""What each lane has spent, read from the source of truth before every job.

* Modal: the workspace billing report (real dollars, hourly), plus this run's own estimate for the
  hour the report has not closed yet. Lane 1 hands over to lane 2 at MODAL_HANDOFF (default $27 of
  the $30 monthly limit: what a window in flight + egress can still add must fit under $30).
* Vast.ai: the account balance from its API (vast.py). No card on file: at $0 instances stop.
* Cloud API calls (plan + review): every conductor run records its spend in the NAME of a tiny
  artifact, `gta6-ledger-<run>-<attempt>-api<micro$>-modal<micro$>-vast<micro$>`, so the sum over all
  runs needs one listing and no downloads. The owner's local api-lane ledger cannot see these calls;
  the cloud budget (GTA6_API_BUDGET, set from the $4.98 reserve) is enforced here instead.
"""
import datetime as dt
import json
import os
import re
import urllib.request

RATES = {   # modal billing rates, 2026-11-12, $ per second / per GiB
    "gpu": {"L4": 0.80 / 3600, "A10": 1.10 / 3600, "A10G": 1.10 / 3600, "T4": 0.59 / 3600},
    "cpu_core": 0.0473 / 3600, "mem_gib": 0.008 / 3600, "egress_gib": 0.04,
}
NAME_RX = re.compile(r"^gta6-ledger-(\d+)-(\d+)-api(\d+)-modal(\d+)-vast(\d+)$")


def month_start(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def modal_spent(now=None):
    """Real Modal spend this calendar month (UTC), to the last full hour."""
    import modal.billing
    rows = modal.billing.workspace_billing_report(start=month_start(now), resolution="h")
    return float(sum(float(r["cost"]) for r in rows))


def modal_estimate(gpu, seconds, cpu=4.0, mem_gib=8.0, egress_gib=0.0):
    g = next((v for k, v in RATES["gpu"].items() if k in (gpu or "")), RATES["gpu"]["A10"])   # unknown = dearer
    return seconds * (g + cpu * RATES["cpu_core"] + mem_gib * RATES["mem_gib"]) + egress_gib * RATES["egress_gib"]


def _gh(path):
    req = urllib.request.Request("https://api.github.com" + path, headers={
        "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"], "Accept": "application/vnd.github+json",
        "User-Agent": "clipbot-ledger"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def cloud_runs(repo=None, since=None):
    """[{run, attempt, api, modal, vast, at}] from the artifact names (expired ones count too)."""
    repo = repo or os.environ["GITHUB_REPOSITORY"]
    out, page = [], 1
    while True:
        got = _gh(f"/repos/{repo}/actions/artifacts?per_page=100&page={page}")
        arts = got.get("artifacts", [])
        for a in arts:
            m = NAME_RX.match(a.get("name", ""))
            if not m:
                continue
            at = a.get("created_at") or ""
            if since and at < since:
                continue
            out.append({"run": int(m[1]), "attempt": int(m[2]), "api": int(m[3]) / 1e6,
                        "modal": int(m[4]) / 1e6, "vast": int(m[5]) / 1e6, "at": at})
        if len(arts) < 100:
            return out
        page += 1


def api_spent(since):
    return sum(r["api"] for r in cloud_runs(since=since))


def ledger_name(run, attempt, api, modal, vast):
    return "gta6-ledger-%d-%d-api%d-modal%d-vast%d" % (int(run), int(attempt), round(api * 1e6),
                                                      round(modal * 1e6), round(vast * 1e6))

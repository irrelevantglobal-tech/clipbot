"""ONE capped Claude call, from GitHub Actions (or any machine), counted against a cloud budget.

Same models, prices and worst-case pre-check as the owner's local api-lane.js, so a cloud call
never costs more than the same call would locally. The local ledger cannot see cloud calls, so the
conductor passes in what the cloud has spent so far (summed from the run ledger, see ledger.py) and
the budget it may use; a call whose worst case would pass it raises Dry, and the caller HOLDS.

Key: env GTA6_ANTHROPIC_KEY (never ANTHROPIC_API_KEY: Claude Code would bill every chat to it).
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

MODELS = {   # $ per million tokens (api-lane.js MODELS, 2026-10-06)
    "sonnet": {"id": "claude-sonnet-5-5", "in": 2.00, "out": 10.00, "cacheRead": 0.20, "fallbacks": True},
    "haiku": {"id": "claude-haiku-5-5", "in": 0.10, "out": 0.50, "cacheRead": 0.01, "fallbacks": False,
              "longIn": 0.50, "longOut": 2.50, "longAt": 100000},
}
URL = "https://api.anthropic.com/v1/messages"


class Dry(RuntimeError):
    """The budget cannot cover this call's worst case (or the account says the credit is gone)."""


def cost(m, u):
    long = m.get("longAt") and (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)) > m["longAt"]
    pin, pout = (m["longIn"], m["longOut"]) if long else (m["in"], m["out"])
    return (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout
            + u.get("cache_read_input_tokens", 0) * m["cacheRead"]
            + u.get("cache_creation_input_tokens", 0) * pin * 1.25) / 1e6


class Budget:
    def __init__(self, limit, spent=0.0):
        self.limit, self.spent, self.calls = float(limit), float(spent), []

    def left(self):
        return self.limit - self.spent

    def call(self, task, prompt, model="sonnet", max_tokens=5000, effort="low", images=()):
        """-> (text, cost). Raises Dry before spending anything it might not be able to pay for."""
        m = MODELS[model]
        worst = ((len(prompt) + 2000 * len(images)) * m.get("longIn", m["in"]) + max_tokens * m.get("longOut", m["out"])) / 1e6
        if worst > self.left():
            raise Dry(f"{task}: worst case ${worst:.3f} > ${self.left():.3f} left of the ${self.limit:.2f} cloud budget")
        key = os.environ.get("GTA6_ANTHROPIC_KEY", "").strip()
        if not key:
            raise Dry(f"{task}: no GTA6_ANTHROPIC_KEY in this job")
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.b64encode(open(p, "rb").read()).decode()}} for p in images]
        content.append({"type": "text", "text": prompt})
        body = {"model": m["id"], "max_tokens": max_tokens, "output_config": {"effort": effort},
                "messages": [{"role": "user", "content": content}]}
        hdr = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        if m["fallbacks"]:
            body["fallbacks"] = "default"
            hdr["anthropic-beta"] = "server-side-fallback-2026-07-01"
        msg = self._post(body, hdr, task)
        c = cost(m, msg.get("usage", {}))
        self.spent += c
        self.calls.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "task": task, "model": m["id"],
                           "stop": msg.get("stop_reason"), "in": msg.get("usage", {}).get("input_tokens"),
                           "out": msg.get("usage", {}).get("output_tokens"), "cost": round(c, 6)})
        text = "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")
        return text, c

    def _post(self, body, hdr, task):
        for k in range(4):
            req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=hdr, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                txt = e.read().decode("utf-8", "replace")[:400]
                if "credit balance" in txt.lower():
                    raise Dry(f"{task}: the API account says the credit is gone ({txt[:160]})")
                if e.code == 400 and "fallbacks" in body and "fallback" in txt.lower():
                    body = {k2: v for k2, v in body.items() if k2 != "fallbacks"}
                    hdr = {k2: v for k2, v in hdr.items() if k2 != "anthropic-beta"}
                    continue
                if e.code in (429, 500, 502, 503, 529) and k < 3:
                    time.sleep(10 * (k + 1))
                    continue
                raise RuntimeError(f"{task}: API {e.code}: {txt[:200]}")
            except (urllib.error.URLError, TimeoutError) as e:
                if k < 3:
                    time.sleep(10 * (k + 1))
                    continue
                raise RuntimeError(f"{task}: API unreachable: {e}")
        raise RuntimeError(f"{task}: API retries exhausted")

"""ONE capped Claude call, from GitHub Actions (or any machine), counted against a cloud budget.
(Fallback, 13 Nov: when Claude is Dry and GTA6_GEMINI_BUDGET is set, the same prompt goes to Gemini on
Vertex AI, counted against its own budget; see polished/gemini.py. Unset = exactly the old behaviour.)

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
    def __init__(self, limit, spent=0.0, gem_limit=0.0, gem_spent=0.0):
        self.limit, self.spent, self.calls = float(limit), float(spent), []
        self.gem_limit, self.gem_spent = float(gem_limit or 0.0), float(gem_spent or 0.0)
        self.last_lane = None

    def left(self):
        return self.limit - self.spent

    def call(self, task, prompt, model="sonnet", max_tokens=5000, effort="low", images=(), temperature=None):
        """-> (text, cost). Claude first, exactly as before; only if that is Dry and a Gemini budget is set,
        the same prompt goes to Gemini (polished/gemini.py). Raises Dry when neither can pay: HOLD."""
        try:
            return self._claude(task, prompt, model, max_tokens, effort, images, temperature)
        except Dry as ex:
            if self.gem_limit <= 0:
                raise
            return self._gemini(task, prompt, model, max_tokens, images, temperature, str(ex))

    def _gemini(self, task, prompt, model, max_tokens, images, temperature, why):
        """Each model of the role in turn (gemini.models_for): an error or an answer that isn't JSON moves on to
        the next. Every try is cap-checked first and every paid answer is recorded. All errors -> Dry (HOLD);
        all answers unusable -> the last answer goes back, and the caller HOLDs on it exactly as for Claude."""
        from polished import gemini
        errs, last = [], None
        for mid in gemini.models_for(model):
            if gemini.price_of(mid) is None:
                errs.append(f"no price known for {mid}")
                continue
            worst = gemini.worst_case(mid, prompt, len(images), max_tokens)
            gleft = self.gem_limit - self.gem_spent
            if worst > gleft:
                errs.append(f"worst case ${worst:.3f} > ${gleft:.3f} left of the ${self.gem_limit:.2f} Gemini budget")
                break
            try:
                text, usage, finish, ver = gemini.generate(task, prompt, mid, max_tokens, images=images,
                                                           temperature=temperature)
            except gemini.GeminiDry as e:
                errs.append(str(e)[:200])
                continue
            c = gemini.cost(mid, usage)
            self.gem_spent += c
            self.last_lane = "gemini " + mid
            self.calls.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "task": task, "model": ver,
                               "lane": "gemini", "stop": finish, "in": usage.get("promptTokenCount"),
                               "out": (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0),
                               "cost": round(c, 6), "why_fallback": why[:200]})
            last = (text, sum(x["cost"] for x in self.calls if x.get("lane") == "gemini" and x.get("task") == task))
            if gemini.usable_json(text):
                return last
            errs.append(f"{mid}: answer not JSON")
        if last:
            return last
        raise Dry(f"{why}; Gemini fallback: " + " | ".join(errs or ["no model configured"]))

    def _claude(self, task, prompt, model, max_tokens, effort, images, temperature):
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
        if temperature is not None:
            body["temperature"] = temperature
        hdr = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        if m["fallbacks"]:
            body["fallbacks"] = "default"
            hdr["anthropic-beta"] = "server-side-fallback-2026-07-01"
        msg = self._post(body, hdr, task)
        c = cost(m, msg.get("usage", {}))
        self.spent += c
        self.last_lane = model
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
                if e.code == 400 and "temperature" in body and "temperature" in txt.lower():
                    body = {k2: v for k2, v in body.items() if k2 != "temperature"}
                    continue
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

"""Gemini on Vertex AI: the FALLBACK for the plan/review calls once the Claude budget is dry.

Claude stays the primary path (llm.Budget tries it first, unchanged). Only when that call raises Dry
(cloud budget spent, the account says the credit is gone, or no key) AND a Gemini budget is set does
the SAME prompt (and the same contact-sheet images) go to Gemini. Its spend is counted on its own
(ledger `-gem<micro$>`, budget var GTA6_GEMINI_BUDGET), never mixed with the Claude money.

Fail-safe by construction: ANY Gemini problem (no budget, unknown model price, no credentials, quota,
HTTP error, empty answer) raises llm.Dry, which is exactly today's HOLD. Nothing here can make a rough
cut ship; the answer still goes through the same JSON validation, gates and review as Claude's.

Paid from the Google Cloud $300 free-trial credit (project aiva-kamiza-i844pa, trial started 29 Oct
2026, so the 16 Nov 2026 Vertex exclusion does not apply). NEVER upgrade that billing account.

Credentials, first that works: env GTA6_GCP_TOKEN (an access token), google-auth default credentials
(GitHub Actions: Workload Identity Federation, no stored key), then the gcloud CLI (owner's laptop).
"""
import base64
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

PROJECT = os.environ.get("GTA6_GCP_PROJECT") or "aiva-kamiza-i844pa"
LOCATION = "global"
# $ per million tokens, Vertex "global" standard tier (cloud.google.com/vertex-ai/generative-ai/pricing,
# read 2026-11-13). Output includes thinking. Promo prices are NOT used: the January rate where one is
# listed, so the cap can only over-count. A model missing here is refused (no price = no call).
PRICES = {
    "gemini-3.1-flash-lite": {"in": 0.25, "out": 1.50},
    "gemini-3.5-flash-lite": {"in": 0.30, "out": 2.50},
    "gemini-3.5-flash": {"in": 1.50, "out": 9.00},
    "gemini-3.8-flash": {"in": 1.50, "out": 7.50},
    "gemini-3.1-pro": {"in": 2.00, "out": 12.00},     # <= 200K input (our prompts are ~10-30K)
}
# (2.5 models: 404 on Vertex global for this project, 13 Nov = retired; not listed so they can't be picked.)
# Claude role -> Gemini models, tried in order (the planner runs as "sonnet", the reviewer as "haiku").
# Picked 13 Nov on saved windows against Claude's own answers (PR notes): as planner 3.1 Pro gave 4/4 usable
# plans close to Sonnet's, 3.5 Flash 3/4 (one broken JSON), the Flash-Lites broken JSON / early ends. As
# reviewer 3.5 Flash (low thinking) was 4/4 on two must-fail cuts (BRB card, cold open ending on "The") and
# a good cut; 3.8 Flash and both Flash-Lites passed the dangling "The" (even at high thinking).
DEFAULT_MODELS = {"sonnet": ["gemini-3.1-pro-preview", "gemini-3.5-flash"],
                  "haiku": ["gemini-3.5-flash", "gemini-3.1-pro-preview"]}
IMG_TOKENS = 2000          # worst-case tokens per contact sheet (Gemini 3 bills ~1120 at default resolution)
THINK_ROOM = 8000          # output room for thinking on top of the answer's own max_tokens

_tok = {"v": None, "t": 0.0}


class GeminiDry(RuntimeError):
    pass


def models_for(role):
    """Comma list from GTA6_GEMINI_PLAN_MODEL / GTA6_GEMINI_REVIEW_MODEL, else the defaults above."""
    env = os.environ.get("GTA6_GEMINI_PLAN_MODEL" if role == "sonnet" else "GTA6_GEMINI_REVIEW_MODEL", "").strip()
    got = [m.strip() for m in env.split(",") if m.strip()]
    return got or list(DEFAULT_MODELS.get(role, DEFAULT_MODELS["haiku"]))


def usable_json(text):
    """The callers take the first {...} in the answer; a model answer that doesn't parse is worth one more try."""
    import re
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return isinstance(json.loads(m.group(0)), dict) if m else False
    except json.JSONDecodeError:
        return False


def price_of(mid):
    for k in sorted(PRICES, key=len, reverse=True):   # "gemini-3.5-flash-lite-preview" -> the -lite row
        if mid == k or mid.startswith(k + "-"):
            return PRICES[k]
    return None


def cost(mid, usage):
    p = price_of(mid)
    out = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)
    return ((usage.get("promptTokenCount") or 0) * p["in"] + out * p["out"]) / 1e6


def worst_case(mid, prompt, n_images, max_tokens):
    p = price_of(mid)
    return ((len(prompt) + IMG_TOKENS * n_images) * p["in"] + (max_tokens + THINK_ROOM) * p["out"]) / 1e6


def _gcloud():
    g = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not g:
        c = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Cloud SDK", "google-cloud-sdk", "bin", "gcloud.cmd")
        g = c if os.path.exists(c) else None
    return g


def token():
    if os.environ.get("GTA6_GCP_TOKEN"):
        return os.environ["GTA6_GCP_TOKEN"].strip()
    if _tok["v"] and time.time() - _tok["t"] < 40 * 60:
        return _tok["v"]
    v, errs = None, []
    try:
        import google.auth
        import google.auth.transport.requests
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        v = creds.token
    except Exception as e:   # noqa: BLE001
        errs.append("google-auth: %s" % str(e)[:120])
    if not v and _gcloud():
        try:
            r = subprocess.run([_gcloud(), "auth", "print-access-token"], capture_output=True, text=True, timeout=60)
            v = r.stdout.strip() if r.returncode == 0 else None
            if not v:
                errs.append("gcloud rc %d" % r.returncode)
        except Exception as e:   # noqa: BLE001
            errs.append("gcloud: %s" % str(e)[:120])
    if not v:
        raise GeminiDry("no Google Cloud credentials (%s)" % "; ".join(errs or ["none found"]))
    _tok.update(v=v, t=time.time())
    return v


def generate(task, prompt, mid, max_tokens, images=(), temperature=None):
    """-> (text, usage, finish_reason, model_version). Raises GeminiDry on anything that is not a usable answer."""
    parts = [{"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(open(p, "rb").read()).decode()}}
             for p in images]
    parts.append({"text": prompt})
    gen = {"maxOutputTokens": int(max_tokens + THINK_ROOM), "responseMimeType": "application/json"}
    # Gemini 3: Google advises leaving temperature at its default 1.0 (lower can loop), so the reviewer's
    # temperature 0 is not passed; its 2-of-3 rule absorbs the variance either way.
    gen["thinkingConfig"] = {"thinkingLevel": "low"}
    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen}
    url = ("https://aiplatform.googleapis.com/v1/projects/%s/locations/%s/publishers/google/models/%s:generateContent"
           % (PROJECT, LOCATION, mid))
    last = ""
    for k in range(4):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": "Bearer " + token(), "Content-Type": "application/json",
            "x-goog-user-project": PROJECT})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                msg = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            last = "HTTP %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:240])
            if e.code == 401 and k == 0 and not os.environ.get("GTA6_GCP_TOKEN"):
                _tok.update(v=None)
                continue
            if e.code in (429, 500, 502, 503, 504) and k < 3:
                time.sleep(15 * (k + 1))
                continue
            raise GeminiDry("%s: Gemini %s" % (task, last))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)[:200]
            if k < 3:
                time.sleep(15 * (k + 1))
                continue
            raise GeminiDry("%s: Gemini unreachable: %s" % (task, last))
    else:
        raise GeminiDry("%s: Gemini retries exhausted (%s)" % (task, last))
    cand = (msg.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
    usage = msg.get("usageMetadata", {})
    return text, usage, cand.get("finishReason"), msg.get("modelVersion") or mid

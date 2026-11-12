"""#episode-status in Kamiza: one live board message + event lines + phone pings for the urgent ones.

Owner 2026-11-12: "lets me know when its working, if its stuck or something and or when it switches or
needs to switch where its editing, what if it runs out of credit or dies mid edit?"

Transport: the ClipBot worker (`/clipbot/epstatus`, Bearer CLIPBOT_TOKEN, already in this job), which
hands it to Kamiza's /content/card with section "status", so it posts as the Kamiza bot, no new secret
anywhere and no KV write per update. Urgent events ALSO go to /clipbot/failed as a note = a DM to the
owner's phone. Routine events are channel-only.
* board(): edited in place, at most every BOARD_EVERY s (and at once on a stage change). Its last-edit
  time IS the heartbeat: Kamiza's watchdog reads the board and flags it stuck when a RUNNING board has
  not moved for STUCK_MIN, or a stage is past the "expected by" time written on it.
* event(): one line per thing that happened. Never contains secrets, never a VOD title.
Without CLIPBOT_WORKER (local tests) everything is printed instead, so dry runs show the exact wording.
"""
import json
import os
import sys
import time
import urllib.request

BOARD_EVERY = 240          # s between heartbeat edits (Discord PATCH: no KV, ~15/h)
STUCK_MIN = 20             # the watchdog's threshold (Kamiza); written on the board so it is visible
ICON = {"started": "▶️", "lane": "🔀", "switch_soon": "⚠️", "progress": "⏳", "held": "⏸️", "done": "✅",
        "died": "💥", "credit": "💸", "all_out": "🛑", "stuck": "🧊", "retry": "🔁", "note": "ℹ️"}
URGENT = {"held", "died", "all_out", "credit", "stuck"}


def _ts(t):
    return "<t:%d:R>" % int(t)


class Status:
    def __init__(self, vod, lo, hi, dry=False):
        self.vod, self.lo, self.hi, self.dry = str(vod), lo, hi, dry
        self.worker = os.environ.get("CLIPBOT_WORKER", "").rstrip("/")
        self.token = os.environ.get("CLIPBOT_TOKEN", "")
        self.state = {"stage": "starting", "lane": "?", "done": 0, "total": 0, "eta": None, "money": {}}
        self.t_board = 0.0
        self.t0 = time.time()
        self.stage_t = time.time()

    # ---------------- transport
    def _post(self, path, body):
        if not (self.worker and self.token):
            print("[status]", json.dumps(body, ensure_ascii=False)[:600], flush=True)
            return
        req = urllib.request.Request(self.worker + path, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json", "User-Agent": "ClipBot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
        except Exception as e:   # noqa: BLE001 - status must never break the edit
            print("[status] could not post (%s)" % str(e)[:120], flush=True)

    def win(self):
        f = lambda s: "%d:%02d" % (int(s) // 3600, int(s) % 3600 // 60)   # noqa: E731
        return "VOD %s %s-%s" % (self.vod, f(self.lo), f(self.hi))

    def event(self, kind, text, urgent=None):
        urgent = kind in URGENT if urgent is None else urgent
        line = "%s %s%s: %s" % (ICON.get(kind, "•"), "[TEST] " if self.dry else "", self.win(), text)
        self._post("/clipbot/epstatus", {"kind": "event", "content": line[:1900]})
        if urgent and not self.dry:
            self._post("/clipbot/failed", {"id": "episode pipeline", "error": line[:600], "note": True})
        elif urgent:
            print("[status] (dry run: phone ping skipped)", line, flush=True)

    def board(self, force=False, **kw):
        new_stage = "stage" in kw and kw["stage"] != self.state.get("stage")
        self.state.update({k: v for k, v in kw.items() if v is not None})
        if new_stage:
            self.stage_t = time.time()
        if not (force or new_stage) and time.time() - self.t_board < BOARD_EVERY:
            return
        self.t_board = time.time()
        s = self.state
        m = s.get("money", {})
        running = s["stage"] not in ("idle", "held", "done")
        lines = ["📋 **Episode pipeline**%s  ·  %s" % (" [TEST]" if self.dry else "",
                 "🟢 RUNNING" if running else {"held": "⏸️ HELD", "done": "✅ DONE", "idle": "💤 IDLE"}[s["stage"]]),
                 "**Window:** %s  ·  **lane:** %s" % (self.win(), s["lane"]),
                 "**Stage:** %s%s" % (s["stage"], "  (%d/%d)" % (s["done"], s["total"]) if s.get("total") else "")]
        if s.get("eta"):
            lines.append("**Expected by:** %s" % _ts(s["eta"]))
        if s.get("detail"):
            lines.append(str(s["detail"])[:300])
        money = []
        if "modal" in m:
            money.append("Modal $%.2f of $30" % m["modal"])
        if "vast" in m:
            money.append("Vast $%.2f left of $10" % m["vast"])
        if "api" in m:
            money.append("API reserve $%.2f left" % m["api"])
        if money:
            lines.append("**Money:** " + "  ·  ".join(money))
        lines.append("*Updated %s. If this says RUNNING and stops updating for %d min, the watchdog posts 🧊 stuck.*"
                     % (_ts(time.time()), STUCK_MIN))
        self._post("/clipbot/epstatus", {"kind": "board", "content": "\n".join(lines)[:1900]})


if __name__ == "__main__":
    # used by the workflow's failure step: python -m polished.status died <vod> <from> <to> "<text>"
    kind, vod, lo, hi, text = sys.argv[1:6]
    st = Status(vod, float(lo), float(hi or 0), dry=os.environ.get("POLISHED_DRY") == "1")
    st.event(kind, text)
    st.board(force=True, stage="held" if kind == "held" else "idle", detail=text)

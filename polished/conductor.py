"""Polished marathon episodes for ONE window of a Twitch VOD. Runs in GitHub Actions (polished.yml).

    python -m polished.conductor --vod <id> --from <s> --to <s> [--final] [--upload] [--lane auto|modal|vast]
                                 [--max-episodes N] [--state <dir>] [--answers <file>] --out out

The same contract as the stream-copy cutter (clipbot/episodes.py), so the worker, the Discord cards
and ep_publish.py do not change: it writes out/episodes.json, the workflow sends it to /clipbot/epoffer.

1. LANE (real spend, read first): Modal while its month-to-date bill + this window's estimate stays
   under MODAL_HANDOFF; else Vast.ai while its balance covers the job; else HOLD.
2. ANALYSE the window in 30-min chunks on GPUs (Modal: in parallel; Vast: one box, in turn), each
   fetching its own exact Twitch segments at 1920x1080. Saved to the state dir as they arrive.
3. PLAN episodes one after another IN ORDER, each starting exactly at the last one's next_start
   (plan_cut.py; one capped Claude call each, against the cloud budget). Nothing falls between two.
4. RENDER every planned episode on GPUs while the episodes already back are gated, reviewed (Haiku
   contact-sheet check) and uploaded PRIVATE (marker guard: never twice).
5. POLISHED ONLY: an episode that fails a data gate or the review, or cannot be planned/reviewed for
   lack of budget, is HELD: nothing from it onwards gets a card (Part order), the manifest is sent as
   `partial` without next_from (the worker keeps the window and retries it after 6 h), and the owner
   gets one line. A retry reuses everything in the state dir: no second analysis, no second plan call.
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from polished import ledger, stages  # noqa: E402
from polished.status import Status  # noqa: E402

CHUNK = 1800.0
MODAL_HANDOFF = float(os.environ.get("GTA6_MODAL_HANDOFF") or 27.0)
EST_PER_STREAM_H = {"modal": 0.75, "vast": 0.30}    # $ per stream-hour, conservative (T1: ~$0.38-0.54 on L4/A10 + egress)
RENDER_PAR = 3
RENDER_TRIES = 2          # 13 Nov fault QA: one transient render failure held the whole window for 6 h; retry once first
MAX_EPISODES_PER_WINDOW = 30
MARK = "clipbot:ep:"


# FAULT SWITCHES for the QA run (owner: "make sure its actually gonna work"). Env POLISHED_FAULTS = comma
# list; honoured ONLY when the run does not upload (a dry run can never ship anything a fault produced).
FAULTS = set()
FAULT_HELP = {
    "modal_near_cap": "Modal bill read as just under the hand-over mark: runs on Modal + 'about to switch' warning",
    "modal_at_cap": "Modal bill read as AT the mark: the window goes to Vast",
    "vast_zero": "Vast balance read as $0: Vast refuses cleanly",
    "vast_start_fail": "every Vast rent attempt fails: the next offer, then HOLD; nothing left running",
    "vast_leftover": "the Vast instance is NOT destroyed at the end (the next run's sweep must destroy it)",
    "api_dry": "cloud API (Claude) budget 0: the Gemini fallback plans/reviews if GTA6_GEMINI_BUDGET is set, else HOLD",
    "gemini_dry": "Gemini fallback budget 0: with api_dry, planning HOLDS (lane 4), never a rough cut",
    "all_lanes_out": "modal_at_cap + vast_zero: HOLD + phone ping",
    "kill_render": "the first Modal render is cancelled mid-way: the window dies, nothing partial is kept",
    "client_dies": "this Actions client exits hard while renders run (exit 137)",
    "modal_down": "workflow: every Modal call refused (bad token): the media comes from the private GitHub copy and the window runs on Vast",
    "stall": "no board update for POLISHED_STALL_MIN (default 30) min while RUNNING: the watchdog must post stuck",
    "fail_window": "the window fails on purpose (3 in a row = the worker steps over it)",
    "no_1080": "the VOD is treated as having no 1920x1080 rendition: hard refuse before any GPU spend",
    "dup_unreadable": "the duplicate-upload guard's sources are unreadable: the run must stop before any upload",
}


def hm(sec):
    """Stream time as the owner reads it: 5:07 = 5 h 7 min into the VOD."""
    sec = int(sec or 0)
    return "%d:%02d" % (sec // 3600, sec % 3600 // 60)


def fault(name):
    return name in FAULTS


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def jdump(obj, path):
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def jload(path, default=None):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Run:
    def __init__(self, a):
        self.a = a
        self.state = a.state
        os.makedirs(self.state, exist_ok=True)
        os.makedirs(a.out, exist_ok=True)
        self.cost = {"modal": 0.0, "vast": 0.0, "api": 0.0, "gemini": 0.0}
        self.modal_spent0 = None
        self.api_spent0 = 0.0
        self.gem_spent0 = 0.0
        self.gem_told = False
        self.notes = []
        self.lane = None
        self.money = {}
        self.all_out = False
        self.st = None

    # ------------------------------------------------------------------ money + lane
    def api_budget(self):
        return 0.0 if fault("api_dry") else float(os.environ.get("GTA6_API_BUDGET") or 0.0)

    def gem_budget(self):
        """Gemini (Vertex AI, Google trial credit) = fallback only, used when the Claude call is Dry. 0 = off."""
        return 0.0 if fault("gemini_dry") else float(os.environ.get("GTA6_GEMINI_BUDGET") or 0.0)

    def api_left_args(self):
        return ["--budget-limit", "%.4f" % self.api_budget(), "--budget-spent", "%.4f" % (self.api_spent0 + self.cost["api"]),
                "--gem-limit", "%.4f" % self.gem_budget(), "--gem-spent", "%.4f" % (self.gem_spent0 + self.cost["gemini"])]

    def add_llm_cost(self, calls):
        gem = [c for c in calls if c.get("lane") == "gemini"]
        self.cost["api"] += sum(c.get("cost", 0) for c in calls if c.get("lane") != "gemini")
        self.cost["gemini"] += sum(c.get("cost", 0) for c in gem)
        if gem and not self.gem_told:
            self.gem_told = True
            self.notes.append("Gemini fallback used (Claude dry): %s" % gem[0].get("why_fallback", "")[:160])
            self.st.event("lane", "Claude API money is out, so **Gemini** (Google trial credit) is planning/reviewing "
                          "now. Same checks and review gate; Gemini left ~$%.2f." % max(
                              0.0, self.gem_budget() - self.gem_spent0 - self.cost["gemini"]), urgent=False)
        self.money["gemini"] = max(0.0, self.gem_budget() - self.gem_spent0 - self.cost["gemini"])

    def pick_lane(self, stream_h):
        want = self.a.lane
        why = []
        if want in ("auto", "modal") and os.environ.get("MODAL_DEPLOYED", "success") not in ("success", "skipped", ""):
            why.append("the Modal app could not be deployed (%s: capped or down)" % os.environ.get("MODAL_DEPLOYED"))
        elif want in ("auto", "modal") and os.environ.get("MODAL_TOKEN_ID"):
            try:
                self.modal_spent0 = ledger.modal_spent()
                est = stream_h * EST_PER_STREAM_H["modal"]
                if fault("modal_near_cap"):
                    self.modal_spent0 = MODAL_HANDOFF - est * 1.2
                if fault("modal_at_cap") or fault("all_lanes_out"):
                    self.modal_spent0 = MODAL_HANDOFF
                self.money["modal"] = self.modal_spent0
                if self.modal_spent0 + est < MODAL_HANDOFF:
                    log(f"lane modal: month-to-date ${self.modal_spent0:.2f} + this window ~${est:.2f} < ${MODAL_HANDOFF:.2f}")
                    if self.modal_spent0 + 2 * est >= MODAL_HANDOFF:
                        self.st.event("switch_soon", f"Modal is at ${self.modal_spent0:.2f} of its ${MODAL_HANDOFF:.0f} "
                                      "hand-over mark. This window still runs on Modal; the NEXT one moves to Vast.ai.")
                    return "modal"
                why.append(f"Modal at ${self.modal_spent0:.2f} of its ${MODAL_HANDOFF:.0f} hand-over mark")
            except Exception as e:   # noqa: BLE001
                why.append(f"Modal billing unreadable ({str(e)[:100]})")
        elif want in ("auto", "modal"):
            why.append("no Modal token in this job")
        if want in ("auto", "vast") and os.environ.get("VAST_API_KEY"):
            from polished import vast
            try:
                bal = 0.0 if (fault("vast_zero") or fault("all_lanes_out")) else vast.Vast().balance()
                self.money["vast"] = bal
                est = stream_h * EST_PER_STREAM_H["vast"]
                if bal - est * 1.5 >= vast.MIN_BALANCE:
                    log(f"lane vast: balance ${bal:.2f}, this window ~${est:.2f}")
                    if want == "auto":
                        self.st.event("lane", "Moved to Vast.ai: " + "; ".join(why) + f". Vast has ${bal:.2f}.", urgent=False)
                    if bal - 3 * est * 1.5 < vast.MIN_BALANCE:
                        self.st.event("switch_soon", f"Vast.ai has ${bal:.2f} left, about 2 more windows. After that "
                                      "windows HOLD until there is a lane (December's Modal credit, the laptop after the week, "
                                      "or your call).")
                    return "vast"
                why.append(f"Vast balance ${bal:.2f} cannot cover ~${est:.2f}")
            except Exception as e:   # noqa: BLE001
                why.append(f"Vast unreadable ({str(e)[:100]})")
        elif want in ("auto", "vast"):
            why.append("no Vast key in this job")
        self.all_out = True
        raise Hold("no GPU lane can take this window: " + "; ".join(why))

    # ------------------------------------------------------------------ sleep skip (24/7 marathon)
    def sleep_skip(self, vod, lo, hi, created):
        """-> sleepskip report; report["skips"] = [[a, b]] VOD seconds whose GPU analysis is skipped (polished/
        sleepskip.py: Resting screen + no word, CPU only, before any GPU is rented). Off unless GTA6_SLEEP_SKIP=1.
        Any failure = nothing skipped (the whole window is scanned, as before). A retry reuses the saved decision,
        so the chunk grid always matches the chunks already in the state dir."""
        p = os.path.join(self.state, "sleepskip.json")
        old = jload(p)
        if old and abs(old["window"][0] - lo) < 0.01 and abs(old["window"][1] - hi) < 0.01:
            return old
        rep = {"window": [lo, hi], "skips": []}
        if os.environ.get("GTA6_SLEEP_SKIP") != "1":
            rep["off"] = True
            return rep
        self.st.board(force=True, stage="sleep check (CPU: Resting screen? anyone talking?)", eta=time.time() + 15 * 60)
        try:
            from datetime import datetime
            from polished import sleepskip
            t0 = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            presses = [dict(x, t=x["t"] - t0) for x in self.st.sleep_log()]
            src = sleepskip.TwitchSource(vod)
            rep = sleepskip.decide(src, lo, hi, presses)
            rep["fetch"] = {"segments": [0, 0], "recording": bool(src.recording), "vod_total": round(src.total, 3),
                            "fps": 60.0}
        except Exception as e:   # noqa: BLE001 - fail open: scan everything
            rep = {"window": [lo, hi], "skips": [], "error": str(e)[:300]}
            log("sleep skip failed, scanning the whole window:", rep["error"])
            self.st.event("note", "Sleep check could not run (%s): the whole window is scanned, nothing skipped."
                          % str(e)[:120], urgent=False)
        jdump(rep, p)
        return rep

    def tell_sleep(self, rep, lo):
        sk = rep.get("skips") or []
        for p in rep.get("presses", []):   # what happened to each 💤 press inside this window
            if p.get("end") is None:
                self.st.event("sleep", "💤 press at stream time %s (%s): still on at the end of this window."
                              % (hm(p["t"]), p.get("by") or "?"), urgent=False)
            else:
                self.st.event("awake", "💤 press at stream time %s (%s) ended at %s: %s." % (
                    hm(p["t"]), p.get("by") or "?", hm(p["end"]), p.get("why", "")[:160]), urgent=False)
        if sk:
            h = sum(b - a for a, b in sk) / 3600
            spans = ", ".join("%s-%s" % (hm(a), hm(b)) for a, b in sk)
            self.st.event("sleep", "Sleep skip ON for %s stream time (Resting screen, nobody talking): %.1f h not sent "
                          "to the GPU. The minutes around it are analysed as usual." % (spans, h), urgent=False)
            self.st.board(force=True, sleep="💤 ON %s (%.1f h skipped)" % (spans, h))
            self.notes.append("sleep skip: %s" % spans)
        elif not rep.get("off"):
            self.st.board(force=True, sleep="☀️ OFF (nothing asleep long enough to skip%s)" % (
                "; check failed, all scanned" if rep.get("error") else ""))

    # ------------------------------------------------------------------ analysis
    def chunks(self, lo, hi):
        out, t = [], lo
        while t < hi - 1.0:
            out.append((round(t, 3), round(min(hi, t + CHUNK), 3)))
            t += CHUNK
        return out

    def analyse(self, vod, lo, hi, gpu, sleep=None):
        skips = (sleep or {}).get("skips") or []
        if skips:
            from polished import sleepskip
            plan = sleepskip.chunk_plan(lo, hi, skips, CHUNK)
        else:   # sleep skip off or nothing to skip: exactly the old 30-min grid
            plan = [(a, b, False) for a, b in self.chunks(lo, hi)]
        todo, got = [], {}
        for a_, b_, sk in plan:
            c = (a_, b_)
            p = os.path.join(self.state, "chunk_%d.json" % int(c[0]))
            r = jload(p)
            if r and abs(r["lo"] - c[0]) < 0.01 and bool(r.get("skipped")) == sk and (
                    abs(r["hi"] - c[1]) < 1.0 or (not sk and c[1] >= hi - 0.5 and r["hi"] <= c[1])):
                got[c] = r
            elif sk:   # asleep on the Resting screen: no GPU (sleepskip.skipped_chunk says what it holds)
                r = sleepskip.skipped_chunk(vod, a_, b_, sleep["fetch"])
                jdump(r, p)
                got[c] = r
            else:
                todo.append(c)
        n_sk = sum(1 for r in got.values() if r.get("skipped"))
        log(f"analysis: {len(got) - n_sk} chunk(s) reused from state, {n_sk} skipped (asleep), {len(todo)} to run on {self.lane}")
        n_all = len(got) + len(todo)
        self.st.board(force=True, stage="analysing (whisper, picture, menus)", done=len(got), total=n_all,
                      eta=time.time() + (12 * 60 if self.lane == "modal" else 7 * 60 * len(todo) + 15 * 60))
        for c, r in (gpu.analyze_many(vod, todo) if todo else []):
            jdump(r, os.path.join(self.state, "chunk_%d.json" % int(c[0])))
            got[c] = r
            self.st.board(done=len(got))
            log(f"  chunk {c[0]:.0f}-{c[1]:.0f}: {len(r['segments'])} segments, {r['gpu']}, {r['timings'].get('total_s')} s")
        an = stages.merge_chunks(list(got.values()))
        jdump(an, os.path.join(self.state, "analysis.json"))
        return an

    # ------------------------------------------------------------------ planning
    def plan_all(self, an, lo, final, vod_start):
        """-> (plans in WINDOW seconds, next_from in VOD seconds or None, hold reason or None)."""
        plans, start, hold = [], (None if vod_start else 0.0), None
        idx = jload(os.path.join(self.state, "plans.json"), {}) or {}
        answers = jload(self.a.answers, {}) if self.a.answers else {}
        apath = os.path.join(self.state, "analysis.json")
        self.st.board(force=True, stage="planning episodes", done=0, total=0, eta=time.time() + 20 * 60)
        for n in range(MAX_EPISODES_PER_WINDOW):
            self.st.board(done=len(plans), detail="planned %d so far" % len(plans))
            if self.a.max_episodes and len(plans) >= self.a.max_episodes:
                break
            key = "vodstart" if start is None else "%.3f" % start
            ep_dir = os.path.join(self.state, "plan_%s" % key.replace(".", "_"))
            os.makedirs(ep_dir, exist_ok=True)
            done = idx.get(key)
            # Reuse only a finished answer: a plan (rc 0) or a tail (rc 2) whose file is there. QA 13 Nov: a crash
            # (rc 1, e.g. the API overloaded for a minute) used to be replayed from plans.json on every retry, so
            # the window died 3 times without planning again and the worker skipped it.
            if done and key not in (answers.get("plans") or {}) and (
                    (done["rc"] == 0 and os.path.exists(os.path.join(ep_dir, "plan.json"))) or
                    (done["rc"] == 2 and os.path.exists(os.path.join(ep_dir, "tail.json")))):
                rc = done["rc"]
            else:
                cmd = [sys.executable, "-m", "polished.plan_cut", apath, ep_dir, "--game", self.a.series,
                       "--brand", self.brand] + self.api_left_args()
                cmd += ["--vod-start"] if start is None else ["--start", "%.3f" % start]
                cmd += ["--final"] if final else []
                ans = answers.get("plans", {}).get(key)
                if ans:   # lane 4: a Claude session answered this prompt; validate it, no API call
                    open(os.path.join(ep_dir, "plan_llm_session.json"), "w", encoding="utf-8").write(ans)
                    cmd += ["--reuse-llm", os.path.join(ep_dir, "plan_llm_session.json")]
                elif done and done["rc"] not in (0, 2, 3) and os.path.exists(os.path.join(ep_dir, "plan_llm.json")):
                    # it crashed AFTER the model answered: check the same answer again, no second paid call
                    cmd += ["--reuse-llm", os.path.join(ep_dir, "plan_llm.json")]
                for f in ("plan.json", "tail.json", "hold.json", "llm_calls.json"):   # calls: never count one twice
                    if os.path.exists(os.path.join(ep_dir, f)):
                        os.remove(os.path.join(ep_dir, f))
                r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
                print(r.stdout[-3000:], r.stderr[-2000:], flush=True)
                rc = r.returncode
                calls = jload(os.path.join(ep_dir, "llm_calls.json"), []) or []
                self.add_llm_cost(calls)
                idx[key] = {"rc": rc}
                jdump(idx, os.path.join(self.state, "plans.json"))
            if rc == 0:
                p = jload(os.path.join(ep_dir, "plan.json"))
                p["_dir"] = ep_dir
                plans.append(p)
                nxt = float(p["next_start"])
                if start is not None and nxt <= start + 1.0:
                    raise RuntimeError(f"planner did not move forward ({start} -> {nxt})")
                start = nxt
                continue
            if rc == 2:
                t = jload(os.path.join(ep_dir, "tail.json"))
                if t.get("quiet_words") is not None:
                    self.st.event("note", "only %d words in this window: treated as away (asleep on the resting "
                                  "screen). If you were playing, tell Claude (the audio may be broken)."
                                  % t["quiet_words"], urgent=False)
                return plans, lo + float(t["next_start"]), None
            if rc == 3:
                h = jload(os.path.join(ep_dir, "hold.json"), {})
                hold = f"planning episode {len(plans) + 1} (stream time {hm(lo + (start or 0.0))}): {h.get('why', 'held')}"
                return plans, None, hold
            raise RuntimeError(f"plan_cut failed (rc {rc}) at {key}")
        # stopped by --max-episodes (tests) or the per-window cap: continue from the last end
        return plans, (lo + start if start is not None else lo), None


class Hold(RuntimeError):
    pass


# ---------------------------------------------------------------------- GPU lanes
class ModalLane:
    def __init__(self, run):
        import modal
        self.run = run
        self.modal = modal
        self.fa = modal.Function.from_name("gta6-polished", "analyze")
        self.fr = modal.Function.from_name("gta6-polished", "render")
        self.vol = modal.Volume.from_name("gta6-xfer", create_if_missing=True)

    def _charge(self, rep, egress_gib=0.0):
        self.run.cost["modal"] += ledger.modal_estimate(rep.get("gpu"), float(rep.get("container_s") or 0), egress_gib=egress_gib)

    def analyze_many(self, vod, todo):
        calls = {c: self.fa.spawn(vod, c[0], c[1]) for c in todo}
        for c, fc in calls.items():
            try:
                r = fc.get(timeout=3600)
            except Exception as e:   # noqa: BLE001 - one retry per chunk, then the window fails
                log(f"  chunk {c[0]:.0f} failed once ({str(e)[:160]}); retrying")
                r = self.fa.remote(vod, c[0], c[1])
            self._charge(r)
            yield c, r

    def render_many(self, vod, jobs, sink, stop):
        """jobs: [(key, plan_vod, title)]. Puts (key, local_mp4 | None, report | error) on sink, in order.
        At most RENDER_PAR renders in flight; once `stop` is set (an episode was held) nothing new starts.
        The end marker (None) is ALWAYS sent, whatever goes wrong in here."""
        try:
            self._render_many(vod, jobs, sink, stop)
        except Exception as e:   # noqa: BLE001
            sink.put(("?", None, "render loop failed: %s" % str(e)[:200]))
        finally:
            sink.put(None)

    def _render_many(self, vod, jobs, sink, stop):
        b = self.run.ident
        with cf.ThreadPoolExecutor(RENDER_PAR) as ex:
            futs = []
            pending = list(jobs)

            def top_up():
                while pending and len([f for _, f in futs if not f.done()]) < RENDER_PAR and not stop.is_set():
                    k, p, t = pending.pop(0)
                    futs.append((k, ex.submit(self._render_one, vod, p, t, b, k)))
            top_up()
            i = 0
            while i < len(futs):
                k, f = futs[i]
                i += 1
                waited = 0
                while not f.done():
                    top_up()
                    time.sleep(2)
                    waited += 2
                top_up()
                try:
                    rep = f.result()
                except Exception as e:   # noqa: BLE001
                    sink.put((k, None, "render failed: %s" % str(e)[:300]))
                    continue
                dst = os.path.join(self.run.a.out, "%s.mp4" % k)
                tmp = dst + ".part"
                try:   # QA R11: a read error here used to leave the job waiting 4 h for an end marker never sent
                    with open(tmp, "wb") as fh:
                        for blk in self.vol.read_file("out/%s.mp4" % k):
                            fh.write(blk)
                except Exception as e:   # noqa: BLE001
                    sink.put((k, None, "could not copy the render out of Modal: %s" % str(e)[:200]))
                    continue
                if os.path.getsize(tmp) != rep["size"]:
                    sink.put((k, None, "download size %d != %d" % (os.path.getsize(tmp), rep["size"])))
                    continue
                os.replace(tmp, dst)
                if not rep.get("reused"):
                    self._charge(rep, egress_gib=rep["size"] / 2 ** 30)
                for f_ in ("out/%s.mp4" % k, "out/%s.json" % k):
                    try:
                        self.vol.remove_file(f_)
                    except Exception:   # noqa: BLE001 - the sweep gets it
                        pass
                sink.put((k, dst, rep))
                top_up()

    def _render_one(self, vod, p, t, b, k):
        for attempt in range(RENDER_TRIES):
            try:
                return self._render_attempt(vod, p, t, b, k)
            except Exception as e:   # noqa: BLE001 - a lost container / cancelled call: once more, then HOLD
                if attempt + 1 >= RENDER_TRIES:
                    raise
                log(f"  render {k} failed ({str(e)[:120]}); rendering it again")
                self.run.st.event("retry", f"an episode render failed ({str(e)[:100]}); rendering it again", urgent=False)

    def _render_attempt(self, vod, p, t, b, k):
        fc = self.fr.spawn(vod, p, t, self.run.a.series, b["brand"], b["login"], k, b["retired"])
        # QA faults fire right after the call is placed (a fast render used to finish before the 60 s mark)
        if fault("client_dies"):
            time.sleep(5)
            log("FAULT client_dies: exiting hard while a render runs")
            os._exit(137)
        if fault("kill_render") and not getattr(self, "_killed_one", False):
            self._killed_one = True
            time.sleep(5)
            log("FAULT kill_render: cancelling the first render")
            fc.cancel()
            raise RuntimeError("render cancelled mid-way (fault kill_render)")
        while True:
            if getattr(self, "cancel_first", False):
                self.cancel_first = False
                fc.cancel()
                raise RuntimeError("render cancelled mid-way (fault kill_render)")
            try:
                return fc.get(timeout=5)
            except TimeoutError:
                continue

    def sweep(self, older_h=6):
        try:
            for e in self.vol.listdir("out"):
                if time.time() - e.mtime > older_h * 3600:
                    self.vol.remove_file(e.path)
        except Exception:   # noqa: BLE001
            pass


class VastLane:
    def __init__(self, run, stream_h):
        from polished import vast
        self.run, self.vast = run, vast
        self.v = vast.Vast()
        # downloads ~3.6 GB per stream-hour (8 Mbps) x 1.1 for chunk overlap + renders' own fetch; uploads = episodes
        self.job = vast.Job(self.v, est_hours=stream_h * 0.9 + 0.3, down_gb=stream_h * 8.0, up_gb=stream_h * 3.5,
                            label_suffix="-%s" % os.environ.get("GITHUB_RUN_ID", "local"))
        if fault("vast_start_fail"):
            def boom(*a_, **k_):
                raise vast.VastError("rent refused (fault vast_start_fail)")
            self.v.create = boom
        if fault("vast_zero"):
            self.v.balance = lambda: 0.0
        self.job.keep_instance = fault("vast_leftover")
        self.host = None

    def __enter__(self):
        self.host = self.job.__enter__()
        try:
            return self._setup()
        except BaseException:
            # 12 Nov: setup failed AFTER the box was rented, and `with` never calls __exit__ when __enter__
            # raises, so the instance was left running. Destroy it here, then re-raise.
            self.run.cost["vast"] += self.job.spent()
            self.job.__exit__(None, None, None)
            raise

    def _setup(self):
        tar = os.path.join(self.job.dir, "repo.tgz")
        subprocess.run(["tar", "czf", tar, "-C", ROOT, "polished", "clipbot/style/fonts", "fonts"], check=True)
        self.host.run("mkdir -p /root/repo", timeout=60, step="mkdir")
        self.host.put(tar, "/root/repo.tgz", step="upload code")
        self.host.run("cd /root/repo && tar xzf /root/repo.tgz && rm /root/repo.tgz", timeout=120, step="unpack code")
        t0 = time.time()
        out = ""
        for st, lim in (("apt", 300), ("ffmpeg", 300), ("pip", 900), ("models", 900), ("check", 120)):
            self.job.check_money()
            o = self.host.run("cd /root/repo && PY=/opt/conda/bin/python bash polished/setup_gpu.sh " + st,
                              timeout=lim, step="setup " + st)
            log("    %s: %s" % (st, " | ".join(o.strip().splitlines()[-2:])[:240]))
            out += o
        log("  vast setup %.0f s" % (time.time() - t0))
        if "SETUP_DONE" not in out:
            raise RuntimeError("vast setup did not finish")
        return self

    def __exit__(self, *exc):
        self.run.cost["vast"] += self.job.spent()
        return self.job.__exit__(*exc)

    def _py(self, args, timeout):
        return self.host.run("cd /root/repo && /opt/conda/bin/python -m polished.worker " + args, timeout=timeout,
                             step=args.split()[0] + " " + args.split()[2][:12])

    def analyze_many(self, vod, todo):
        for c in todo:
            self.job.check_money()
            rp = "/root/an_%d.json" % int(c[0])
            self._py("analyze %s %.3f %.3f %s" % (vod, c[0], c[1], rp), timeout=1800)
            loc = os.path.join(self.job.dir, "an.json")
            self.host.get(rp, loc, timeout=300, step="get analysis")
            self.host.run("rm -f " + rp, timeout=60, step="cleanup")
            yield c, jload(loc)

    def render_many(self, vod, jobs, sink, stop):
        b = self.run.ident
        for k, p, t in jobs:
            if stop.is_set():
                break
            for attempt in range(RENDER_TRIES):
                try:
                    sink.put(self._render_vast(vod, k, p, t, b))
                    break
                except Exception as e:   # noqa: BLE001 - once more on the same box, then HOLD
                    if attempt + 1 < RENDER_TRIES:
                        log(f"  render {k} failed on the box ({str(e)[:120]}); rendering it again")
                        self.run.st.event("retry", f"an episode render failed ({str(e)[:100]}); rendering it again",
                                          urgent=False)
                        continue
                    sink.put((k, None, "render failed: %s" % str(e)[:300]))
                    sink.put(None)
                    return
        sink.put(None)

    def _render_vast(self, vod, k, p, t, b):
        import shlex
        self.job.check_money()
        pf = os.path.join(self.job.dir, "plan_%s.json" % k)
        jdump(dict(p, _retired=b["retired"]), pf)   # in the file, not on the box's command line
        self.host.put(pf, "/root/plan_%s.json" % k)
        od = "/root/ep_%s" % k
        self._py("render %s /root/plan_%s.json %s %s %s %s %s" % (
            vod, k, shlex.quote(t), shlex.quote(self.run.a.series), shlex.quote(b["brand"]),
            shlex.quote(b["login"]), od), timeout=3600)
        rep_l = os.path.join(self.job.dir, "rep_%s.json" % k)
        self.host.get(od + "/report.json", rep_l)
        rep = jload(rep_l)
        dst = os.path.join(self.run.a.out, "%s.mp4" % k)
        self.host.get(od + "/episode.mp4", dst)
        if os.path.getsize(dst) != rep["size"]:
            raise RuntimeError("download size mismatch")
        self.host.run("rm -rf %s/_work %s/episode.mp4" % (od, od))
        return (k, dst, rep)


class LocalLane:
    """Lane 3: this machine's own GPU (the owner's laptop), same stages in-process. Explicit only.
    Refuses while OBS runs (the owner is live) unless POLISHED_ALLOW_WHILE_LIVE=1, which is set only
    after the Elgato-day load test passed AND the owner said yes. Pair with POLISHED_ARCHIVE (backlog)."""

    def __init__(self, run):
        from polished import backlog
        if backlog.obs_live() and os.environ.get("POLISHED_ALLOW_WHILE_LIVE") != "1":
            raise Hold("laptop lane refused: OBS is running (never render while live without a passed load test + OK)")
        from polished.brand import camp_fetch
        camp_fetch.ensure()          # the stings' media: private GitHub copy (this machine's gh login), then Modal
        self.run = run

    def analyze_many(self, vod, todo):
        for c in todo:
            t0 = time.time()
            r = stages.analyze_chunk(vod, c[0], c[1], os.path.join(self.run.a.out, "_an_%d" % int(c[0])))
            r["container_s"] = round(time.time() - t0, 1)
            shutil.rmtree(os.path.join(self.run.a.out, "_an_%d" % int(c[0])), ignore_errors=True)
            yield c, r

    def render_many(self, vod, jobs, sink, stop):
        from polished.brand import camp as stings
        b = self.run.ident
        bdir = os.path.join(self.run.a.out, "_brand")
        outro = None
        for k, p, t in jobs:
            if stop.is_set():
                break
            p = dict(p)
            index = p.pop("_index", None)                    # the episode's place in its series (intro rotation)
            for attempt in range(RENDER_TRIES):
                try:
                    if outro is None:
                        outro = stings.outro(self.run.a.series, b["brand"], b["login"], bdir, b["retired"])
                    intro = stings.intro(t, self.run.a.series, b["brand"], b["login"], bdir, b["retired"], index=index)
                    mp4, rep = stages.render_episode(vod, p, intro, outro, os.path.join(self.run.a.out, "_ep_" + k))
                    dst = os.path.join(self.run.a.out, "%s.mp4" % k)
                    os.replace(mp4, dst)
                    rep["size"] = os.path.getsize(dst)
                    sink.put((k, dst, rep))
                    break
                except Exception as e:   # noqa: BLE001 - once more, then HOLD
                    if attempt + 1 < RENDER_TRIES:
                        log(f"  render {k} failed ({str(e)[:120]}); rendering it again")
                        continue
                    sink.put((k, None, "render failed: %s" % str(e)[:300]))
                    sink.put(None)
                    return
        sink.put(None)


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vod", required=True)
    ap.add_argument("--from", dest="lo", type=float, default=0.0)
    ap.add_argument("--to", dest="hi", type=float, default=0.0)
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--series", default="GTA 6")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--lane", default="auto", choices=["auto", "modal", "vast", "local"])
    ap.add_argument("--max-episodes", type=int, default=0)
    ap.add_argument("--keep", type=int, default=0, help="test: keep the first N rendered mp4s in out/ (upload=false)")
    ap.add_argument("--state", default="state")
    ap.add_argument("--answers", default="")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    from clipbot import common, episodes
    run = Run(a)
    run.ident = {"brand": common.BRAND, "login": common.LOGIN,
                 "retired": [n for n in common.IDENT.get("legacy", []) if n]}   # the stings refuse these names
    run.brand = common.BRAND
    v = common.helix(f"videos?id={a.vod}")["data"][0]
    total = episodes._secs(v["duration"])
    lo, hi = a.lo, min(a.hi or total, total)
    man = {"vod": a.vod, "vod_title": v["title"], "vod_created": v["created_at"], "series": a.series,
           "window": [lo, hi], "final": a.final, "polished": True, "episodes": [], "holes": []}
    mpath = os.path.join(a.out, "episodes.json")
    t_run = time.time()
    if not a.upload:
        FAULTS.update(x.strip() for x in (os.environ.get("POLISHED_FAULTS") or "").split(",") if x.strip())
        if FAULTS:
            log("FAULTS ON (dry run):", sorted(FAULTS))
    elif os.environ.get("POLISHED_FAULTS"):
        log("POLISHED_FAULTS ignored: this run uploads")
    run.st = Status(a.vod, lo, hi, dry=not a.upload)
    run.st.event("started", "%s window started%s." % ("Polishing" if a.upload else "TEST polish of a",
                                                      " (faults: %s)" % ", ".join(sorted(FAULTS)) if FAULTS else ""),
                 urgent=False)
    try:
        run.api_spent0 = ledger.api_spent(since=os.environ.get("GTA6_API_SINCE") or "2026-11-01") if os.environ.get("GITHUB_TOKEN") else 0.0
    except Exception as e:   # noqa: BLE001 - unreadable ledger = assume the worst: the budget is spent
        run.api_spent0 = run.api_budget()
        run.notes.append("API ledger unreadable (%s): treated as spent" % str(e)[:80])
    try:
        since = os.environ.get("GTA6_GEMINI_SINCE") or "2026-11-13"
        run.gem_spent0 = ledger.gemini_spent(since=since) if (os.environ.get("GITHUB_TOKEN") and run.gem_budget() > 0) else 0.0
    except Exception as e:   # noqa: BLE001 - same rule: unreadable = spent, so Gemini is off and windows HOLD
        run.gem_spent0 = run.gem_budget()
        run.notes.append("Gemini ledger unreadable (%s): treated as spent" % str(e)[:80])
    log(f"VOD {a.vod} window {lo:.0f}-{hi:.0f}{' FINAL' if a.final else ''}; cloud API ${run.api_spent0:.3f} of ${run.api_budget():.2f}")
    if hi - lo < 60 and not a.final:
        man["next_from"] = lo
        jdump(man, mpath)
        return 0
    hold = None
    try:
        # 1080p preflight: refuse before any GPU money is spent (fetch() re-checks every file anyway)
        from polished import fetch
        if fault("no_1080"):
            raise RuntimeError(f"VOD {a.vod} has no 1920x1080 rendition (fault no_1080); refusing (owner: everything 1080p)")
        fetch.rendition(a.vod)
        run.money["api"] = max(0.0, run.api_budget() - run.api_spent0)
        if run.gem_budget() > 0:
            run.money["gemini"] = max(0.0, run.gem_budget() - run.gem_spent0)
        sleep = run.sleep_skip(a.vod, lo, hi, v["created_at"])
        run.tell_sleep(sleep, lo)
        gpu_s = (hi - lo) - sum(b - a_ for a_, b in sleep.get("skips") or [])
        man["sleep_skips"] = sleep.get("skips") or []
        run.st.board(force=True, stage="choosing a lane", money=run.money)
        if gpu_s < 1.0:   # the whole window is him asleep: nothing to analyse, no GPU rented
            run.lane = "none"
        else:
            run.lane = "local" if a.lane == "local" else run.pick_lane(gpu_s / 3600)
        run.st.board(force=True, lane={"modal": "Modal (cloud GPU)", "vast": "Vast.ai (rented GPU)",
                                       "local": "laptop", "none": "none (asleep: no GPU needed)"}[run.lane],
                     money=run.money)
        man["lane"] = run.lane
        if run.lane == "none":
            ctx = _null(None)
        elif run.lane == "local":
            ctx = _null(LocalLane(run))
        elif run.lane == "modal":
            lane_cm = ModalLane(run)
            lane_cm.sweep()
            ctx = _null(lane_cm)
        else:
            ctx = VastLane(run, (hi - lo) / 3600)
        with ctx as gpu:
            an = run.analyse(a.vod, lo, hi, gpu, sleep)
            plans, next_from, hold = run.plan_all(an, lo, a.final, vod_start=(lo < 1.0))
            log(f"planned {len(plans)} episode(s); next window from {next_from}; hold: {hold}")
            hold = process(run, gpu, an, plans, lo, man, common, episodes) or hold
            if hold is None:
                man["next_from"] = round(next_from if next_from is not None else hi, 3)
    except Hold as h:
        hold = str(h)
    except Exception as e:   # noqa: BLE001 - reported, then the job fails so the worker retries the window
        traceback.print_exc()
        man["error"] = str(e)[:500]
        jdump(man, mpath)
        finish(run, man, t_run)
        n_ok = sum(1 for x in man["episodes"] if x.get("video"))
        run.st.event("died", "stopped during **%s**: %s. Nothing half-made was uploaded%s. %s" % (
            run.st.state.get("stage"), str(e)[:220],
            " (%d finished episode(s) before it are up and get their cards)" % n_ok if n_ok else "",
            "The worker starts this window again at its next check (3 tries, then it steps over it and tells you)."
            if a.upload else "Test run: nothing else happens."))
        run.st.board(force=True, stage="idle", detail="last window died; see the line above")
        open(os.path.join(a.out, "status_died.txt"), "w").write("posted")
        raise
    if hold:
        man["partial"] = True
        man.pop("next_from", None)
        man["held"] = hold
        kind = "all_out" if run.all_out else ("credit" if ("budget" in hold or "credit" in hold) else "held")
        run.st.event(kind, "HELD: %s. Nothing rough is posted and nothing after it gets a card. %s" % (
            hold[:400], "Ask Claude: \"polished hold\" (a Claude session can plan/review it on your plan)."
            if kind != "all_out" else "Every GPU lane is out of credit: windows wait (Twitch keeps the VODs ~2 weeks). "
            "Decide: December's Modal credit, the laptop after the week, or top-up."))
        run.st.board(force=True, stage="held", detail=hold[:300])
    else:
        n = len(man["episodes"])
        run.st.event("done", "%d episode(s) %s in %.0f min. Next window starts at %s." % (
            n, "uploaded PRIVATE, cards in #episode-approvals" if a.upload else "rendered (test, nothing uploaded)",
            (time.time() - t_run) / 60, "stream time " + hm(man.get("next_from", hi))), urgent=False)
        run.st.board(force=True, stage="idle", done=0, total=0, eta=None,
                     detail="last window: %d episode(s), $%.2f" % (n, sum(run.cost.values())))
    jdump(man, mpath)
    finish(run, man, t_run)
    return 0


class _null:
    def __init__(self, x):
        self.x = x

    def __enter__(self):
        return self.x

    def __exit__(self, *e):
        return False


def tell(common, ident, line):
    log("OWNER:", line)
    try:
        common.worker("/clipbot/failed", {"id": ident, "error": line, "note": True})
    except Exception as e:   # noqa: BLE001
        log("could not tell the owner:", str(e)[:120])


def finish(run, man, t_run):
    man["cost"] = {k: round(v, 4) for k, v in run.cost.items()}
    man["wall_s"] = round(time.time() - t_run, 1)
    man["notes"] = run.notes
    jdump(man, os.path.join(run.a.out, "episodes.json"))
    name = ledger.ledger_name(os.environ.get("GITHUB_RUN_ID", 0), os.environ.get("GITHUB_RUN_ATTEMPT", 0),
                              run.cost["api"], run.cost["modal"], run.cost["vast"], run.cost["gemini"])
    open(os.path.join(run.a.out, "ledger_name.txt"), "w").write(name)
    log("cost this run:", man["cost"], "->", name)


def series_base(common, series, vod, first_start):
    """How many episodes of this series come BEFORE this window, from the worker's episode list (every episode that
    got a card). Ordered by (VOD id, start second), so a re-run of the same window gets the same number and the
    intro rotation (camp.pick_variant) never repeats a shot between consecutive episodes, across windows and VODs.
    None if the list can't be read: the intros then fall back to a hash of the title (no-repeat not guaranteed)."""
    try:
        items = common.worker("/clipbot/eplist").get("items", [])
    except Exception as e:   # noqa: BLE001
        log("episode list unreadable for the intro rotation (%s): title hash instead" % str(e)[:80])
        return None
    n = 0
    for it in items:
        if (it.get("series") or series) != series:
            continue
        parts = str(it.get("marker", "")).rsplit(":", 2)
        try:
            v, s = int(parts[-2]), float(parts[-1])
        except (ValueError, IndexError):
            continue
        if (v, s) < (int(vod), float(first_start)):
            n += 1
    return n


def holes_of(plan_vod):
    ks = plan_vod["keep"]
    return [[round(x[1], 2), round(y[0], 2)] for x, y in zip(ks, ks[1:]) if y[0] - x[1] > 0.05]


def process(run, gpu, an, plans, lo, man, common, episodes):
    """Render, gate, review, upload IN ORDER. Returns a hold reason or None."""
    a = run.a
    jobs, meta = [], {}
    base = series_base(common, a.series, a.vod, plans[0]["bounds"][0] + lo) if plans else None
    for i, p in enumerate(plans, 1):
        pv = {"keep": [[round(s + lo, 3), round(e + lo, 3)] for s, e in p["keep"]],
              "cold": [round(p["cold"][0] + lo, 3), round(p["cold"][1] + lo, 3)]}
        key = hashlib.sha1(json.dumps([a.vod, pv, p["title"], run.brand, a.series]).encode()).hexdigest()[:16]
        start, end = round(p["bounds"][0] + lo, 2), round(p["bounds"][1] + lo, 2)
        if base is not None:
            pv["_index"] = base + i - 1                    # after the key: the shot never changes the episode's identity
        meta[key] = {"i": i, "plan": p, "pv": pv, "start": start, "end": end}
        jobs.append((key, pv, p["title"]))
    if not jobs:
        return None
    if fault("fail_window"):
        raise RuntimeError("fault fail_window: this window fails on purpose")
    if fault("dup_unreadable"):
        common.recent_uploads = lambda *x, **k: None      # the uploads playlist cannot be read
        episodes.marker_on_channel(f"{MARK}{a.vod}:{int(meta[jobs[0][0]]['start'])}")   # must raise, so nothing uploads
    if a.upload:
        common.channel_gate()
    # already up (a retry): never render or upload twice
    todo = []
    for key, pv, t in jobs:
        m = meta[key]
        m["marker"] = f"{MARK}{a.vod}:{int(m['start'])}"
        if a.upload:
            have = episodes.marker_on_channel(m["marker"])
            if have:
                log(f"  ep {m['i']}: already on YouTube as {have}")
                m["video"] = have
                continue
        todo.append((key, pv, t))
    if fault("stall"):
        # QA: the board says RUNNING and then goes silent for the whole stall window, so the Kamiza watchdog
        # (20 min) must post the stuck line; then the run carries on and finishes (13 Nov: the old version only
        # skipped heartbeats and the run ended in 10 min, so the watchdog never saw a silence)
        mins = float(os.environ.get("POLISHED_STALL_MIN") or 30)
        run.st.board(force=True, stage="render, review, upload", detail="QA stall: no update for %.0f min" % mins)
        log("FAULT stall: silent for %.0f min" % mins)
        time.sleep(mins * 60)
    sink, stop = queue.Queue(), threading.Event()
    th = threading.Thread(target=gpu.render_many, args=(a.vod, todo, sink, stop), daemon=True)
    th.start()
    ready = {}
    hold = None
    kept_n = 0
    for key, pv, t in jobs:
        m = meta[key]
        if "video" not in m:
            t_wait = time.time()
            while key not in ready:
                try:
                    item = sink.get(timeout=60)
                except queue.Empty:
                    if time.time() - t_wait > 4 * 3600:
                        break
                    run.st.board(detail="episode %d of %d rendering" % (m["i"], len(jobs)))   # heartbeat, max 1 edit / 4 min
                    continue
                if item is None:
                    break
                ready[item[0]] = item
            got = ready.get(key)
            if not got or got[1] is None:
                hold = f"episode {m['i']} (stream time {hm(m['start'])}): {got[2] if got else 'never rendered'}"
                break
            _, mp4, rep = got
            g = rep.get("gates", {})
            if not g.get("pass"):
                hold = f"episode {m['i']} (stream time {hm(m['start'])}): data gate failed (verify rc {g.get('verify_rc')}, A/V {g.get('av_sync')})"
                break
            rv = review(run, an, m, mp4)
            m["review"] = rv
            if rv.get("verdict") != "pass":
                hold = f"episode {m['i']} (stream time {hm(m['start'])}): review {rv.get('verdict')}: " + \
                    "; ".join(x.get("what", "") for x in rv.get("issues", []) if x.get("severity") == "blocker")[:300]
                break
            m["report"] = {k: rep.get(k) for k in ("duration", "expected", "gpu", "encoder", "timings", "size")}
            run.st.board(force=True, stage="render, review, upload", done=m["i"], total=len(jobs),
                         detail="episode %d passed the gates and the review" % m["i"])
            if a.upload:
                title, desc = draft_copy(run, m, episodes)
                try:
                    vid = episodes.upload_private(mp4, title, desc, episodes.tags_for(a.series))
                except Exception as e:   # noqa: BLE001
                    why = str(e)
                    try:   # QA R7: urllib's message is just "HTTP Error 400"; the reason is in the body
                        why += " " + e.read().decode("utf-8", "replace")[:400]
                    except Exception:   # noqa: BLE001
                        pass
                    if (getattr(e, "code", None) in (400, 403) or "quota" in why.lower()
                            or "uploadlimitexceeded" in why.lower()):
                        # the daily YouTube quota (shared by every lane) is spent: HOLD, don't die. Dying makes the
                        # worker retry at once and burn its 3 tries before the 08:00 UK reset, then skip the window.
                        hold = (f"episode {m['i']} (stream time {hm(m['start'])}): YouTube upload quota used up "
                                "(resets 08:00 UK); the window is kept and retried")
                        break
                    raise
                m["video"] = vid
                log(f"  ep {m['i']}: uploaded PRIVATE https://youtu.be/{vid}")
            if kept_n < a.keep and not a.upload:
                kept_n += 1
                shutil.move(mp4, os.path.join(a.out, "keep_ep%02d.mp4" % m["i"]))
            elif os.path.exists(mp4):
                os.remove(mp4)
        p = m["plan"]
        ep = {"i": m["i"], "start": m["start"], "end": m["end"], "spans": m["pv"]["keep"], "cold": m["pv"]["cold"],
              "kept": round(sum(e - s for s, e in m["pv"]["keep"]), 1), "marker": m["marker"],
              "hook": p["title"], "description": p["description"], "hashtags": [],
              "draft_title": p["youtube_title"], "chapters": p["chapters"], "review": m.get("review", {}).get("verdict"),
              "plan_problems": p["stats"].get("problems", []), "report": m.get("report")}
        if m.get("video"):
            ep["video"] = m["video"]
        man["episodes"].append(ep)
        man["holes"] += holes_of(m["pv"])
        jdump(man, os.path.join(a.out, "episodes.json"))
    if hold:
        stop.set()
    th.join(timeout=60 if not hold else 900)
    return hold


def draft_copy(run, m, episodes):
    """Private-upload copy until approval sets the real Part number (ep_publish rewrites both)."""
    p = m["plan"]
    title = episodes.title_for(run.a.series, "?", p["title"]).replace("Part ?", "(draft)")
    desc = episodes.description_for(p["description"].replace("Part {PART}.", "").strip(), [], m["marker"])
    return title, desc


def review(run, an, m, mp4):
    wd = os.path.join(run.state, "review_%s" % hashlib.sha1(m["marker"].encode()).hexdigest()[:10])
    os.makedirs(wd, exist_ok=True)
    psha = hashlib.sha1(open(os.path.join(HERE, "review_prompt.md"), "rb").read()).hexdigest()[:12]
    old = jload(os.path.join(wd, "review.json"))
    if old and old.get("mp4_size") == os.path.getsize(mp4) and old.get("prompt_sha") == psha:
        return old                              # same file, same rubric: no second call
    answers = jload(run.a.answers, {}) if run.a.answers else {}
    sess = (answers.get("reviews") or {}).get(m["marker"])
    if sess:   # lane 4: a Claude session looked at these sheets and gave the verdict
        sess = dict(sess, lane="claude-session", mp4_size=os.path.getsize(mp4))
        jdump(sess, os.path.join(wd, "review.json"))
        return sess
    jdump(m["plan"], os.path.join(wd, "plan.json"))
    shutil.copy(os.path.join(run.state, "analysis.json"), os.path.join(wd, "analysis.json"))

    def one_look(temp):
        r = subprocess.run([sys.executable, "-m", "polished.review", mp4, wd, "--brand", run.brand,
                            "--temperature", str(temp)] + run.api_left_args(),
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        print(r.stdout[-2500:], r.stderr[-1500:], flush=True)
        calls = jload(os.path.join(wd, "review_calls.json"), []) or []
        run.add_llm_cost(calls)
        if r.returncode == 3:
            h = jload(os.path.join(wd, "review_hold.json"), {})
            return {"verdict": "hold", "issues": [{"severity": "blocker", "what": h.get("why", "review held")}]}
        if r.returncode:
            return {"verdict": "hold", "issues": [{"severity": "blocker", "what": "review crashed rc %d" % r.returncode}]}
        return jload(os.path.join(wd, "review.json"), {})

    # 13 Nov fault QA: the SAME cut passed at 22:22 and failed at 03:40 (reviewer variance). First look at
    # temperature 0; a fail gets two more independent looks and HOLDS only if 2 of the 3 fail.
    res = one_look(0.0)
    if res.get("verdict") == "fail":
        looks = [res] + [one_look(1.0) for _ in range(2)]
        fails = [x for x in looks if x.get("verdict") != "pass"]
        if len(fails) >= 2:
            res = dict(fails[0], looks=[x.get("verdict") for x in looks])
        else:
            res = dict(next(x for x in looks if x.get("verdict") == "pass"), looks=[x.get("verdict") for x in looks],
                       overruled=[i.get("what") for i in res.get("issues", []) if i.get("severity") == "blocker"])
        log("  review second look: %s -> %s" % (res["looks"], res.get("verdict")))
    if res.get("verdict") == "hold":
        return res
    res["mp4_size"] = os.path.getsize(mp4)
    res["prompt_sha"] = psha
    jdump(res, os.path.join(wd, "review.json"))
    os.remove(os.path.join(wd, "analysis.json"))
    return res


if __name__ == "__main__":
    sys.exit(main())

"""Agent-run interface for the Studio backend, plus a scripted StubRunner.

The web layer only talks to `AgentRunner`. A later task implements the real agent loop
behind the same interface; `StubRunner` plays a deterministic fake session for tests and
UI development. It writes REAL events/gates through `adfarming.studio` (so the gate
enforcement, replay and memory code paths are exercised) but only ever 'produces'
placeholder artifacts (copies of tiny fixture files, or generated placeholder bytes).

Shared helpers (`set_status`, `get_status`, `charge`, caps) live here so the real runner
uses exactly the same status/cost bookkeeping as the stub.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import threading
import time
import zipfile
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .studio import db, events, gates, memory

log = logging.getLogger("adfarming.runner")
TERMINAL = ("done", "error", "cancelled")


class CostCapExceeded(Exception):
    pass


# ---------------------------------------------------------------- config / helpers
def out_root() -> Path:
    return Path(os.environ.get("ADFARMING_OUT") or "out")


def run_dir(run_id: str) -> Path:
    return out_root() / run_id


def run_cap_usd() -> float:
    return float(os.environ.get("STUDIO_RUN_CAP_USD") or 1.5)


def daily_cap_usd() -> float:
    return float(os.environ.get("STUDIO_DAILY_CAP_USD") or 15)


def run_cost(run_id: str) -> float:
    with db.read() as c:
        return float(c.execute("SELECT COALESCE(SUM(usd),0) FROM cost_ledger WHERE run_id=?",
                               (run_id,)).fetchone()[0])


def daily_cost() -> float:
    day = db.now()[:10]  # UTC date
    with db.read() as c:
        return float(c.execute("SELECT COALESCE(SUM(usd),0) FROM cost_ledger WHERE ts>=?",
                               (day,)).fetchone()[0])


def check_caps(run_id: str | None = None, extra: float = 0.0) -> None:
    """Raise CostCapExceeded when the run or the day is at/over its cap."""
    if run_id is not None and run_cost(run_id) + extra > run_cap_usd() + 1e-9:
        raise CostCapExceeded(f"run cost cap ${run_cap_usd():.2f} reached")
    if daily_cost() + extra > daily_cap_usd() + 1e-9:
        raise CostCapExceeded(f"daily cost cap ${daily_cap_usd():.2f} reached")


def charge(run_id: str, tool: str, usd: float) -> None:
    """Cap-checked cost record (cost ledger + cost event). Call BEFORE doing paid work."""
    check_caps(run_id, usd)
    events.record_cost(run_id, tool, usd)


def set_status(run_id: str, status: str) -> None:
    events.append_event(run_id, "run_status", "system", {"status": status})


def get_status(run_id: str) -> str:
    with db.read() as c:
        r = c.execute("SELECT payload FROM events WHERE run_id=? AND kind='run_status' "
                      "ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    if not r:
        return "running"
    import json
    return json.loads(r[0])["status"]


# ---------------------------------------------------------------- interface
class AgentRunner(ABC):
    """start/resume are expected to return promptly after leaving the run either
    'awaiting_human' (a gate is pending), 'done', or 'error'; a real runner may do the
    work in a background thread/task and return immediately after setting 'running'."""

    @abstractmethod
    def start(self, run_id: str, client_id: str, goal: str | None) -> None: ...

    @abstractmethod
    def resume(self, run_id: str) -> None:
        """Called after a human answered the pending gate (answer is already in the log)."""

    @abstractmethod
    def cancel(self, run_id: str) -> None: ...

    def recover(self, run_id: str) -> None:
        """Restart a run left 'interrupted'/'error'. A pending gate just goes back to awaiting_human."""
        if events.rebuild_run_state(run_id)["pending_gate"]:
            set_status(run_id, "awaiting_human")
        else:
            self.resume(run_id)


class RealRunner(AgentRunner):
    """Runs the real agent loop (`adfarming.agent.AgentLoop`) in a small thread pool.

    start/resume return right away; the loop does one advance (up to the next gate or the end) in the
    background and the status events (awaiting_human / done / error) are what the SSE stream and the UI
    read. One advance per run at a time (lock); the web layer enforces one running run globally.
    """

    SAFE_ERROR = "The agent hit an unexpected problem and stopped. Your approvals are saved; use Resume to try again."

    def __init__(self, loop=None, workers: int = 2) -> None:
        self._loop = loop
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="adf-agent")
        self._guard = threading.Lock()
        self._busy: set[str] = set()
        self._cancelled: set[str] = set()

    def _agent(self):
        if self._loop is None:
            from .agent import AgentLoop
            self._loop = AgentLoop()
        return self._loop

    # -- interface
    def start(self, run_id: str, client_id: str, goal: str | None) -> None:
        run_dir(run_id).mkdir(parents=True, exist_ok=True)
        self._submit(run_id)

    def resume(self, run_id: str) -> None:
        self._submit(run_id)

    def recover(self, run_id: str) -> None:
        self._submit(run_id)  # the loop itself returns awaiting_human when a gate is pending

    def cancel(self, run_id: str) -> None:
        with self._guard:
            self._cancelled.add(run_id)
        events.append_event(run_id, "commentary", "system", {"text": "Run cancelled."})

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Test helper: block until no advance is in flight."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._guard:
                if not self._busy:
                    return True
            time.sleep(0.01)
        return False

    # -- internals
    def _submit(self, run_id: str) -> None:
        with self._guard:
            if run_id in self._busy:
                return  # one advance per run; the in-flight one will pick up the newest log state
            self._cancelled.discard(run_id)
            self._busy.add(run_id)
        set_status(run_id, "running")
        try:
            self._pool.submit(self._work, run_id)
        except Exception:
            with self._guard:
                self._busy.discard(run_id)
            raise

    def _is_cancelled(self, run_id: str) -> bool:
        with self._guard:
            return run_id in self._cancelled

    def _work(self, run_id: str) -> None:
        try:
            if self._is_cancelled(run_id):
                return
            check_caps(run_id)
            res = self._agent().advance(run_id)
            if self._is_cancelled(run_id):
                if get_status(run_id) != "cancelled":
                    set_status(run_id, "cancelled")
                return
            status = (res or {}).get("status")
            if status in ("awaiting_human", "done", "error"):
                if get_status(run_id) != status:
                    set_status(run_id, status)
            elif status == "running":
                pass  # another advance holds the loop lock; it reports its own status
            else:
                self._fail(run_id, self.SAFE_ERROR)
        except CostCapExceeded as e:
            self._fail(run_id, f"Spending cap reached: {e}. Nothing more was spent.")
        except Exception as e:  # never leak a stack trace or key to the client
            log.error("agent advance failed for run %s: %s: %s", run_id, type(e).__name__, _redact(str(e)))
            self._fail(run_id, self.SAFE_ERROR)
        finally:
            with self._guard:
                self._busy.discard(run_id)

    @staticmethod
    def _fail(run_id: str, msg: str) -> None:
        try:
            events.append_event(run_id, "error", "system", {"message": msg})
            set_status(run_id, "error")
        except Exception:
            log.exception("could not record failure for run %s", run_id)


def _redact(text: str) -> str:
    out = text[:300]
    for k, v in os.environ.items():
        if v and len(v) >= 8 and any(w in k.upper() for w in ("KEY", "SECRET", "PASSWORD", "TOKEN")):
            out = out.replace(v, "***")
    return out


# ---------------------------------------------------------------- stub
def _mem_item(i: dict) -> dict:
    return {k: i[k] for k in ("id", "kind", "text", "source", "status", "pinned", "evidence",
                              "created_at")} | {"evidence": i.get("evidence") or None}


class StubRunner(AgentRunner):
    """Scripted session: brief -> ideas -> preview -> edit -> check -> ship."""

    FRAMES = ["f1.webp", "f2.webp", "f3.webp"]
    CUTS = {"9x16": "c_9x16.mp4", "1x1": "c_1x1.mp4", "16x9": "c_16x9.mp4"}
    NEXT_COST = {"brief": 0.03, "ideas": 0.12, "preview": 0.20, "edit": 0.06}

    def __init__(self, fixtures_dir: str | os.PathLike | None = None) -> None:
        d = fixtures_dir or os.environ.get("STUDIO_STUB_FIXTURES")
        self.fixtures = Path(d) if d else None

    # -- interface
    def start(self, run_id: str, client_id: str, goal: str | None) -> None:
        run_dir(run_id).mkdir(parents=True, exist_ok=True)
        events.append_event(run_id, "commentary", "agent",
                            {"text": "Reading approved client memory and the brief."})
        mem = memory.read_memory(client_id, run_id=run_id)
        self._tool(run_id, "muse-spark", 0.01, "Drafting a one-paragraph brief.")
        self._request(run_id, "brief", {
            "type": "brief",
            "text": (goal or "Show the product in use and end with a clear call to action.")
                    + " Keep it warm, honest and under 30 seconds.",
            "memory_cited": [m["id"] for m in mem[:2]],
            "cost_estimate_usd": self.NEXT_COST["brief"]})

    def resume(self, run_id: str) -> None:
        st = events.rebuild_run_state(run_id)
        ans = self._last_answer(run_id)
        step, kind = ans["step"], ans["kind"]
        client_id = st["client_id"]
        text = (ans.get("text") or "").strip()
        set_status(run_id, "running")
        if kind == "approve":
            nxt = events.STEPS[events.step_index(step) + 1] if step != "ship" else None
            if nxt is None:
                events.append_event(run_id, "commentary", "agent", {"text": "Shipped. Session complete."})
                set_status(run_id, "done")
                return
            self._produce(run_id, client_id, nxt, ans, steer=None)
        elif kind == "back":
            self._produce(run_id, client_id, ans.get("to") or step, ans, steer=None)
        else:  # steer / edit -> revise same step
            self._produce(run_id, client_id, step, ans, steer=text or None, edited=ans.get("fields"))

    def cancel(self, run_id: str) -> None:
        events.append_event(run_id, "commentary", "system", {"text": "Run cancelled."})

    # -- internals
    def _last_answer(self, run_id: str) -> dict:
        for e in reversed(events.list_events(run_id)):
            if e["kind"] == "gate_answered":
                g = gates.get_gate(e["payload"]["gate_id"])
                a = g["answer"] or {}
                p = a.get("payload") or {}
                return {"step": e["payload"]["step"], "kind": e["payload"]["kind"],
                        "to": e["payload"].get("to"), "text": a.get("text"),
                        "choice": p.get("choice"), "fields": p.get("fields")}
        raise RuntimeError("resume without an answered gate")

    def _tool(self, run_id: str, tool: str, usd: float, say: str | None = None) -> None:
        if say:
            events.append_event(run_id, "commentary", "agent", {"text": say})
        events.append_event(run_id, "tool_started", "agent", {"tool": tool})
        charge(run_id, tool, usd)
        events.append_event(run_id, "tool_done", "agent", {"tool": tool, "usd": usd})

    def _request(self, run_id: str, step: str, card: dict) -> int:
        gates.propose_artifact(run_id, step, step)
        gid = gates.request_gate(run_id, step, {"card": card})
        set_status(run_id, "awaiting_human")
        return gid

    def _put(self, run_id: str, name: str, kind: str) -> str:
        """Place a placeholder artifact: copy a fixture if one exists, else generate bytes."""
        dst = run_dir(run_id) / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        src = self.fixtures / name if self.fixtures else None
        if src is None and self.fixtures is None:
            src = None
        if src and src.is_file():
            shutil.copyfile(src, dst)
        elif self.fixtures and (self.fixtures / f"placeholder.{kind}").is_file():
            shutil.copyfile(self.fixtures / f"placeholder.{kind}", dst)
        else:
            dst.write_bytes(_placeholder(kind, name))
        return f"/api/runs/{run_id}/artifacts/{name}"

    def _concepts(self, steer: str | None, mem_ids: list[str]) -> list[dict]:
        sfx = f" (revised: {steer})" if steer else ""
        out = []
        for n, (title, angle) in enumerate([("Morning ritual", "calm routine"),
                                            ("Before and after", "proof"),
                                            ("Ask the maker", "founder voice")], 1):
            out.append({
                "id": f"c{n}", "title": title + sfx, "angle": angle,
                "hook": f"Placeholder hook {n}", "cta": "Shop now",
                "beats": [{"clip": "clip_a.mp4", "src_in": 1.0 * n, "src_out": 3.0 * n + 1,
                           "purpose": "hook", "on_screen_text": f"Hook {n}"},
                          {"clip": "clip_b.mp4", "src_in": 0.0, "src_out": 4.0,
                           "purpose": "proof", "on_screen_text": "Proof"}],
                "length_s": 18 + n, "why_it_fits": "Matches the approved brand voice.",
                "risks": "Placeholder concept for UI development.",
                "cites": mem_ids[:1]})
        return out

    def _produce(self, run_id: str, client_id: str, step: str, ans: dict,
                 steer: str | None, edited: dict | None = None) -> None:
        mem = memory.read_memory(client_id, run_id=run_id)
        mem_ids = [m["id"] for m in mem]
        prev = _latest_card(run_id, step) or {}
        if step == "brief":
            text = (edited or {}).get("text") or prev.get("text", "Brief")
            if steer:
                text += f" Steer: {steer}"
            card = {"type": "brief", "text": text, "memory_cited": mem_ids[:2],
                    "cost_estimate_usd": self.NEXT_COST["brief"]}
        elif step == "ideas":
            self._tool(run_id, "muse-spark", 0.03, "Proposing three concepts.")
            cs = self._concepts(steer, mem_ids)
            for ed in ([(edited or {})] if edited else []):
                for c in cs:
                    c["hook"] = ed.get("hook", c["hook"]); c["cta"] = ed.get("cta", c["cta"])
            _write_json(run_id, "concepts.json", cs)
            card = {"type": "ideas", "concepts": cs, "recommended": cs[0]["id"],
                    "cost_estimate_usd": self.NEXT_COST["ideas"]}
        elif step == "preview":
            gates.assert_approved(run_id, "storyboard")  # code-enforced paid-tool gate
            self._tool(run_id, "muse-image", 0.12, "Drawing storyboard frames (AI stand-ins).")
            frames = [self._put(run_id, n, "webp") for n in self.FRAMES]
            card = {"type": "preview", "frames": frames, "cost_estimate_usd": self.NEXT_COST["preview"]}
        elif step == "edit":
            gates.assert_approved(run_id, "build_cuts")
            self._tool(run_id, "sam-3.1", 0.06, "Building the three cuts.")
            for n in self.CUTS.values():
                self._put(run_id, n, "mp4")
            ideas = _latest_card(run_id, "ideas") or {}
            chosen = (ideas.get("concepts") or [{}])[0]
            beats = prev.get("beats") or chosen.get("beats") or []
            caps = prev.get("captions") or [{"start": 0.0, "end": 1.7, "text": "Placeholder caption"},
                                            {"start": 1.7, "end": 3.4, "text": "Second caption"}]
            hook = prev.get("hook") or chosen.get("hook", "Hook")
            cta = prev.get("cta") or chosen.get("cta", "Shop now")
            if edited:
                hook = edited.get("hook", hook); cta = edited.get("cta", cta)
                if isinstance(edited.get("captions"), list):
                    caps = [dict(c) for c in caps]
                    for i, c in enumerate(edited["captions"]):
                        if isinstance(c, dict) and i < len(caps) and "text" in c:
                            caps[i]["text"] = c["text"]
            if steer:
                hook = f"{hook} ({steer})"
            card = {"type": "edit", "beats": beats, "captions": caps, "hook": hook, "cta": cta,
                    "cost_estimate_usd": 0.06}
        elif step == "check":
            self._tool(run_id, "muse-wonder", 0.06, "Checking the cut against the brief.")
            hist = list(prev.get("history") or [])
            attempt = len(hist) + 1
            ok = attempt >= 2 or bool(steer)
            score = 5 if ok else 3
            hist.append({"attempt": attempt, "score": score, "pass": ok,
                         "summary": "Looks good." if ok else "Caption timing is slightly off."})
            verdict = {"pass": ok, "score": score, "issues": [] if ok else ["caption timing"],
                       "fixes": [] if ok else ["re-time captions"], "summary": hist[-1]["summary"]}
            card = {"type": "check", "verdict": verdict, "history": hist,
                    "proposed_fixes": [] if ok else ["Re-time captions to speech"]}
        elif step == "ship":
            cuts = {k: f"/api/runs/{run_id}/artifacts/{n}" for k, n in self.CUTS.items()}
            zname = "run.zip"
            _write_zip(run_id, zname, [n for n in self.CUTS.values()])
            memory.propose_memory(client_id, "performance",
                                  "Short Hinglish captions held attention in the stub run.",
                                  evidence={"run_id": run_id}, run_id=run_id)
            props = [_mem_item(i) for i in memory.list_memory(client_id, "proposed")]
            card = {"type": "ship", "cuts": cuts, "zip": f"/api/runs/{run_id}/artifacts/{zname}",
                    "memory_proposals": props}
        else:
            raise ValueError(step)
        self._request(run_id, step, card)


def _latest_card(run_id: str, step: str) -> dict | None:
    for e in reversed(events.list_events(run_id)):
        if e["kind"] == "gate_requested" and e["payload"]["step"] == step:
            return (e["payload"].get("payload") or {}).get("card")
    return None


def _write_json(run_id: str, name: str, obj) -> None:
    import json
    d = run_dir(run_id); d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(obj, indent=2))


def _write_zip(run_id: str, name: str, members: list[str]) -> None:
    d = run_dir(run_id)
    with zipfile.ZipFile(d / name, "w") as z:
        for m in members:
            if (d / m).is_file():
                z.write(d / m, m)


def _placeholder(kind: str, name: str) -> bytes:
    if kind == "webp":
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 114), (200, 120, 80)).save(buf, "WEBP")
        return buf.getvalue()
    return b"PLACEHOLDER " + name.encode() + b"\n" + bytes(range(256))


def make_runner() -> AgentRunner:
    kind = (os.environ.get("STUDIO_RUNNER") or "stub").lower()
    if kind == "stub":
        return StubRunner()
    if kind == "real":
        if (os.environ.get("STUDIO_AGENT") or "auto").lower() in ("auto", ""):
            os.environ["STUDIO_AGENT"] = "wonder"  # auto = Wonder with per-turn scripted fallback
        return RealRunner()
    raise ValueError(f"STUDIO_RUNNER must be stub|real, got {kind!r}")

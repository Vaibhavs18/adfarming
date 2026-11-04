"""Agent loop: per-step STATELESS turns.

Each advance() starts a fresh step turn from the event log alone (system prompt + approved memory +
brief + artifact summaries + the human's last answer). The turn ends when ask_human creates a gate
(or finish is called). A paused run holds no thread and no previous_response_id; previous_response_id
is used only between the (max 8) turns inside one step, with an in-step stateless replay if the
server rejects it. Two drivers share one interface: WonderDriver (model picks tools) and
ScriptedDriver (fixed table, always works). On ANY Wonder failure the step turn falls back to the
ScriptedDriver and a `fallback` event is logged.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..studio import events
from . import prompts
from . import compose_tools
from .tools import Backends, Toolbox, run_cap_usd, tool_schemas

PRICE_IN, PRICE_OUT = 4 / 1e6, 20 / 1e6   # muse-wonder-1.0 $/token (task brief; unverified vs billing)
MODEL = "muse-wonder-1.0"
MAX_STEP_TURNS = 8        # Wonder turns inside one step
MAX_CALLS_PER_TURN = 14
MAX_STEP_TURNS_PER_ADVANCE = 8
RETRY_STATUS = (429, 500, 502, 503, 504)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message[:300]}")
        self.status, self.message = status, message


def usd_from_usage(usage: dict | None) -> float:
    u = usage or {}
    return round(u.get("input_tokens", 0) * PRICE_IN + u.get("output_tokens", 0) * PRICE_OUT, 6)


class WonderClient:
    """POST /v1/responses with 3 tries and backoff on 429/5xx/network errors. Never logs the key."""

    def __init__(self, post: Callable | None = None, sleep: Callable[[float], None] = time.sleep, tries: int = 3):
        self._post, self._sleep, self.tries = post, sleep, tries

    def create(self, body: dict) -> dict:
        import requests
        from .. import llm
        post = self._post or requests.post
        for i in range(self.tries):
            try:
                r = post(llm.META + "/responses", headers=llm._hdr(), json=body, timeout=240)
            except requests.RequestException as e:
                if i < self.tries - 1:
                    self._sleep(2 ** i)
                    continue
                raise ApiError(0, f"network error {type(e).__name__}") from None
            if r.status_code == 200:
                return r.json()
            if r.status_code in RETRY_STATUS and i < self.tries - 1:
                self._sleep(2 ** i)
                continue
            raise ApiError(r.status_code, r.text)
        raise ApiError(0, "unreachable")


@dataclass
class LoopResult:
    status: str                      # asked | finished | text_only | cap | error
    driver: str = "wonder"
    turns: int = 0
    calls: int = 0
    usd: float = 0.0
    secs: float = 0.0
    tools: list[str] = field(default_factory=list)
    commentary: list[str] = field(default_factory=list)
    error: str | None = None


def _msg(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _text(item: dict) -> str:
    return "".join(c.get("text", "") for c in item.get("content", []) if isinstance(c, dict))


def _is_chain_error(e: ApiError) -> bool:
    if e.status not in (400, 404):
        return False
    return bool(re.search(r"previous_response|response.{0,30}(not found|expired|unknown|does not exist)|"
                          r"No function call found|not found", e.message, re.I)) or e.status == 404


def run_tool_loop(client, tb: Toolbox, step: str, user_text: str, *, max_turns: int = MAX_STEP_TURNS,
                  effort: str = "low", max_out: int = 8000, model: str = MODEL,
                  max_calls: int = MAX_CALLS_PER_TURN, system: str | None = None) -> LoopResult:
    """One step turn: tools of this step offered every turn; ends when ask_human/finish succeeds."""
    res = LoopResult("cap", "wonder")
    t0 = time.time()
    tools = tool_schemas(step, tb.look())
    system = system or prompts.system_prompt(tb.client_id)
    history: list[dict] = [_msg(user_text)]      # in-step transcript, for stateless replay
    next_input: list[dict] = list(history)
    prev: str | None = None
    stateless = False
    nudged = False

    def request(body: dict) -> dict:
        nonlocal prev, stateless
        try:
            return client.create(body)
        except ApiError as e:
            if "previous_response_id" in body and _is_chain_error(e):
                tb._ev("chain_fallback", {"step": step, "status": e.status}, "system")
                body = {k: v for k, v in body.items() if k != "previous_response_id"}
                body.update(input=_replay(history), store=False, include=["reasoning.encrypted_content"])
                stateless, prev = True, None
                return client.create(body)
            raise

    for turn in range(1, max_turns + 1):
        if tb.spent() >= run_cap_usd() * 1.2:
            res.status, res.error = "error", "cost_cap"
            break
        body: dict[str, Any] = {"model": model, "instructions": system, "tools": tools, "input": next_input,
                                "reasoning": {"effort": effort}, "max_output_tokens": max_out}
        if prev and not stateless:
            body["previous_response_id"] = prev   # the API rejects `include` together with previous_response_id
        else:
            body["include"] = ["reasoning.encrypted_content"]
            body["input"] = _replay(history)
            if stateless:
                body["store"] = False
        t1 = time.time()
        try:
            j = request(body)
            if j.get("status") == "incomplete" and not any(o.get("type") == "function_call" for o in j.get("output", [])):
                body["max_output_tokens"] = max_out * 2
                j2 = request(body)
                j = j2
        except ApiError as e:
            res.status, res.error = "error", f"api_{e.status}: {e.message[:160]}"
            break
        res.turns += 1
        usage = j.get("usage") or {}
        usd = usd_from_usage(usage)
        res.usd += usd
        if usd:
            events.record_cost(tb.run_id, "muse-wonder", usd)
        output = j.get("output") or []
        calls = [o for o in output if o.get("type") == "function_call"]
        for o in output:
            if o.get("type") == "message":
                t = _text(o).strip()
                if t:
                    res.commentary.append(t)
                    tb._ev("commentary", {"text": t[:600], "phase": o.get("phase") or "final"})
        tb._ev("wonder_turn", {"step": step, "turn": turn, "secs": round(time.time() - t1, 2), "usd": usd,
                               "calls": [c.get("name") for c in calls], "status": j.get("status"),
                               "in": usage.get("input_tokens"), "out": usage.get("output_tokens")}, "system")
        if stateless:
            prev = None
        elif j.get("id"):
            prev = j["id"]
        history += [o for o in output if o.get("type") != "reasoning" or o.get("encrypted_content")]
        if not calls:
            if not nudged:
                nudged = True
                next_input = [_msg(prompts.NUDGE)]
                history += next_input
                continue
            res.status, res.error = "text_only", "model replied without a tool call twice"
            break
        outs, asked, finished = [], False, False
        batch = calls[:max_calls]
        has_ask = any(c.get("name") == "ask_human" for c in batch)
        for idx, c in enumerate(calls):
            cid = c.get("call_id") or c.get("id")
            if idx >= max_calls:
                out = json.dumps({"ok": False, "error": {"code": "too_many_calls", "message": "max 14 tool calls per turn"}})
            elif asked or finished:
                out = json.dumps({"ok": False, "error": {"code": "not_executed", "message": "not executed: wait for the human"}})
            else:
                try:
                    args = json.loads(c.get("arguments") or "{}")
                except ValueError as e:
                    args = None
                    out = json.dumps({"ok": False, "error": {"code": "bad_arguments", "retryable": True,
                                                              "message": f"arguments are not valid JSON: {e}"}})
                if args is not None:
                    r = tb.call(c.get("name", ""), args, call_id=cid, batch_has_ask=has_ask)
                    out = r.out()
                    res.calls += 1
                    res.tools.append(c.get("name", ""))
                    asked, finished = asked or r.asked, finished or r.finished
            outs.append({"type": "function_call_output", "call_id": cid, "output": out})
        if asked or finished:
            res.status = "asked" if asked else "finished"
            break
        next_input = outs
        history += outs
    res.secs = round(time.time() - t0, 2)
    return res


def _replay(history: list[dict]) -> list[dict]:
    return list(history)


# ------------------------------------------------------------------ context
def artifact_summaries(tb: Toolbox, st: dict) -> dict:
    out: dict[str, Any] = {}
    stale = st["stale_artifacts"]
    if tb._art_ok(st, "concepts"):
        out["concepts"] = tb._concept_summaries(tb.concepts_list())
    if st["step_approval"].get("ideas"):
        out["chosen_concept"] = tb.selected_concept_id(st)
        try:
            wc = tb.working_concept(st)
            out["working_concept"] = {"id": wc["id"], "hook": wc.get("hook"), "cta": wc.get("cta")}
        except Exception:
            pass
    if tb._art_ok(st, "frames"):
        fr = tb._read("frames.json") or {}
        out["frames"] = len(fr.get("files", []))
        out["frames_kind"] = fr.get("kind") or "storyboard"
    out["look"] = tb.look()
    pr = tb.plan_rec() if tb.look() == "composed" else None
    if pr:
        from .. import compose_plan
        out["plan"] = {"id": pr["id"], "preset": pr["plan"].get("preset"), "current": tb.plan_current(st) is not None,
                       "summary": compose_plan.plan_summary(pr["plan"])[:8], "human_locks": tb.plan_locks()}
    if tb._art_ok(st, "cuts"):
        m = tb._read("meta.json") or {}
        q = m.get("qc") or {}
        out["cuts"] = {"formats": list(m.get("cuts", {})), "rounds": m.get("rounds"), "look": m.get("look"),
                       "qc": {"pass": q.get("pass"), "score": q.get("score"), "issues": q.get("issues", [])[:4],
                              "summary": q.get("summary")}}
    if stale:
        out["stale"] = stale
    return out


def build_context(tb: Toolbox, step: str, st: dict) -> dict:
    return {"step": step, "brief": tb.full_brief(st), "memory": tb.memory_pack(),
            "artifacts": artifact_summaries(tb, st), "steers": [s["text"] for s in st["steers"] if s["text"]],
            "last_answer": tb.last_answer(), "approvals": sorted(st["step_approval"], key=events.step_index)}


# ------------------------------------------------------------------ drivers
class WonderDriver:
    name = "wonder"

    def __init__(self, client: WonderClient | None = None, model: str = MODEL, effort: str = "low",
                 max_out: int = 8000):
        self.client, self.model, self.effort, self.max_out = client or WonderClient(), model, effort, max_out

    def step_turn(self, tb: Toolbox, ctx: dict) -> LoopResult:
        user = prompts.user_message(client_name=tb.client_id, step=ctx["step"], brief=ctx["brief"],
                                    memory=ctx["memory"], artifacts=ctx["artifacts"], steers=ctx["steers"],
                                    last_answer=ctx["last_answer"], approvals=ctx["approvals"])
        return run_tool_loop(self.client, tb, ctx["step"], user, effort=self.effort, max_out=self.max_out,
                             model=self.model)


class ScriptedDriver:
    """Fixed table step -> tool calls, derived from the event log so it can finish a half-done Wonder turn.
    Uses plain Spark JSON calls only for steer interpretation and memory-proposal wording."""
    name = "scripted"

    def step_turn(self, tb: Toolbox, ctx: dict) -> LoopResult:
        res = LoopResult("error", "scripted")
        t0 = time.time()
        for n in range(12):
            st, evs = tb.state(), events.list_events(tb.run_id)
            step = tb.current_step(st)
            name, args = self._next(tb, st, evs, step)
            r = tb.call(name, args, call_id=f"scripted-{n}")
            if not r.ok and r.error and r.error.get("retryable") and name != "ask_human":
                r = tb.call(name, args, call_id=f"scripted-{n}r")
            res.calls += 1
            res.tools.append(name)
            if not r.ok:
                res.error = f"{name}: {r.error['code']}: {r.error['message'][:160]}"
                break
            if r.asked or r.finished:
                res.status = "asked" if r.asked else "finished"
                break
        else:
            res.error = "scripted driver made no progress"
        res.secs = round(time.time() - t0, 2)
        return res

    # -- helpers
    @staticmethod
    def _last_steer(evs: list[dict], step: str) -> tuple[int | None, str]:
        sid, text = None, ""
        for e in evs:
            if e["kind"] == "steer" and e["payload"].get("step") == step:
                sid, text = e["id"], e["payload"].get("text") or ""
        return sid, text

    @staticmethod
    def _newer(evs: list[dict], kind: str, sid: int | None, artifact: str | None = None) -> bool:
        return any(e["kind"] == kind and e["id"] > (sid or 0) and (artifact is None or e["payload"].get("artifact") == artifact)
                   for e in evs)

    def _interpret(self, tb: Toolbox, step: str, text: str, concepts: list[dict]) -> dict:
        try:
            out = tb.b.interpret_steer(step, text, concepts)
            guid = (out.get("guidance") or "").strip()
            return {"concept_id": out.get("concept_id"), "guidance": f"{text}\n(Interpreted: {guid})" if guid else text}
        except Exception:
            return {"concept_id": None, "guidance": text}


    # -- composed look
    def _next_preview_composed(self, tb: Toolbox, st: dict, evs: list[dict], sid, text: str, refs, d) -> tuple[str, dict]:
        plan_ok = tb.plan_current(st) is not None
        steer_pending = sid is not None and not self._newer(evs, "artifact_proposed", sid, "plan")
        if not plan_ok or steer_pending:
            return "plan_composition", ({"guidance": text, **d} if text else dict(d))
        fr = tb._read("frames.json") or {}
        frames_ok = tb._art_ok(st, "frames") and (fr.get("kind") != "composed" or fr.get("plan_id") == tb.plan_rec()["id"])
        if not frames_ok:
            return "preview_composition", dict(d)
        return "ask_human", {"summary": "Composed look preview: the real speaker cut out over the planned backgrounds "
                                        "with the headline, callout, captions and CTA.", "memory_refs": refs, **d}

    def _next_edit_composed(self, tb: Toolbox, st: dict, evs: list[dict], sid, text: str, refs, d) -> tuple[str, dict]:
        meta = tb._read("meta.json") or {}
        cuts_ok = tb._art_ok(st, "cuts")
        cuts_new = sid is None or self._newer(evs, "artifact_proposed", sid, "cuts")
        if sid is not None and not cuts_new:
            if compose_tools.is_look_steer(text):
                if not self._newer(evs, "artifact_proposed", sid, "edit_plan"):
                    return "plan_composition", {"guidance": text, **d}
            elif not self._newer(evs, "concept_revised", sid):
                it = self._interpret(tb, "edit", text, [])
                return "revise_edit", {"steer": it["guidance"], **d}
        pending = meta.get("look") == "pending_compose"
        if not cuts_ok or not cuts_new or pending:
            return ("render_composition" if tb.master_fresh(st) else "build_cuts"), dict(d)
        q = meta.get("qc") or {}
        return "ask_human", {"summary": f"Composed cut ({meta.get('look')}). QC: {q.get('summary') or 'no verdict'} "
                                        f"(score {q.get('score')}).", "memory_refs": refs, **d}

    def _next(self, tb: Toolbox, st: dict, evs: list[dict], step: str) -> tuple[str, dict]:
        refs = [m["id"] for m in tb.approved_memory() if m["kind"] == "hard_rule"][:4]
        sid, text = self._last_steer(evs, step)
        d = {"decision": "scripted step table"}
        if step == "brief":
            base = " ".join((tb.b.load_brief() or "").split())[:700]
            summary = f"Brief as I understand it: {base}" + (f" Human steer applied: {text}" if text else "")
            return "ask_human", {"summary": summary, "memory_refs": refs, **d}
        if step == "ideas":
            have = tb._art_ok(st, "concepts")
            pending = sid is not None and not self._newer(evs, "artifact_proposed", sid, "concepts")
            if not have:
                return "write_concepts", {"memory_ids_used": refs, **d}
            if pending:
                cs = tb.concepts_list()
                it = self._interpret(tb, step, text, cs)
                if it["concept_id"] and any(c["id"] == it["concept_id"] for c in cs):
                    return "revise_concept", {"concept_id": it["concept_id"], "steer": it["guidance"], **d}
                return "write_concepts", {"guidance": it["guidance"], "memory_ids_used": refs, **d}
            cs = tb.concepts_list()
            return "ask_human", {"summary": f"{len(cs)} concepts: " + "; ".join(f"{c['id']} {c.get('title')}" for c in cs),
                                 "recommendation": cs[0]["id"], "memory_refs": refs, **d}
        composed = tb.look() == "composed"
        if step == "preview" and composed:
            return self._next_preview_composed(tb, st, evs, sid, text, refs, d)
        if step == "preview":
            if not tb._art_ok(st, "frames") or (sid is not None and not self._newer(evs, "artifact_proposed", sid, "frames")):
                return "storyboard", ({"style_note": text, **d} if text else dict(d))
            return "ask_human", {"summary": "Preview frames (AI stand-ins) for the approved concept.",
                                 "memory_refs": refs, **d}
        if step in ("edit", "check") and composed:
            return self._next_edit_composed(tb, st, evs, sid, text, refs, d)
        if step in ("edit", "check"):
            cuts_ok = tb._art_ok(st, "cuts")
            cuts_new = sid is None or self._newer(evs, "artifact_proposed", sid, "cuts")
            revised = sid is not None and self._newer(evs, "concept_revised", sid)
            if sid is not None and not cuts_new and not revised:
                it = self._interpret(tb, step, text, [])
                return "revise_edit", {"steer": it["guidance"], **d}
            if not cuts_ok or not cuts_new:
                return "build_cuts", dict(d)
            q = (tb._read("meta.json") or {}).get("qc") or {}
            return "ask_human", {"summary": f"QC: {q.get('summary') or 'no verdict'} (score {q.get('score')}).",
                                 "memory_refs": refs, **d}
        if step == "ship":
            if not any(e["kind"] == "scripted_proposals" for e in evs):
                steers = [s["text"] for s in st["steers"] if s["text"]]
                tb._ev("scripted_proposals", {"n": len(steers)}, "system")
                if steers:
                    try:
                        for rule in tb.b.propose_rules(steers)[:2]:
                            tb.call("propose_memory", {"kind": rule.get("kind", "preference"), "text": rule["text"],
                                                       "human_quote": steers[0][:240]})
                    except Exception:
                        pass
            return "ask_human", {"summary": "Package ready: three cuts, captions and the decision log.",
                                 "memory_refs": refs, **d}
        return "finish", {"summary": "Session complete; all steps approved by the human.", **d}


# ------------------------------------------------------------------ the loop
_LOCKS: dict[str, threading.Lock] = {}


class AgentLoop:
    """advance(run_id) -> {'status': 'awaiting_human'|'done'|'error'|'running', ...}. All state is in events."""

    def __init__(self, client: WonderClient | None = None, backends: Backends | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.client = client or WonderClient(sleep=sleep)
        self.backends = backends or Backends()
        self.scripted = ScriptedDriver()

    def _wonder(self) -> WonderDriver:
        return WonderDriver(self.client, model=os.environ.get("STUDIO_MODEL") or MODEL)

    def advance(self, run_id: str) -> dict:
        lock = _LOCKS.setdefault(run_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return {"status": "running", "run_id": run_id, "error": "another advance is in progress"}
        try:
            return self._advance(run_id)
        finally:
            lock.release()

    def _status(self, run_id: str, status: str, **extra: Any) -> dict:
        st = events.rebuild_run_state(run_id)
        return {"status": status, "run_id": run_id, "cost_usd": st["cost_usd"], **extra}

    def _advance(self, run_id: str) -> dict:
        mode = (os.environ.get("STUDIO_AGENT") or "wonder").lower()
        if mode == "deterministic":
            mode = "scripted"
        tb = Toolbox(run_id, self.backends)
        if not any(e["kind"] == "run_mode" for e in events.list_events(run_id)):
            tb._ev("run_mode", {"mode": mode}, "system")
        agg = {"step_turns": 0, "wonder_turns": 0, "tool_calls": 0, "fallbacks": 0}
        for _ in range(MAX_STEP_TURNS_PER_ADVANCE):
            st = tb.state()
            last = next((e["payload"]["status"] for e in reversed(events.list_events(run_id))
                         if e["kind"] == "run_status"), None)
            if last in ("done", "cancelled"):
                return self._status(run_id, last, **agg)
            if st["pending_gate"]:
                events.append_event(run_id, "run_status", "system", {"status": "awaiting_human"})
                return self._status(run_id, "awaiting_human", gate_id=st["pending_gate"]["gate_id"],
                                    step=st["pending_gate"]["step"], **agg)
            events.append_event(run_id, "run_status", "system", {"status": "running"})
            step = tb.current_step(st)
            cost0 = st["cost_usd"]
            tb.consume_answer()
            ctx = build_context(tb, step, tb.state())
            res = None
            if mode == "wonder":
                try:
                    res = self._wonder().step_turn(tb, ctx)
                except Exception as e:  # any driver failure -> scripted for this turn
                    res = LoopResult("error", "wonder", error=f"{type(e).__name__}: {str(e)[:160]}")
                self._log_turn(tb, step, res, cost0)
                agg["wonder_turns"] += res.turns
                agg["tool_calls"] += res.calls
                if res.status not in ("asked", "finished"):
                    agg["fallbacks"] += 1
                    events.append_event(run_id, "fallback", "system", {
                        "step": step, "from": "wonder", "to": "scripted", "reason": res.status,
                        "detail": res.error, "mode": "scripted"})
                    res = None
            if res is None:
                res = self.scripted.step_turn(tb, ctx)
                self._log_turn(tb, step, res, cost0)
                agg["tool_calls"] += res.calls
            agg["step_turns"] += 1
            if res.status in ("asked", "finished"):
                continue
            events.append_event(run_id, "error", "system", {"message": res.error or res.status, "step": step})
            events.append_event(run_id, "run_status", "system", {"status": "error"})
            return self._status(run_id, "error", error=res.error or res.status, step=step, **agg)
        events.append_event(run_id, "run_status", "system", {"status": "error"})
        return self._status(run_id, "error", error="too many step turns in one advance", **agg)

    @staticmethod
    def _log_turn(tb: Toolbox, step: str, res: LoopResult, cost0: float) -> None:
        usd = round(tb.state()["cost_usd"] - cost0, 5)   # model + tool spend during this turn
        tb._ev("step_turn", {"step": step, "driver": res.driver, "status": res.status, "turns": res.turns,
                             "calls": res.calls, "tools": res.tools, "usd": usd, "wonder_usd": round(res.usd, 5),
                             "secs": res.secs, "error": res.error}, "system")

"""Studio tools: the single place where tools run and gates are enforced.

Wonder (or the ScriptedDriver) only ever *requests* a tool; this module decides whether it may
run. Gates come from `studio.gates` (event-sourced): a paid tool runs only if the matching step
was approved AFTER the latest relevant proposal. Every blocked attempt is logged as a
`gate_blocked_attempt` event (a model trying to skip a gate is evidence for the demo).

Tool results are compact JSON strings (artifact names + short summaries, never media or transcripts).
Cards for the human are built HERE from artifacts (docs/API_CONTRACT.md); the model only supplies
`summary`, `memory_refs` and an optional `recommendation` to ask_human.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

from .. import trace
from ..studio import events, gates, memory
from ..studio.events import STEPS, step_index

SPARK_CALL_USD = 0.01          # flat estimate per successful Spark call (token prices unverified)
MAX_RESULT_CHARS = 4000
MAX_PROPOSALS = 3
MEMORY_ALL_IF_AT_MOST = 15
EST = {"write_concepts": 0.03, "revise_concept": 0.01, "revise_edit": 0.01, "storyboard": 0.04,
       "build_cuts": 0.20, "review_cut": 0.03, "track_subject": 0.011}
NEXT_COST = {"brief": 0.03, "ideas": 0.04, "preview": 0.20, "edit": 0.03, "check": 0.0, "ship": 0.0}
RATIOS = ("9x16", "1x1", "16x9")

STEP_TOOLS: dict[str, list[str]] = {
    "brief": ["read_memory", "ask_human"],
    "ideas": ["read_memory", "write_concepts", "revise_concept", "propose_memory", "ask_human"],
    "preview": ["read_memory", "storyboard", "ask_human"],
    "edit": ["read_memory", "build_cuts", "track_subject", "revise_edit", "ask_human"],
    "check": ["read_memory", "review_cut", "revise_edit", "build_cuts", "ask_human"],
    "ship": ["read_memory", "propose_memory", "ask_human"],
    "done": ["propose_memory", "finish"],
}

_DEC = {"type": "string", "description": "One sentence: why you are calling this tool now."}
SPECS: dict[str, tuple[str, dict]] = {
    "read_memory": ("Read the client's approved memory (hard rules first). Free.",
                    {"type": "object", "properties": {"decision": _DEC}}),
    "propose_memory": (
        "Propose ONE new client-memory item for the human to accept or reject (never applied silently). "
        "At most 3 per run. Free.",
        {"type": "object", "required": ["kind", "text"], "properties": {
            "kind": {"enum": list(memory.KINDS)},
            "text": {"type": "string", "description": "One atomic sentence, 8-280 characters."},
            "human_quote": {"type": "string", "description": "The human's own words that justify it."},
            "decision": _DEC}}),
    "ask_human": (
        "Present the current step to the human and STOP. The card (concepts, frames, cuts, QC) is built by "
        "the system from the artifacts; you supply only the summary. Ends your turn; the answer arrives later.",
        {"type": "object", "required": ["summary"], "properties": {
            "summary": {"type": "string", "description": "Brief step: your restatement of the brief (3-6 "
                        "sentences, no invented facts). Other steps: 1-2 sentences on what you did and why."},
            "memory_refs": {"type": "array", "items": {"type": "string"},
                            "description": "Memory ids that shaped this step."},
            "recommendation": {"type": "string", "description": "Ideas step: the concept id you recommend."},
            "decision": _DEC}}),
    "write_concepts": (
        "Write 3 new ad concepts from the brief, footage and client memory. Cheap. Pass the human's steer "
        "verbatim in guidance; every steer so far is also appended by the system.",
        {"type": "object", "properties": {
            "guidance": {"type": "string"},
            "memory_ids_used": {"type": "array", "items": {"type": "string"}}, "decision": _DEC}}),
    "revise_concept": (
        "Rewrite ONE listed concept after a steer (Ideas step). Cheap. Pass the steer verbatim.",
        {"type": "object", "required": ["concept_id", "steer"], "properties": {
            "concept_id": {"type": "string"}, "steer": {"type": "string"}, "decision": _DEC}}),
    "storyboard": (
        "PAID. Draw one preview frame per beat of the approved concept. Needs the Ideas approval.",
        {"type": "object", "properties": {"style_note": {"type": "string"}, "decision": _DEC}}),
    "build_cuts": (
        "PAID. Cut the three ratios, track the speaker (SAM), run QC and auto-fix up to max_rounds, "
        "delivering the best round. Needs the Preview approval.",
        {"type": "object", "properties": {
            "max_rounds": {"type": "integer", "minimum": 1, "maximum": 3},
            "subject_prompt": {"type": "string"}, "decision": _DEC}}),
    "track_subject": (
        "PAID. Track the speaker on the latest master cut and report the crop stats. Needs the Preview approval.",
        {"type": "object", "properties": {"prompt": {"type": "string"}, "decision": _DEC}}),
    "review_cut": ("Run Wonder QC again on the current 9:16 cut. Cheap.",
                   {"type": "object", "properties": {"decision": _DEC}}),
    "revise_edit": (
        "Rewrite the concept being cut after a steer or QC fixes (Edit/Check steps), then call build_cuts. "
        "Cheap. Pass the steer verbatim.",
        {"type": "object", "required": ["steer"], "properties": {
            "steer": {"type": "string"},
            "structured_changes": {"type": "array", "items": {"type": "string"}}, "decision": _DEC}}),
    "finish": ("End the session after the human approved Ship.",
               {"type": "object", "required": ["summary"], "properties": {
                   "summary": {"type": "string"}, "decision": _DEC}}),
}
_VALIDATORS = {n: Draft202012Validator(p) for n, (_, p) in SPECS.items()}


def tool_schemas(step: str) -> list[dict]:
    """Flat Responses-API function tools allowed in `step`."""
    return [{"type": "function", "name": n, "description": SPECS[n][0], "parameters": SPECS[n][1]}
            for n in STEP_TOOLS[step]]


# ------------------------------------------------------------------ config
def out_dir() -> Path:
    return Path(os.environ.get("ADFARMING_OUT") or "out")


def run_cap_usd() -> float:
    return float(os.environ.get("STUDIO_RUN_CAP_USD") or 1.5)


def artifact_url(run_id: str, name: str) -> str:
    return f"/api/runs/{run_id}/artifacts/{name}"


# ------------------------------------------------------------------ backends (injectable for tests)
def _d_load_brief() -> str:
    from ..footage import PRIV
    f = PRIV / "brief.md"
    return f.read_text() if f.exists() else ""


def _d_load_subject() -> str:
    from ..footage import PRIV
    f = PRIV / "subject.txt"
    return f.read_text().strip() if f.exists() else "an Indian woman in a saree"


def _d_load_refs() -> str:
    from ..footage import PRIV
    f = PRIV / "refs.md"
    return f.read_text() if f.exists() else ""


def _d_load_clips():
    from ..footage import load_clips
    return load_clips()


def _d_generate(*a, **k):
    from .. import concepts
    return concepts.generate(*a, **k)


def _d_revise(*a, **k):
    from .. import concepts
    return concepts.revise(*a, **k)


def _d_frames(*a, **k):
    from .. import storyboard
    return storyboard.frames(*a, **k)


def _d_build(*a, **k):
    from .. import cuts
    return cuts.build(*a, **k)


def _d_track(*a, **k):
    from .. import sam
    return sam.track(*a, **k)


def _d_review(*a, **k):
    from .. import qc
    return qc.review(*a, **k)


def _d_interpret_steer(step: str, text: str, concepts: list[dict]) -> dict:
    """Plain Spark JSON call: which concept does the steer target, and what is the clean instruction?"""
    from ..llm import spark_json
    ids = [c["id"] for c in concepts]
    out, _ = spark_json(
        "You route a creative director's feedback on ad concepts. Return ONLY JSON.",
        f"Step: {step}. Concept ids: {ids}.\nFEEDBACK: {text}\n"
        'Return {"concept_id": "<one id if the feedback is about ONE concept, else null>", '
        '"guidance": "<the feedback restated as one clear editing instruction, keeping every constraint>"}',
        effort="low", max_out=2000)
    return out


def _d_propose_rules(steers: list[str]) -> list[dict]:
    """One plain Spark call: turn steers into at most 2 reusable client rules (or none)."""
    from ..llm import spark_json
    out, _ = spark_json(
        "You write reusable creative rules for an ad client's memory. Return ONLY JSON.",
        "Human feedback given during one ad session:\n- " + "\n- ".join(steers) +
        '\nReturn {"rules":[{"kind":"preference|rejection|hard_rule","text":"one atomic sentence, 8-200 chars"}]} '
        "with at most 2 rules that would still make sense in a future session, or an empty list.",
        effort="low", max_out=2000)
    return out.get("rules", [])


_DEFAULTS: dict[str, Callable] = {
    "load_brief": _d_load_brief, "load_subject": _d_load_subject, "load_refs": _d_load_refs,
    "load_clips": _d_load_clips, "generate": _d_generate, "revise": _d_revise, "frames": _d_frames,
    "build": _d_build, "track": _d_track, "review": _d_review,
    "interpret_steer": _d_interpret_steer, "propose_rules": _d_propose_rules,
}


class Backends:
    """Everything that touches footage/models. Override any name with a keyword for tests."""

    def __init__(self, **overrides: Callable) -> None:
        bad = set(overrides) - set(_DEFAULTS)
        if bad:
            raise TypeError(f"unknown backends {sorted(bad)}")
        self._over = overrides

    def __getattr__(self, name: str) -> Callable:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._over:
            return self._over[name]
        if name in _DEFAULTS:
            return _DEFAULTS[name]
        raise AttributeError(name)


# ------------------------------------------------------------------ results
@dataclass
class ToolResult:
    ok: bool
    data: dict = field(default_factory=dict)
    error: dict | None = None
    asked: bool = False       # ask_human created a gate: end the step turn
    finished: bool = False

    def out(self) -> str:
        body = {"ok": True, **self.data} if self.ok else {"ok": False, "error": self.error}
        s = json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(s) > MAX_RESULT_CHARS:
            s = json.dumps({"ok": self.ok, "truncated": True, "preview": s[:MAX_RESULT_CHARS - 200]},
                           ensure_ascii=False, separators=(",", ":"))
        return s


def err(code: str, message: str, retryable: bool = False, hint: str = "") -> ToolResult:
    return ToolResult(False, error={"code": code, "message": message, "retryable": retryable, "hint": hint})


class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, hint: str = ""):
        super().__init__(message)
        self.res = err(code, message, retryable, hint)


def _trim(obj: Any, n: int = 600) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= n else obj[:n] + "..."
    if isinstance(obj, dict):
        return {k: _trim(v, n) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_trim(v, n) for v in obj[:30]]
    return obj


def best_of_rounds(run, concept: dict, clips: list[dict], brief: str, *, build, review, revise, track,
                   max_rounds: int = 3, budget_ok: Callable[[], bool] = lambda: True) -> dict:
    """Build -> QC -> auto-revise, delivering the BEST round (not the last).

    Extracted from scripts/run_pipeline.py (same logic: stop on pass, on unavailable QC, on no score
    improvement, or when out of rounds); additionally stops when `budget_ok()` is false."""
    history, rounds = [], []
    for attempt in range(1, max_rounds + 1):
        rmeta = copy.deepcopy(build(run, concept, clips, track=track))
        verdict = review(run, brief, concept, rmeta["captions"], run.dir / rmeta["cuts"]["9x16"])
        rdir = run.dir / f"round{attempt}"
        rdir.mkdir(exist_ok=True)
        for f in [*rmeta["cuts"].values(), "qc_0.jpg", "qc_1.jpg", "qc_2.jpg", "qc_3.jpg"]:
            if (run.dir / f).exists():
                shutil.copy2(run.dir / f, rdir / f)
        if rmeta.get("master") and Path(rmeta["master"]).exists():
            shutil.copy2(rmeta["master"], rdir / "master_hold.mp4")
        score = verdict.get("score") if isinstance(verdict.get("score"), (int, float)) else None
        history.append({"attempt": attempt, "score": score, "pass": verdict.get("pass") is True,
                        "summary": verdict.get("summary")})
        rounds.append({"meta": rmeta, "verdict": verdict, "concept": copy.deepcopy(concept), "score": score})
        if verdict.get("pass") is True or score is None or attempt == max_rounds:
            break
        prev = rounds[-2]["score"] if len(rounds) > 1 else None
        if prev is not None and score <= prev:
            break
        if not budget_ok():
            break
        concept = revise(run, brief, concept, verdict, clips)
    bi = max(range(len(rounds)), key=lambda i: (rounds[i]["verdict"].get("pass") is True,
                                                rounds[i]["score"] if rounds[i]["score"] is not None else -1))
    best = rounds[bi]
    for f in best["meta"]["cuts"].values():
        src = run.dir / f"round{bi + 1}" / f
        if src.exists():
            shutil.copy2(src, run.dir / f)
    for i in range(4):
        src = run.dir / f"round{bi + 1}" / f"qc_{i}.jpg"
        if src.exists():
            shutil.copy2(src, run.dir / f"qc_{i}.jpg")
    return {"meta": best["meta"], "verdict": best["verdict"], "history": history, "best_round": bi + 1,
            "rounds": len(rounds), "concept": best["concept"]}


# ------------------------------------------------------------------ the toolbox
class Toolbox:
    def __init__(self, run_id: str, backends: Backends | None = None, out: str | Path | None = None):
        self.run_id = run_id
        self.b = backends or Backends()
        st = events.rebuild_run_state(run_id)
        if not st["client_id"]:
            raise ValueError(f"unknown run {run_id!r}")
        self.client_id = st["client_id"]
        self.run = trace.Run(run_id, Path(out) if out else out_dir())
        self.dir: Path = self.run.dir
        self._clips: list[dict] | None = None

    # ---------------- state helpers
    def state(self) -> dict:
        return events.rebuild_run_state(self.run_id)

    def current_step(self, st: dict | None = None) -> str:
        st = st or self.state()
        for s in STEPS:
            if s not in st["step_approval"]:
                return s
        return "done"

    def spent(self) -> float:
        return float(self.state()["cost_usd"])

    def clips(self) -> list[dict]:
        if self._clips is None:
            self._clips = self.b.load_clips()
        return self._clips

    def _ev(self, kind: str, payload: dict, actor: str = "agent", reason: str | None = None) -> int:
        return events.append_event(self.run_id, kind, actor, payload, reason)

    def _write(self, name: str, obj: Any) -> None:
        (self.dir / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2))

    def _read(self, name: str, default: Any = None) -> Any:
        f = self.dir / name
        return json.loads(f.read_text()) if f.exists() else default

    def _art_ok(self, st: dict, name: str) -> bool:
        a = st["artifacts"].get(name)
        return bool(a) and a["status"] != "stale"

    def approved_memory(self) -> list[dict]:
        return memory.list_memory(self.client_id, "approved")

    def memory_pack(self) -> list[dict]:
        """All approved items if <= 15, else hard rules/pinned first within the char budget. Ids are logged by code."""
        items = memory.list_memory(self.client_id, "approved")
        if len(items) <= MEMORY_ALL_IF_AT_MOST:
            items.sort(key=lambda i: (i["kind"] != "hard_rule", not i["pinned"], i["id"]))
            self._ev("memory_read", {"client_id": self.client_id, "ids": [i["id"] for i in items],
                                     "chars": sum(len(i["text"]) for i in items)}, "system")
            return items
        return memory.read_memory(self.client_id, run_id=self.run_id)

    def full_brief(self, st: dict | None = None) -> str:
        st = st or self.state()
        base = (self.b.load_brief() or "").strip()
        goal = (st.get("brief") or "").strip()
        parts = [base] if base else []
        if goal and goal not in base:
            parts.append(f"SESSION GOAL: {goal}")
        bt = self._last_brief_text()
        if bt:
            parts.append(f"CONFIRMED BRIEF (restated by the agent, reviewed by the human):\n{bt}")
        return "\n\n".join(parts)

    def _last_brief_text(self) -> str:
        for e in reversed(events.list_events(self.run_id)):
            if e["kind"] == "brief_text":
                return e["payload"]["text"]
        return ""

    def _memory_block(self, hard_only: bool = False) -> str:
        items = self.approved_memory()
        if hard_only:
            items = [i for i in items if i["kind"] == "hard_rule"]
        return "\n".join(f"- [{i['id']}] ({i['kind']}) {i['text']}" for i in items)

    def _trace_mark(self) -> int:
        f = self.dir / "trace.jsonl"
        return sum(1 for _ in f.open()) if f.exists() else 0

    def _cost_since(self, mark: int) -> float:
        f = self.dir / "trace.jsonl"
        if not f.exists():
            return 0.0
        usd = 0.0
        for line in f.read_text().splitlines()[mark:]:
            r = json.loads(line)
            if "cost_usd" in r:
                usd += float(r["cost_usd"])
            elif r.get("tool") == "muse-spark" and r.get("status") == "ok":
                usd += SPARK_CALL_USD
        return round(usd, 5)

    # ---------------- concepts (ideas) helpers
    def concepts_list(self) -> list[dict]:
        return self._read("concepts.json", [])

    def _save_concepts(self, cs: list[dict], st: dict | None = None, actor: str = "agent") -> int:
        n = len(list(self.dir.glob("concepts.v*.json"))) + 1
        self._write(f"concepts.v{n}.json", cs)
        self._write("concepts.json", cs)
        gates.propose_artifact(self.run_id, "ideas", "concepts", ref=f"concepts.v{n}.json", actor=actor)
        return n

    def selected_concept_id(self, st: dict | None = None) -> str | None:
        st = st or self.state()
        ap = st["step_approval"].get("ideas")
        if not ap:
            return None
        ans = gates.get_gate(ap["gate_id"])["answer"] or {}
        cid = (ans.get("payload") or {}).get("choice")
        cs = self.concepts_list()
        if cid and any(c["id"] == cid for c in cs):
            return cid
        card = self._gate_card(ap["gate_id"]) or {}
        rec = card.get("recommended")
        if rec and any(c["id"] == rec for c in cs):
            return rec
        return cs[0]["id"] if cs else None

    def _gate_card(self, gate_id: int) -> dict | None:
        for e in events.list_events(self.run_id):
            if e["kind"] == "gate_requested" and e["payload"].get("gate_id") == gate_id:
                return (e["payload"].get("payload") or {}).get("card")
        return None

    def working_concept(self, st: dict | None = None) -> dict:
        st = st or self.state()
        ap = st["step_approval"].get("ideas")
        if not ap:
            raise ToolError("missing_artifact", "no concept has been approved yet",
                            hint="the human must approve the Ideas step first")
        d = self._read("working_concept.json")
        if d and d.get("from_gate") == ap["gate_id"]:
            return d["concept"]
        cid = self.selected_concept_id(st)
        c = copy.deepcopy(next(x for x in self.concepts_list() if x["id"] == cid))
        self._write("working_concept.json", {"from_gate": ap["gate_id"], "concept": c})
        return c

    def _save_working(self, st: dict, concept: dict) -> None:
        self._write("working_concept.json",
                    {"from_gate": st["step_approval"]["ideas"]["gate_id"], "concept": concept})

    def _lock_fields(self, cid: str, fields: dict) -> None:
        """Remember fields a human edited so no automatic revision can overwrite them."""
        d = self._read("human_locks.json") or {}
        d.setdefault(cid, {}).update(fields)
        self._write("human_locks.json", d)

    def _freeze(self, concept: dict) -> dict:
        """Re-apply human-edited hook/cta/title to a concept (human edits always win)."""
        locks = (self._read("human_locks.json") or {}).get(concept.get("id"), {})
        for k, v in locks.items():
            concept[k] = v
        if "hook" in locks and concept.get("beats"):
            concept["beats"][0]["on_screen_text"] = locks["hook"]
        return concept

    def _stale(self, names_steps: list[tuple[str, str]]) -> None:
        st = self.state()
        for name, step in names_steps:
            a = st["artifacts"].get(name)
            if a and a["status"] != "stale":
                self._ev("artifact_stale", {"artifact": name, "step": step, "reason": "revised"}, "system")

    # ---------------- dispatch
    def call(self, name: str, args: Any, call_id: str | None = None, batch_has_ask: bool = False) -> ToolResult:
        """Never raises. Gate/allow-list/cap/validation enforced here."""
        if name not in SPECS:
            return err("unknown_tool", f"no tool named {name!r}", True, f"available: {sorted(SPECS)}")
        if not isinstance(args, dict):
            return err("bad_arguments", "arguments must be a JSON object", True)
        bad = next(iter(_VALIDATORS[name].iter_errors(args)), None)
        if bad is not None:
            where = "/".join(str(p) for p in bad.absolute_path) or "arguments"
            return err("validation", f"{where}: {bad.message}", True, f"schema: {json.dumps(SPECS[name][1])[:300]}")
        step = self.current_step()
        self._ev("tool_call", {"tool": name, "call_id": call_id, "step": step, "args": _trim(args)},
                 reason=str(args.get("decision") or "")[:300] or None)
        if name in gates.TOOL_GATES:
            if batch_has_ask:
                return self._blocked(name, call_id, "paid tool in the same batch as ask_human",
                                     "wait for the human's answer; paid tools never share a batch with ask_human")
            try:
                gates.assert_approved(self.run_id, name)
            except gates.GateBlocked as e:
                return self._blocked(name, call_id, str(e), f"call ask_human for the '{gates.TOOL_GATES[name]}' "
                                     "step and wait; do not retry around the gate")
        if name not in STEP_TOOLS[step]:
            if name in gates.TOOL_GATES:
                self._ev("gate_blocked_attempt", {"tool": name, "call_id": call_id, "reason": f"not allowed in step {step}"})
            return err("not_allowed_in_step", f"{name} is not available in step '{step}'", False,
                       f"allowed now: {STEP_TOOLS[step]}")
        est = EST.get(name, 0.0)
        if est and self.spent() + est > run_cap_usd() + 1e-9:
            return err("cost_cap", f"run cap ${run_cap_usd():.2f} would be exceeded "
                       f"(spent ${self.spent():.2f}, {name} ~${est:.2f})", False,
                       "stop spending: ask_human with what you have")
        self._ev("tool_started", {"tool": name, "call_id": call_id})
        mark, t0 = self._trace_mark(), time.time()
        try:
            res = getattr(self, f"_t_{name}")(args, call_id)
        except ToolError as e:
            res = e.res
        except gates.GateError as e:
            res = err("gate_error", str(e))
        except Exception as e:  # tool failures never kill the loop
            res = err("tool_failed", f"{type(e).__name__}: {str(e)[:200]}", True,
                      "retry once with the same arguments, otherwise ask_human")
        usd = self._cost_since(mark)
        if usd > 0:
            events.record_cost(self.run_id, name, usd)
        self._ev("tool_done", {"tool": name, "call_id": call_id, "ok": res.ok, "usd": usd,
                               "secs": round(time.time() - t0, 2),
                               **({"error": res.error["code"]} if res.error else {})})
        return res

    def _blocked(self, name: str, call_id: str | None, reason: str, hint: str) -> ToolResult:
        self._ev("gate_blocked_attempt", {"tool": name, "call_id": call_id, "reason": reason})
        return err("gate_blocked", reason, False, hint)

    # ---------------- tools
    def _t_read_memory(self, a: dict, call_id) -> ToolResult:
        items = self.memory_pack()
        return ToolResult(True, {"count": len(items), "items": [
            {"id": i["id"], "kind": i["kind"], "text": i["text"], "pinned": i["pinned"]} for i in items]})

    def _t_propose_memory(self, a: dict, call_id) -> ToolResult:
        n = sum(1 for e in events.list_events(self.run_id) if e["kind"] == "agent_memory_proposal")
        if n >= MAX_PROPOSALS:
            raise ToolError("limit", f"at most {MAX_PROPOSALS} memory proposals per run")
        text = a["text"].strip()
        if not 8 <= len(text) <= 280:
            raise ToolError("validation", "text must be 8-280 characters", True)
        quote = (a.get("human_quote") or "").strip()[:240] or None
        res = memory.propose_memory(self.client_id, a["kind"], text, evidence={
            "run_id": self.run_id, "human_quote": quote, "doc_ref": "agent_proposal"}, run_id=self.run_id)
        self._ev("agent_memory_proposal", {"text": text, **res})
        return ToolResult(True, res)

    def _t_write_concepts(self, a: dict, call_id) -> ToolResult:
        st = self.state()
        steers = [s["text"] for s in st["steers"] if s["step"] in ("brief", "ideas") and s["text"]]
        parts = [a["guidance"].strip()] if (a.get("guidance") or "").strip() else []
        parts += [f"Human steer (verbatim): {t}" for t in steers]
        guidance = "\n".join(parts)
        brief = self.full_brief(st)
        mem = self._memory_block()
        if mem:
            brief += "\n\nCLIENT MEMORY (approved; hard_rule items are absolute):\n" + mem
        cs = self.b.generate(self.run, brief, self.clips(), self.b.load_refs(), guidance=guidance)
        known = {i["id"] for i in self.approved_memory()}
        cites = [m for m in a.get("memory_ids_used", []) if m in known]
        for c in cs:
            c["cites"] = cites
        v = self._save_concepts(cs, st)
        return ToolResult(True, {"version": v, "concepts": self._concept_summaries(cs),
                                 "guidance_sent": guidance[:300]})

    def _t_revise_concept(self, a: dict, call_id) -> ToolResult:
        st = self.state()
        cs = self.concepts_list()
        old = next((c for c in cs if c["id"] == a["concept_id"]), None)
        if old is None:
            raise ToolError("validation", f"unknown concept {a['concept_id']!r}", True,
                            f"ids: {[c['id'] for c in cs]}")
        brief = self.full_brief(st)
        new = self.b.revise(self.run, brief, copy.deepcopy(old), {"issues": [], "fixes": []}, self.clips(),
                            guidance=a["steer"])
        new["cites"] = old.get("cites", [])
        cs2 = [new if c["id"] == old["id"] else c for c in cs]
        v = self._save_concepts(cs2, st)
        return ToolResult(True, {"version": v, "concept_id": old["id"], "hook_before": old.get("hook"),
                                 "hook_after": new.get("hook"), "cta_after": new.get("cta")})

    def _t_storyboard(self, a: dict, call_id) -> ToolResult:
        st = self.state()
        cid = self.selected_concept_id(st)
        concept = next((c for c in self.concepts_list() if c["id"] == cid), None)
        if concept is None:
            raise ToolError("missing_artifact", "no approved concept to storyboard")
        notes = [s["text"] for s in st["steers"] if s["step"] == "preview" and s["text"]]
        if a.get("style_note"):
            notes.append(a["style_note"])
        subject = self.b.load_subject() + (" Style notes from the human: " + "; ".join(notes) if notes else "")
        paths = self.b.frames(self.run, copy.deepcopy(concept), subject)
        names = []
        for i, p in enumerate(paths, 1):
            name = f"frame_{cid}_{i}{Path(p).suffix or '.webp'}"
            shutil.copy2(self.dir / p, self.dir / name)
            names.append(name)
        self._write("frames.json", {"concept_id": cid, "files": names})
        gates.propose_artifact(self.run_id, "preview", "frames", ref="frames.json")
        return ToolResult(True, {"concept_id": cid, "frames": names})

    def _safe_track(self, prompt: str):
        def track(m):
            try:
                return self.b.track(self.run, Path(m), prompt)
            except Exception as e:
                self.run.trace("sam-3.1", {}, {"error": str(e)[:100]}, status="error",
                               decision="SAM failed, fixed crop")
                return None
        return track

    def _t_build_cuts(self, a: dict, call_id) -> ToolResult:
        st = self.state()
        wc = self.working_concept(st)
        brief = (self.b.load_brief() or "").strip() or self.full_brief(st)
        hard = self._memory_block(hard_only=True)
        if hard:
            brief += "\n\nCLIENT HARD RULES (QC must verify):\n" + hard
        mark0 = self._trace_mark()
        res = best_of_rounds(
            self.run, self._freeze(wc), self.clips(), brief, build=self.b.build, review=self.b.review,
            revise=lambda *a, **k: self._freeze(self.b.revise(*a, **k)),
            track=self._safe_track(a.get("subject_prompt") or "woman"),
            max_rounds=int(a.get("max_rounds") or 3),
            budget_ok=lambda: self.spent() + self._cost_since(mark0) + 0.10 <= run_cap_usd())
        meta = {k: v for k, v in res["meta"].items() if k != "master"}
        meta.update(qc=res["verdict"], qc_history=res["history"], best_round=res["best_round"],
                    rounds=res["rounds"], concept_id=wc["id"])
        self._write("meta.json", meta)
        self._save_working(st, res["concept"])
        gates.propose_artifact(self.run_id, "edit", "cuts", ref="meta.json")
        gates.propose_artifact(self.run_id, "check", "qc", ref="meta.json")
        q = res["verdict"]
        return ToolResult(True, {"concept_id": wc["id"], "cuts": meta["cuts"], "rounds": res["rounds"],
                                 "best_round": res["best_round"],
                                 "qc": {"pass": q.get("pass"), "score": q.get("score"),
                                        "issues": q.get("issues", [])[:5], "summary": q.get("summary")}})

    def _t_track_subject(self, a: dict, call_id) -> ToolResult:
        master = self.dir / "work" / "master_hold.mp4"
        if not master.exists():
            raise ToolError("missing_artifact", "no master cut yet", False, "call build_cuts first")
        stats = self._safe_track(a.get("prompt") or "woman")(master) or {"frames": 0}
        return ToolResult(True, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in stats.items()
                                 if k != "boxes"})

    def _t_review_cut(self, a: dict, call_id) -> ToolResult:
        st = self.state()
        meta = self._read("meta.json")
        if not meta or not self._art_ok(st, "cuts"):
            raise ToolError("missing_artifact", "no current cut to review", False, "call build_cuts first")
        brief = (self.b.load_brief() or "").strip() or self.full_brief(st)
        v = self.b.review(self.run, brief, self.working_concept(st), meta["captions"],
                          self.dir / meta["cuts"]["9x16"])
        meta["qc"] = v
        meta["qc_history"] = meta.get("qc_history", []) + [{
            "attempt": len(meta.get("qc_history", [])) + 1, "score": v.get("score"),
            "pass": v.get("pass") is True, "summary": v.get("summary")}]
        self._write("meta.json", meta)
        gates.propose_artifact(self.run_id, "check", "qc", ref="meta.json")
        return ToolResult(True, {"pass": v.get("pass"), "score": v.get("score"),
                                 "issues": v.get("issues", [])[:5], "summary": v.get("summary")})

    def _t_revise_edit(self, a: dict, call_id) -> ToolResult:
        st = self.state()
        wc = self.working_concept(st)
        guidance = a["steer"] + ("\nStructured changes: " + "; ".join(a["structured_changes"])
                                 if a.get("structured_changes") else "")
        new = self.b.revise(self.run, self.full_brief(st), copy.deepcopy(wc), {"issues": [], "fixes": []},
                            self.clips(), guidance=guidance)
        new["cites"] = wc.get("cites", [])
        self._save_working(st, new)
        self._stale([("cuts", "edit"), ("qc", "check")])
        self._ev("concept_revised", {"concept_id": wc["id"], "steer": a["steer"][:300]})
        return ToolResult(True, {"concept_id": wc["id"], "hook_before": wc.get("hook"),
                                 "hook_after": new.get("hook"), "cta_after": new.get("cta"),
                                 "note": "cuts are stale: call build_cuts, then ask_human"})

    def _t_finish(self, a: dict, call_id) -> ToolResult:
        st = self.state()
        if "ship" not in st["step_approval"]:
            return self._blocked("finish", call_id, "the human has not approved Ship", "call ask_human for ship")
        self._ev("run_summary", {"summary": a["summary"][:600]})
        self._ev("run_status", {"status": "done"}, "system")
        return ToolResult(True, {"done": True}, finished=True)

    # ---------------- ask_human + cards (built by code)
    REQUIRED_ARTIFACT = {"ideas": "concepts", "preview": "frames", "edit": "cuts", "check": "cuts"}

    def _t_ask_human(self, a: dict, call_id) -> ToolResult:
        st = self.state()
        step = self.current_step(st)
        if step == "done":
            raise ToolError("nothing_to_ask", "every step is approved; call finish")
        if st["pending_gate"]:
            raise ToolError("already_pending", "a gate is already waiting for the human")
        evs = events.list_events(self.run_id)
        self._check_steer_applied(step, evs)
        summary = a["summary"].strip()
        refs = [m for m in a.get("memory_refs", []) if m in {i["id"] for i in self.approved_memory()}]
        card = self._card(step, st, summary, refs, a.get("recommendation"))
        if step == "brief":
            self._ev("brief_text", {"text": summary})
            gates.propose_artifact(self.run_id, "brief", "brief")
        gid = gates.request_gate(self.run_id, step, {"card": card, "summary": summary, "memory_refs": refs,
                                                       "call_id": call_id})
        self._ev("run_status", {"status": "awaiting_human"}, "system")
        return ToolResult(True, {"pending": True, "gate_id": gid, "step": step}, asked=True)

    def _check_steer_applied(self, step: str, evs: list[dict]) -> None:
        sid = None
        for e in evs:
            if e["kind"] == "gate_answered" and e["payload"]["step"] == step and e["payload"]["kind"] == "steer":
                sid = e["id"]
        if sid is None:
            return
        if step == "brief":
            return  # the new restatement is compared in _card (must differ from the last one)
        req = self.REQUIRED_ARTIFACT.get(step)
        if req and not any(e["kind"] == "artifact_proposed" and e["payload"].get("artifact") == req
                           and e["id"] > sid for e in evs):
            tool = {"ideas": "write_concepts or revise_concept", "preview": "storyboard",
                    "edit": "revise_edit then build_cuts", "check": "revise_edit then build_cuts"}[step]
            raise ToolError("steer_not_applied", "the human steered and nothing was produced since: a steer is "
                            "never approval", True, f"apply the steer with {tool}, then call ask_human again")

    def _urls(self, names: list[str]) -> list[str]:
        return [artifact_url(self.run_id, n) for n in names]

    def _concept_summaries(self, cs: list[dict]) -> list[dict]:
        return [{"id": c["id"], "title": c.get("title"), "angle": c.get("angle"), "hook": c.get("hook"),
                 "cta": c.get("cta"), "length_s": c.get("length_s"), "beats": len(c.get("beats", []))} for c in cs]

    def _card(self, step: str, st: dict, summary: str, refs: list[str], rec: str | None) -> dict:
        cost = NEXT_COST[step]
        req = self.REQUIRED_ARTIFACT.get(step)
        if req and not self._art_ok(st, req):
            tool = {"ideas": "write_concepts", "preview": "storyboard", "edit": "build_cuts",
                    "check": "build_cuts"}[step]
            raise ToolError("missing_artifact", f"nothing to show for step '{step}' yet", True,
                            f"call {tool} first")
        if step == "brief":
            if not summary:
                raise ToolError("validation", "summary (the restated brief) is required", True)
            if any(s["step"] == "brief" for s in st["steers"]) and summary == self._last_brief_text():
                raise ToolError("steer_not_applied", "restated brief is identical to the previous one", True,
                                "revise the restatement to reflect the human's steer")
            return {"type": "brief", "text": summary, "memory_cited": refs, "cost_estimate_usd": cost}
        if step == "ideas":
            cs = self.concepts_list()
            rec = rec if rec in {c["id"] for c in cs} else None
            return {"type": "ideas", "concepts": cs, "recommended": rec, "summary": summary,
                    "memory_cited": refs, "cost_estimate_usd": cost}
        if step == "preview":
            fr = self._read("frames.json", {"files": []})
            return {"type": "preview", "frames": self._urls(fr["files"]), "summary": summary,
                    "cost_estimate_usd": cost}
        meta = self._read("meta.json") or {}
        if step == "edit":
            wc = self.working_concept(st)
            return {"type": "edit", "beats": wc.get("beats", []), "captions": meta.get("captions", []),
                    "hook": wc.get("hook"), "cta": wc.get("cta"),
                    "cuts": {r: artifact_url(self.run_id, n) for r, n in meta.get("cuts", {}).items()},
                    "summary": summary, "cost_estimate_usd": cost}
        if step == "check":
            q = meta.get("qc") or {}
            return {"type": "check", "verdict": q, "history": meta.get("qc_history", []),
                    "proposed_fixes": q.get("fixes", []),
                    "cuts": {r: artifact_url(self.run_id, n) for r, n in meta.get("cuts", {}).items()},
                    "summary": summary, "cost_estimate_usd": cost}
        zname = self._package(meta)
        props = [{k: i[k] for k in ("id", "kind", "text", "source", "status", "pinned", "evidence", "created_at")}
                 for i in memory.list_memory(self.client_id, "proposed")]
        return {"type": "ship", "cuts": {r: artifact_url(self.run_id, n) for r, n in meta.get("cuts", {}).items()},
                "zip": artifact_url(self.run_id, zname), "memory_proposals": props, "summary": summary,
                "cost_estimate_usd": cost}

    def _package(self, meta: dict) -> str:
        srt = []
        for i, c in enumerate(meta.get("captions", []), 1):
            srt.append(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{c['text']}\n")
        (self.dir / "captions.srt").write_text("\n".join(srt))
        with zipfile.ZipFile(self.dir / "package.zip", "w") as z:
            for n in meta.get("cuts", {}).values():
                if (self.dir / n).is_file():
                    z.write(self.dir / n, n)
            z.write(self.dir / "captions.srt", "captions.srt")
        return "package.zip"

    # ---------------- answers (applied once, at the start of the next step turn)
    def consume_answer(self) -> dict | None:
        """Apply the newest unapplied human answer (edits written as artifacts). Returns the compact
        description the next step turn is given, or None when there is nothing new."""
        evs = events.list_events(self.run_id)
        applied = {e["payload"]["gate_id"] for e in evs if e["kind"] == "answer_applied"}
        last = None
        for e in evs:
            if e["kind"] == "gate_answered":
                last = e
        if last is None or last["payload"]["gate_id"] in applied:
            return None
        out = self.answer_output(last["payload"]["gate_id"])
        self._ev("answer_applied", {"gate_id": last["payload"]["gate_id"], "output": out}, "system")
        return out

    def last_answer(self) -> dict | None:
        for e in reversed(events.list_events(self.run_id)):
            if e["kind"] == "answer_applied":
                return e["payload"]["output"]
        return None

    def answer_output(self, gate_id: int) -> dict:
        g = gates.get_gate(gate_id)
        a = g["answer"] or {}
        kind, step, payload = a.get("kind"), g["step"], a.get("payload") or {}
        st = self.state()
        out: dict[str, Any] = {"step": step, "gate_id": gate_id}
        if kind == "approve":
            out["answer"] = "approved"
            out["next_step"] = self.current_step(st)
            if step == "ideas":
                out["chosen_concept"] = self.selected_concept_id(st)
        elif kind == "steer":
            out.update(answer="steer", text=a.get("text"), approved=False,
                       note="A steer is NOT approval. Apply it with a tool, then ask_human again.")
        elif kind == "edit":
            out.update(answer="edited", approved=False, **self._apply_edit(step, payload, st, gate_id))
        elif kind == "back":
            to = next((e["payload"].get("to") for e in reversed(events.list_events(self.run_id))
                       if e["kind"] == "gate_answered" and e["payload"]["gate_id"] == gate_id), None)
            out.update(answer="back", to=to, reason=a.get("reason"), stale_artifacts=st["stale_artifacts"],
                       approved_steps=sorted(st["step_approval"], key=step_index),
                       note=f"The human sent the run back to '{to}'. Later artifacts are stale and must be redone.")
        return out

    def _apply_edit(self, step: str, payload: dict, st: dict, gate_id: int = 0) -> dict:
        fields = dict(payload.get("fields") or {})
        applied, ignored = [], []
        if step == "brief":
            if fields.get("text"):
                self._ev("brief_text", {"text": str(fields["text"])}, "human")
                applied.append("text")
            ignored += [k for k in fields if k != "text"]
        elif step in ("ideas", "edit"):
            def put(c: dict) -> None:
                locked = {}
                for k in ("hook", "cta", "title"):
                    if k in fields and isinstance(fields[k], str):
                        c[k] = fields[k]
                        locked[k] = fields[k]
                        applied.append(k)
                if "hook" in fields and c.get("beats"):
                    c["beats"][0]["on_screen_text"] = fields["hook"]
                if locked:
                    self._lock_fields(c["id"], locked)
            if step == "ideas":
                cs = self.concepts_list()
                tid = fields.get("concept_id") or payload.get("choice") or payload.get("concept_id")
                card = self._gate_card(gate_id) or {}
                target = next((c for c in cs if c["id"] == tid), None) or \
                    next((c for c in cs if c["id"] == card.get("recommended")), None) or (cs[0] if cs else None)
                if target is not None:
                    put(target)
                    self._save_concepts(cs, st, actor="system")
            else:
                wc = self.working_concept(st)
                put(wc)
                self._save_working(st, wc)
                self._stale([("cuts", "edit"), ("qc", "check")])
                if "captions" in fields:
                    ignored.append("captions (burned-in captions are generated from speech; edit is not applied)")
            ignored += [k for k in fields if k not in ("hook", "cta", "title", "captions", "concept_id")]
        else:
            ignored += list(fields)
        return {"fields": _trim(fields, 300), "applied": sorted(set(applied)), "ignored": ignored,
                "note": "The human edited these fields. Treat them as final; do not overwrite them. "
                        + ("Rebuild with build_cuts, then ask_human." if step == "edit" else "Ask_human again.")}


def _ts(t: float) -> str:
    ms = int(round(float(t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


# ------------------------------------------------------------------ audit
def audit_gate_violations(run_id: str) -> dict:
    """Walk the log: every paid tool that STARTED must have had a valid approval at that moment."""
    evs = events.list_events(run_id)
    violations, blocked = [], 0
    for i, e in enumerate(evs):
        if e["kind"] == "gate_blocked_attempt":
            blocked += 1
        if e["kind"] == "tool_started" and e["payload"].get("tool") in gates.TOOL_GATES:
            tool = e["payload"]["tool"]
            st = events.replay(evs[:i])
            need = gates.TOOL_GATES[tool]
            ap = st["step_approval"].get(need)
            ok = bool(ap) and not any(step_index(s) <= step_index(need) and pid > ap["event_id"]
                                      for s, pid in st["latest_proposal"].items())
            if not ok:
                violations.append({"event_id": e["id"], "tool": tool})
    return {"violations": violations, "blocked_attempts": blocked}

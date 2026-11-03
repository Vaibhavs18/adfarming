"""Human gates. The code, not the model, decides whether a paid tool may run."""
from __future__ import annotations

import json
from typing import Any

from . import db, events
from .events import STEPS, step_index

# paid/approval-required tool -> step whose gate must be approved
TOOL_GATES = {"storyboard": "ideas", "build_cuts": "preview", "track_subject": "preview"}
ANSWER_KINDS = ("approve", "steer", "edit", "back")
_STATUS = {"approve": "approved", "steer": "steered", "edit": "edited", "back": "back"}


class GateError(Exception):
    pass


class GateBlocked(GateError, PermissionError):
    """A paid tool was attempted without a valid approval."""


class GateAlreadyAnswered(GateError):
    pass


def request_gate(run_id: str, step: str, payload: dict | None = None) -> int:
    events.step_index(step)
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
            raise GateError(f"unknown run {run_id!r}")
        if c.execute("SELECT 1 FROM gates WHERE run_id=? AND status='pending'", (run_id,)).fetchone():
            raise GateError("run already has a pending gate")
        gid = c.execute("INSERT INTO gates(run_id,step,status,requested_at) VALUES (?,?,'pending',?)",
                        (run_id, step, db.now())).lastrowid
        events.insert_event(c, run_id, "gate_requested", "agent",
                            {"gate_id": gid, "step": step, "payload": payload or {}})
    return int(gid)


def get_gate(gate_id: int) -> dict[str, Any]:
    with db.read() as c:
        r = c.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone()
    if not r:
        raise GateError(f"unknown gate {gate_id}")
    d = dict(r)
    d["answer"] = json.loads(d["answer"]) if d["answer"] else None
    return d


def propose_artifact(run_id: str, step: str, artifact: str, ref: str | None = None,
                     actor: str = "agent") -> int:
    """Record that the agent produced/changed an artifact; any earlier approval no
    longer covers it."""
    events.step_index(step)
    return events.append_event(run_id, "artifact_proposed", actor,
                               {"step": step, "artifact": artifact, "ref": ref})


def answer_gate(gate_id: int, kind: str, payload: dict | None = None, text: str | None = None,
                reason: str | None = None) -> dict[str, Any]:
    if kind not in ANSWER_KINDS:
        raise ValueError(f"kind must be one of {ANSWER_KINDS}")
    payload = dict(payload or {})
    text = (text or "").strip() or None
    if kind == "steer" and not text:
        raise ValueError("steer requires non-empty text")
    if kind == "edit" and not isinstance(payload.get("fields"), dict):
        raise ValueError("edit requires payload['fields'] dict")
    from . import memory  # local import avoids cycle

    with db.tx() as c:
        g = c.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone()
        if not g:
            raise GateError(f"unknown gate {gate_id}")
        run_id, step = g["run_id"], g["step"]
        to = None
        if kind == "back":
            i = step_index(step)
            if i == 0:
                raise ValueError("cannot go back from the first step")
            to = payload.get("to") or STEPS[i - 1]
            if step_index(to) >= i:
                raise ValueError("back target must be an earlier step")
        # atomic compare-and-set: only one concurrent answer can win
        n = c.execute("UPDATE gates SET status=?, answered_at=?, answer=? WHERE id=? AND status='pending'",
                      (_STATUS[kind], db.now(),
                       json.dumps({"kind": kind, "payload": payload, "text": text, "reason": reason}),
                       gate_id)).rowcount
        if n != 1:
            raise GateAlreadyAnswered(f"gate {gate_id} already answered ({g['status']})")
        ev = {"gate_id": gate_id, "step": step, "kind": kind, "status": _STATUS[kind]}
        if to:
            ev["to"] = to
        eid = events.insert_event(c, run_id, "gate_answered", "human", ev, reason)
        out: dict[str, Any] = {"gate_id": gate_id, "run_id": run_id, "step": step,
                               "kind": kind, "status": _STATUS[kind], "event_id": eid}
        if kind == "steer":
            events.insert_event(c, run_id, "steer", "human", {"step": step, "text": text, "gate_id": gate_id})
            client_id = c.execute("SELECT client_id FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            out["memory_item"] = memory.propose_memory(
                client_id, "preference", text, source="agent_inferred",
                evidence={"run_id": run_id, "event_seq": eid, "human_quote": text,
                          "doc_ref": f"steer@{step}"},
                run_id=run_id, conn=c)
        elif kind == "edit":
            events.insert_event(c, run_id, "artifact_edited", "human",
                                {"step": step, "artifact": payload.get("artifact"),
                                 "fields": payload["fields"]}, reason)
        elif kind == "back":
            _invalidate_from(c, run_id, to)
            out["to"] = to
    return out


def _invalidate_from(c, run_id: str, to: str) -> None:
    """Back to `to`: revoke approvals of steps >= to, mark later artifacts stale. Never deletes."""
    st = events.replay(events.list_events(run_id, conn=c))
    t = step_index(to)
    for step, ap in sorted(st["step_approval"].items(), key=lambda kv: step_index(kv[0])):
        if step_index(step) >= t:
            events.insert_event(c, run_id, "gate_invalidated", "system",
                                {"step": step, "gate_id": ap["gate_id"], "reason": f"back to {to}"})
    for name, a in st["artifacts"].items():
        if step_index(a["step"]) > t and a["status"] != "stale":
            events.insert_event(c, run_id, "artifact_stale", "system",
                                {"artifact": name, "step": a["step"], "reason": f"back to {to}"})


def is_approved(run_id: str, tool: str) -> tuple[bool, str]:
    if tool not in TOOL_GATES:
        raise ValueError(f"tool {tool!r} is not a gated tool; known: {sorted(TOOL_GATES)}")
    need = TOOL_GATES[tool]
    st = events.replay(events.list_events(run_id))
    ap = st["step_approval"].get(need)
    if not ap:
        return False, f"gate '{need}' is not approved for {tool}"
    for step, pid in st["latest_proposal"].items():
        if step_index(step) <= step_index(need) and pid > ap["event_id"]:
            return False, f"'{step}' was changed after the '{need}' approval; re-approve"
    return True, "ok"


def assert_approved(run_id: str, tool: str) -> None:
    """Raise GateBlocked unless the matching gate was approved AFTER the latest relevant proposal."""
    ok, why = is_approved(run_id, tool)
    if not ok:
        raise GateBlocked(why)

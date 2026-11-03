"""Append-only event log and pure replay of run state.

Event kinds understood by the replay (all others are stored but ignored):
  run_created        {client_id, brief}
  artifact_proposed  {artifact, step, ref?}
  gate_requested     {gate_id, step}
  gate_answered      {gate_id, step, kind, status, to?}
  gate_invalidated   {step, gate_id}       (approval revoked by a back)
  artifact_stale     {artifact, step}      (never deleted, only marked)
  artifact_edited    {artifact?, step, fields}
  cost               {tool, usd}
  steer              {text, step}
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db

STEPS = ["brief", "ideas", "preview", "edit", "check", "ship"]
ACTORS = ("human", "agent", "system")


def step_index(step: str) -> int:
    try:
        return STEPS.index(step)
    except ValueError:
        raise ValueError(f"unknown step {step!r}; expected one of {STEPS}") from None


def insert_event(conn: sqlite3.Connection, run_id: str, kind: str, actor: str = "system",
                 payload: dict | None = None, reason: str | None = None) -> int:
    if actor not in ACTORS:
        raise ValueError(f"bad actor {actor!r}")
    cur = conn.execute(
        "INSERT INTO events(run_id,ts,kind,actor,payload,reason) VALUES (?,?,?,?,?,?)",
        (run_id, db.now(), kind, actor, json.dumps(payload or {}, sort_keys=True), reason),
    )
    return int(cur.lastrowid)


def append_event(run_id: str, kind: str, actor: str = "system", payload: dict | None = None,
                 reason: str | None = None, conn: sqlite3.Connection | None = None) -> int:
    with db.tx(conn) as c:
        return insert_event(c, run_id, kind, actor, payload, reason)


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    return d


def list_events(run_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    def q(c):
        return [_row(r) for r in c.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,))]
    if conn is not None:
        return q(conn)
    with db.read() as c:
        return q(c)


def create_run(client_id: str, run_id: str | None = None, brief: str | None = None) -> str:
    import uuid
    run_id = run_id or "r_" + uuid.uuid4().hex[:10]
    with db.tx() as c:
        db.ensure_client(c, client_id)
        c.execute("INSERT INTO runs(id,client_id,brief,created_at) VALUES (?,?,?,?)",
                  (run_id, client_id, brief, db.now()))
        insert_event(c, run_id, "run_created", "system",
                     {"client_id": client_id, "brief": brief})
    return run_id


def record_cost(run_id: str, tool: str, usd: float, conn: sqlite3.Connection | None = None) -> int:
    if usd < 0:
        raise ValueError("usd must be >= 0")
    with db.tx(conn) as c:
        c.execute("INSERT INTO cost_ledger(run_id,tool,usd,ts) VALUES (?,?,?,?)",
                  (run_id, tool, float(usd), db.now()))
        return insert_event(c, run_id, "cost", "system", {"tool": tool, "usd": float(usd)})


def replay(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure function: events -> state. Nothing outside the log is consulted."""
    st: dict[str, Any] = {
        "run_id": events[0]["run_id"] if events else None,
        "client_id": None, "brief": None,
        "current_step": "brief", "pending_gate": None,
        "artifacts": {},            # artifact -> {step, status, ref, event_id}
        "step_approval": {},        # step -> {gate_id, event_id}   (valid approvals only)
        "latest_proposal": {},      # step -> event id of latest proposal
        "cost_usd": 0.0, "cost_by_tool": {}, "steers": [],
        "last_event_id": events[-1]["id"] if events else 0,
    }
    for e in events:
        k, p, eid = e["kind"], e["payload"], e["id"]
        if k == "run_created":
            st["client_id"], st["brief"] = p.get("client_id"), p.get("brief")
        elif k == "artifact_proposed":
            name = p.get("artifact") or f"{p['step']}"
            st["artifacts"][name] = {"step": p["step"], "status": "proposed",
                                     "ref": p.get("ref"), "event_id": eid}
            st["latest_proposal"][p["step"]] = eid
        elif k == "artifact_edited":
            st["latest_proposal"][p["step"]] = eid
            if p.get("artifact") in st["artifacts"]:
                st["artifacts"][p["artifact"]].update(status="proposed", event_id=eid,
                                                      fields=p.get("fields"))
        elif k == "gate_requested":
            st["pending_gate"] = {"gate_id": p["gate_id"], "step": p["step"],
                                  "payload": p.get("payload"), "event_id": eid}
            st["current_step"] = p["step"]
        elif k == "gate_answered":
            if st["pending_gate"] and st["pending_gate"]["gate_id"] == p["gate_id"]:
                st["pending_gate"] = None
            step, kind = p["step"], p["kind"]
            if kind == "approve":
                st["step_approval"][step] = {"gate_id": p["gate_id"], "event_id": eid}
                for a in st["artifacts"].values():
                    if a["step"] == step and a["status"] == "proposed":
                        a["status"] = "approved"
                i = step_index(step)
                st["current_step"] = STEPS[i + 1] if i + 1 < len(STEPS) else "done"
            elif kind == "back":
                st["current_step"] = p.get("to", step)
            else:
                st["current_step"] = step
        elif k == "gate_invalidated":
            st["step_approval"].pop(p["step"], None)
            for a in st["artifacts"].values():
                if a["step"] == p["step"] and a["status"] == "approved":
                    a["status"] = "proposed"
        elif k == "artifact_stale":
            if p.get("artifact") in st["artifacts"]:
                st["artifacts"][p["artifact"]]["status"] = "stale"
        elif k == "cost":
            st["cost_usd"] = round(st["cost_usd"] + float(p["usd"]), 6)
            st["cost_by_tool"][p["tool"]] = round(
                st["cost_by_tool"].get(p["tool"], 0.0) + float(p["usd"]), 6)
        elif k == "steer":
            st["steers"].append({"step": p.get("step"), "text": p.get("text")})
    st["approved_artifacts"] = {n: a for n, a in st["artifacts"].items()
                                if a["status"] == "approved"}
    st["stale_artifacts"] = sorted(n for n, a in st["artifacts"].items()
                                   if a["status"] == "stale")
    return st


def rebuild_run_state(run_id: str) -> dict[str, Any]:
    """Reconstruct current step, pending gate, approved artifacts, cost from the log only."""
    return replay(list_events(run_id))

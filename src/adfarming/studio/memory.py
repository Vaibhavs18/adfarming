"""Client memory: only APPROVED items are ever injected into prompts."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from . import db, events

KINDS = ("brand_voice", "hard_rule", "performance", "asset", "preference", "rejection")
DEFAULT_BUDGET_CHARS = 4800  # ~1200 tokens at chars/4


def normalise(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.casefold(), flags=re.UNICODE).strip()


def _item(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["evidence"] = json.loads(d["evidence"] or "{}")
    d["pinned"] = bool(d["pinned"])
    return d


def _history(c, client_id: str, action: str, changes: list[dict]) -> None:
    c.execute("INSERT INTO memory_history(client_id,ts,action,changes) VALUES (?,?,?,?)",
              (client_id, db.now(), action, json.dumps(changes)))


def _insert(c, *, id: str, client_id: str, kind: str, text: str, source: str, status: str,
            evidence: dict | None, supersedes: str | None = None, pinned: bool = False,
            created_at: str | None = None, decided_by: str | None = None,
            decision_note: str | None = None, edited_from: str | None = None) -> None:
    if kind not in KINDS:
        raise ValueError(f"bad kind {kind!r}")
    if not text or not text.strip():
        raise ValueError("empty memory text")
    db.ensure_client(c, client_id)
    c.execute(
        """INSERT INTO memory_items(id,client_id,kind,text,norm,source,status,evidence,created_at,
           supersedes,pinned,decided_at,decided_by,decision_note,edited_from)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (id, client_id, kind, text.strip(), normalise(text), source, status,
         json.dumps(evidence or {}, sort_keys=True), created_at or db.now(), supersedes,
         int(pinned), db.now() if status != "proposed" else None, decided_by, decision_note,
         edited_from))


def _new_id() -> str:
    return "m_" + uuid.uuid4().hex[:10]


def load_seed(client_id: str, path: str | Path) -> int:
    """Load approved.jsonl for a client. Idempotent; ids are namespaced '<client>:<seed id>'."""
    p = Path(path)
    if p.is_dir():
        p = p / "approved.jsonl"
    n = 0
    with db.tx() as c:
        db.ensure_client(c, client_id)
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            iid = f"{client_id}:{d['id']}"
            if c.execute("SELECT 1 FROM memory_items WHERE id=?", (iid,)).fetchone():
                continue
            sup = d.get("supersedes")
            _insert(c, id=iid, client_id=client_id, kind=d["kind"], text=d["text"],
                    source=d.get("source", "human"), status=d.get("status", "approved"),
                    evidence=d.get("evidence"), supersedes=f"{client_id}:{sup}" if sup else None,
                    pinned=bool(d.get("pinned")), created_at=d.get("created_at"),
                    decided_by=d.get("decided_by"), decision_note=d.get("decision_note"))
            n += 1
    return n


def list_memory(client_id: str, status: str | None = None) -> list[dict[str, Any]]:
    with db.read() as c:
        q, a = "SELECT * FROM memory_items WHERE client_id=?", [client_id]
        if status:
            q, a = q + " AND status=?", a + [status]
        return [_item(r) for r in c.execute(q + " ORDER BY created_at, id", a)]


def read_memory(client_id: str, budget_chars: int = DEFAULT_BUDGET_CHARS,
                run_id: str | None = None) -> list[dict[str, Any]]:
    """Approved items only. Order: hard_rule, pinned, human-sourced, most recent.
    Hard rules are never dropped (even if they alone exceed the budget); other
    items are added while they fit."""
    items = list_memory(client_id, "approved")
    items.sort(key=lambda i: i["id"])                       # stable tiebreak
    items.sort(key=lambda i: i["created_at"], reverse=True)  # recent first
    items.sort(key=lambda i: (i["kind"] != "hard_rule", not i["pinned"], i["source"] != "human"))
    out, used = [], 0
    for it in items:
        cost = len(it["text"])
        if it["kind"] == "hard_rule" or used + cost <= budget_chars:
            out.append(it)
            used += cost
    if run_id:
        events.append_event(run_id, "memory_read", "system",
                            {"client_id": client_id, "ids": [i["id"] for i in out], "chars": used})
    return out


def propose_memory(client_id: str, kind: str, text: str, source: str = "agent_inferred",
                   evidence: dict | None = None, run_id: str | None = None,
                   supersedes: str | None = None,
                   conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Agent proposal. Never touches approved memory. Returns
    {'status': 'proposed'|'duplicate'|'previously_dismissed', 'item_id'|'duplicate_of'}."""
    if source != "agent_inferred":
        raise ValueError("only agent_inferred proposals are allowed; humans use add_human_memory")
    norm = normalise(text)
    with db.tx(conn) as c:
        row = c.execute(
            "SELECT id,status FROM memory_items WHERE client_id=? AND norm=? AND status!='superseded' "
            "ORDER BY (status='rejected') DESC, id LIMIT 1", (client_id, norm)).fetchone()
        if row and row["status"] == "rejected":
            res = {"status": "previously_dismissed", "duplicate_of": row["id"]}
        elif row:
            res = {"status": "duplicate", "duplicate_of": row["id"]}
        else:
            iid = _new_id()
            _insert(c, id=iid, client_id=client_id, kind=kind, text=text, source=source,
                    status="proposed", evidence=evidence, supersedes=supersedes)
            _history(c, client_id, "propose", [{"id": iid, "before_status": None,
                                                 "after_status": "proposed"}])
            res = {"status": "proposed", "item_id": iid}
        if run_id:
            events.insert_event(c, run_id, "memory_proposed", "agent",
                                {"client_id": client_id, "kind": kind, "text": text, **res})
        return res


def add_human_memory(client_id: str, kind: str, text: str, evidence: dict | None = None,
                     pinned: bool = False, decided_by: str = "human") -> str:
    with db.tx() as c:
        iid = _new_id()
        _insert(c, id=iid, client_id=client_id, kind=kind, text=text, source="human",
                status="approved", evidence=evidence, pinned=pinned, decided_by=decided_by)
        _history(c, client_id, "add", [{"id": iid, "before_status": None, "after_status": "approved"}])
    return iid


def decide_memory(item_id: str, decision: str, text: str | None = None, decided_by: str = "human",
                  note: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    """accept | reject | edit. 'edit' approves a NEW human-sourced item with the new text that
    supersedes the original (original kept, soft-superseded)."""
    if decision not in ("accept", "reject", "edit"):
        raise ValueError("decision must be accept|reject|edit")
    if decision == "edit" and not (text and text.strip()):
        raise ValueError("edit requires text")
    with db.tx() as c:
        r = c.execute("SELECT * FROM memory_items WHERE id=?", (item_id,)).fetchone()
        if not r:
            raise KeyError(item_id)
        if r["status"] != "proposed":
            raise ValueError(f"item {item_id} is {r['status']}, only proposed items can be decided")
        cid, now = r["client_id"], db.now()
        changes: list[dict] = []
        result: dict[str, Any] = {"item_id": item_id, "decision": decision}
        if decision in ("accept", "reject"):
            new = "approved" if decision == "accept" else "rejected"
            c.execute("UPDATE memory_items SET status=?,decided_at=?,decided_by=?,decision_note=? WHERE id=?",
                      (new, now, decided_by, note, item_id))
            changes.append({"id": item_id, "before_status": "proposed", "after_status": new})
            result["status"] = new
            if decision == "accept" and r["supersedes"]:
                sup = c.execute("SELECT status FROM memory_items WHERE id=?", (r["supersedes"],)).fetchone()
                if sup and sup["status"] == "approved":
                    c.execute("UPDATE memory_items SET status='superseded' WHERE id=?", (r["supersedes"],))
                    changes.append({"id": r["supersedes"], "before_status": "approved",
                                    "after_status": "superseded"})
        else:
            nid = _new_id()
            # original proposal is dismissed-as-superseded; new human item carries the edit
            c.execute("UPDATE memory_items SET status='superseded',decided_at=?,decided_by=? WHERE id=?",
                      (now, decided_by, item_id))
            _insert(c, id=nid, client_id=cid, kind=r["kind"], text=text, source="human",
                    status="approved", evidence=json.loads(r["evidence"] or "{}"),
                    supersedes=item_id, decided_by=decided_by, decision_note=note,
                    edited_from=r["text"])
            changes += [{"id": item_id, "before_status": "proposed", "after_status": "superseded"},
                        {"id": nid, "before_status": None, "after_status": "approved"}]
            result.update(status="approved", new_item_id=nid)
        _history(c, cid, decision, changes)
        if run_id:
            events.insert_event(c, run_id, "memory_decided", "human", result, note)
    return result


def supersede_memory(old_id: str, kind: str, text: str, decided_by: str = "human") -> str:
    """Human replaces an approved item with new text (soft: old kept as 'superseded')."""
    with db.tx() as c:
        r = c.execute("SELECT * FROM memory_items WHERE id=?", (old_id,)).fetchone()
        if not r or r["status"] != "approved":
            raise ValueError("can only supersede an approved item")
        nid = _new_id()
        c.execute("UPDATE memory_items SET status='superseded' WHERE id=?", (old_id,))
        _insert(c, id=nid, client_id=r["client_id"], kind=kind, text=text, source="human",
                status="approved", evidence=None, supersedes=old_id, decided_by=decided_by,
                edited_from=r["text"])
        _history(c, r["client_id"], "supersede",
                 [{"id": old_id, "before_status": "approved", "after_status": "superseded"},
                  {"id": nid, "before_status": None, "after_status": "approved"}])
    return nid


def undo_last(client_id: str) -> dict[str, Any] | None:
    """Soft undo of the latest not-yet-undone change: restores previous statuses.
    Rows created by the change become 'rejected' (kept, never deleted); proposals
    that were created stay as they are (undoing 'propose' dismisses it)."""
    with db.tx() as c:
        h = c.execute("SELECT * FROM memory_history WHERE client_id=? AND undone=0 "
                      "ORDER BY id DESC LIMIT 1", (client_id,)).fetchone()
        if not h:
            return None
        for ch in reversed(json.loads(h["changes"])):
            c.execute("UPDATE memory_items SET status=?, decided_at=NULL WHERE id=?",
                      (ch["before_status"] or "rejected", ch["id"]))
        c.execute("UPDATE memory_history SET undone=1 WHERE id=?", (h["id"],))
        return {"undone": h["action"], "history_id": h["id"]}

import threading

import pytest

from adfarming.studio import events, gates, memory


def approve_step(run, step, artifact=None):
    if artifact:
        gates.propose_artifact(run, step, artifact)
    g = gates.request_gate(run, step, {})
    gates.answer_gate(g, "approve")
    return g


def test_gate_cannot_be_bypassed():
    r = events.create_run("c1")
    with pytest.raises(gates.GateBlocked):
        gates.assert_approved(r, "build_cuts")
    gates.propose_artifact(r, "ideas", "concepts")
    approve_step(r, "ideas")
    gates.assert_approved(r, "storyboard")
    with pytest.raises(gates.GateBlocked):          # preview gate still needed
        gates.assert_approved(r, "build_cuts")
    approve_step(r, "preview", "frames")
    gates.assert_approved(r, "build_cuts")
    gates.assert_approved(r, "track_subject")
    # back invalidates
    g = gates.request_gate(r, "edit")
    out = gates.answer_gate(g, "back", {"to": "ideas"})
    assert out["to"] == "ideas"
    for tool in ("storyboard", "build_cuts", "track_subject"):
        with pytest.raises(gates.GateBlocked):
            gates.assert_approved(r, tool)
    st = events.rebuild_run_state(r)
    assert "frames" in st["stale_artifacts"] or st["artifacts"]["frames"]["status"] == "stale"
    # nothing deleted: original events still there
    kinds = [e["kind"] for e in events.list_events(r)]
    assert kinds.count("gate_answered") == 3 and "artifact_stale" in kinds


def test_steer_is_not_approval_and_new_proposal_revokes():
    r = events.create_run("c1")
    gates.propose_artifact(r, "ideas", "concepts")
    g = gates.request_gate(r, "ideas")
    gates.answer_gate(g, "steer", text="c2 but shorter hook")
    with pytest.raises(gates.GateBlocked):
        gates.assert_approved(r, "storyboard")
    approve_step(r, "ideas")
    gates.assert_approved(r, "storyboard")
    gates.propose_artifact(r, "ideas", "concepts")   # revised after approval
    with pytest.raises(gates.GateBlocked):
        gates.assert_approved(r, "storyboard")


def test_validation():
    r = events.create_run("c1")
    g = gates.request_gate(r, "brief")
    with pytest.raises(gates.GateError):
        gates.request_gate(r, "ideas")               # one pending gate at a time
    with pytest.raises(ValueError):
        gates.answer_gate(g, "steer", text="  ")
    with pytest.raises(ValueError):
        gates.answer_gate(g, "edit", {})
    with pytest.raises(ValueError):
        gates.answer_gate(g, "back")                 # first step
    with pytest.raises(ValueError):
        gates.answer_gate(g, "maybe")
    with pytest.raises(ValueError):
        gates.assert_approved(r, "not_a_tool")
    assert gates.get_gate(g)["status"] == "pending"


def test_steer_creates_verbatim_event_and_proposed_memory():
    r = events.create_run("c1")
    g = gates.request_gate(r, "ideas")
    txt = "c2 but shorter hook, and no Diwali"
    out = gates.answer_gate(g, "steer", text=txt)
    assert out["memory_item"]["status"] == "proposed"
    steers = [e for e in events.list_events(r) if e["kind"] == "steer"]
    assert steers[0]["payload"]["text"] == txt
    items = memory.list_memory("c1", "proposed")
    assert len(items) == 1 and items[0]["text"] == txt
    assert items[0]["source"] == "agent_inferred"
    assert memory.read_memory("c1") == []             # not injected


def test_concurrent_answers_do_not_double_apply():
    r = events.create_run("c1")
    g = gates.request_gate(r, "brief")
    results, errs = [], []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        try:
            results.append(gates.answer_gate(g, "steer", text=f"steer {i}"))
        except gates.GateAlreadyAnswered as e:
            errs.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(results) == 1 and len(errs) == 7
    evs = events.list_events(r)
    assert sum(e["kind"] == "gate_answered" for e in evs) == 1
    assert sum(e["kind"] == "steer" for e in evs) == 1
    assert len(memory.list_memory("c1", "proposed")) == 1


def test_state_rebuilds_identically_after_reopen(tmp_db, monkeypatch):
    r = events.create_run("c1", brief="b")
    gates.propose_artifact(r, "ideas", "concepts", ref="out/c.json")
    approve_step(r, "ideas")
    events.record_cost(r, "storyboard", 0.07)
    events.record_cost(r, "storyboard", 0.03)
    g = gates.request_gate(r, "preview", {"cost": 0.3})
    before = events.rebuild_run_state(r)
    assert before["pending_gate"]["gate_id"] == g and before["current_step"] == "preview"
    assert before["cost_usd"] == 0.1 and "concepts" in before["approved_artifacts"]
    # "restart": new process = fresh connections only; copy DB file to a new location
    import shutil
    new = tmp_db.parent / "copy.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            shutil.copy(str(tmp_db) + suffix, str(new) + suffix)
        except FileNotFoundError:
            pass
    monkeypatch.setenv("ADFARMING_DB", str(new))
    assert events.rebuild_run_state(r) == before
    gates.answer_gate(g, "approve")                   # paused gate still answerable
    gates.assert_approved(r, "build_cuts")


def test_event_log_is_append_only():
    import sqlite3
    r = events.create_run("c1")
    from adfarming.studio import db
    with db.tx() as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("UPDATE events SET kind='x' WHERE run_id=?", (r,))
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("DELETE FROM events WHERE run_id=?", (r,))

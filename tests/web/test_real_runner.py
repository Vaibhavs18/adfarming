"""RealRunner behaviour with a fake AgentLoop (no network, no paid tools)."""
import json
import threading

import pytest

from adfarming import runner as runner_mod
from adfarming import web
from adfarming.studio import events, gates

from .helpers import answer, new_run


class FakeLoop:
    """advance(): first call asks for the brief gate; later calls obey `script`."""

    def __init__(self, script=None):
        self.calls, self.script = [], list(script or [])
        self.release = None  # optional threading.Event to hold an advance open
        self.entered = threading.Event()

    def advance(self, run_id):
        self.calls.append(run_id)
        self.entered.set()
        if self.release is not None:
            self.release.wait(10)
        if self.script:
            act = self.script.pop(0)
            if isinstance(act, Exception):
                raise act
            if act == "done":
                events.append_event(run_id, "run_status", "system", {"status": "done"})
                return {"status": "done", "run_id": run_id}
            if act == "error":
                return {"status": "error", "run_id": run_id, "error": "boom"}
            if act == "weird":
                return {"status": "???"}
        st = events.rebuild_run_state(run_id)
        if st["pending_gate"]:
            return {"status": "awaiting_human", "run_id": run_id}
        step = next(s for s in events.STEPS if s not in st["step_approval"])
        gates.propose_artifact(run_id, step, step)
        gates.request_gate(run_id, step, {"card": {"type": step, "text": "x", "memory_cited": [],
                                                   "cost_estimate_usd": 0.0}})
        # NOTE: the real loop appends its own run_status; the runner must map the returned status itself.
        return {"status": "awaiting_human", "run_id": run_id}


@pytest.fixture
def real(env):
    def make(script=None):
        loop = FakeLoop(script)
        r = runner_mod.RealRunner(loop=loop)
        web.set_runner(r)
        return r, loop
    return make


def test_start_returns_immediately_then_awaiting(real, client):
    r, loop = real()
    loop.release = threading.Event()
    rid = new_run(client)
    assert client.get(f"/api/runs/{rid}").json()["status"] == "running"
    loop.entered.wait(5)
    loop.release.set()
    assert r.wait_idle()
    st = client.get(f"/api/runs/{rid}").json()
    assert st["status"] == "awaiting_human" and st["gate"]["step"] == "brief"


def test_answer_resumes_in_background(real, client):
    r, loop = real()
    rid = new_run(client)
    r.wait_idle()
    resp = answer(client, rid, "approve")
    assert resp.status_code == 202
    r.wait_idle()
    st = client.get(f"/api/runs/{rid}").json()
    assert st["status"] == "awaiting_human" and st["gate"]["step"] == "ideas"
    assert len(loop.calls) == 2


@pytest.mark.parametrize("script,status", [(["done"], "done"), (["error"], "error"), (["weird"], "error")])
def test_status_mapping(real, client, script, status):
    r, loop = real(script)
    rid = new_run(client)
    r.wait_idle()
    assert client.get(f"/api/runs/{rid}").json()["status"] == status


def test_exception_is_safe_error(real, client, monkeypatch):
    monkeypatch.setenv("META_MODEL_API_KEY", "sk-SECRETSECRET123456")
    r, _ = real([RuntimeError("Traceback ... Bearer sk-SECRETSECRET123456 failed")])
    rid = new_run(client)
    r.wait_idle()
    st = client.get(f"/api/runs/{rid}").json()
    assert st["status"] == "error"
    log = json.dumps(client.get(f"/api/runs/{rid}/log").json())
    assert "SECRETSECRET" not in log and "Traceback" not in log and "RuntimeError" not in log
    assert "Resume" in log
    sse = "".join(web._sse_frames(events.list_events(rid), 0).__repr__())
    assert "SECRETSECRET" not in sse


def test_exception_not_logged_with_key(real, client, monkeypatch, caplog):
    monkeypatch.setenv("META_MODEL_API_KEY", "sk-SECRETSECRET123456")
    r, _ = real([RuntimeError("bad sk-SECRETSECRET123456")])
    new_run(client)
    r.wait_idle()
    assert "SECRETSECRET" not in caplog.text


def test_resume_after_restart(real, client, env):
    r, loop = real()
    rid = new_run(client)
    r.wait_idle()
    answer(client, rid, "approve")
    r.wait_idle()
    # simulate a crash while working: status left 'running' with no thread
    runner_mod.set_status(rid, "running")
    web._startup()  # what a server start does
    assert client.get(f"/api/runs/{rid}").json()["status"] == "interrupted"
    # resume: pending gate exists -> straight back to awaiting_human without a new advance
    n = len(loop.calls)
    assert client.post(f"/api/runs/{rid}/resume").status_code == 202
    r.wait_idle()
    st = client.get(f"/api/runs/{rid}").json()
    assert st["status"] == "awaiting_human"
    assert len(loop.calls) == n + 1  # the loop itself reports the pending gate


def test_resume_not_resumable(real, client):
    r, _ = real()
    rid = new_run(client)
    r.wait_idle()
    resp = client.post(f"/api/runs/{rid}/resume")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "not_resumable"
    assert client.post("/api/runs/00000000-0000-0000-0000-000000000000/resume").status_code == 404


def test_one_advance_per_run(real, client):
    r, loop = real()
    loop.release = threading.Event()
    rid = new_run(client)
    loop.entered.wait(5)
    r.resume(rid)  # second submit while the first is in flight
    r.resume(rid)
    loop.release.set()
    assert r.wait_idle()
    assert len(loop.calls) == 1


def test_global_one_running_run(real, client):
    r, loop = real()
    loop.release = threading.Event()
    rid = new_run(client)
    loop.entered.wait(5)
    resp = client.post("/api/runs", json={"client_id": "dharaa"})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "busy"
    loop.release.set()
    r.wait_idle()
    # a second run exists and is running -> answering the first is refused with busy
    loop.release = threading.Event()
    rid2 = new_run(client)
    loop.entered.clear()
    loop.entered.wait(5)
    resp = answer(client, rid, "approve")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "busy"
    loop.release.set()
    r.wait_idle()


def test_cap_blocks_resume_and_answer(real, client, monkeypatch):
    r, loop = real()
    rid = new_run(client)
    r.wait_idle()
    monkeypatch.setenv("STUDIO_RUN_CAP_USD", "0.10")
    events.record_cost(rid, "muse-image", 0.5)
    resp = answer(client, rid, "approve")
    assert resp.status_code == 402 and resp.json()["error"]["code"] == "cost_cap"
    assert len(loop.calls) == 1  # nothing advanced


def test_cap_checked_inside_worker(real, env):
    r, loop = real()
    rid = events.create_run("dharaa", run_id="11111111-1111-4111-8111-111111111111")
    import os
    os.environ["STUDIO_RUN_CAP_USD"] = "0.10"
    try:
        events.record_cost(rid, "x", 0.5)
        r.start(rid, "dharaa", None)
        r.wait_idle()
    finally:
        del os.environ["STUDIO_RUN_CAP_USD"]
    assert runner_mod.get_status(rid) == "error"
    assert loop.calls == []


def test_cancel_stops_advance_result(real, client):
    r, loop = real()
    loop.release = threading.Event()
    rid = new_run(client)
    loop.entered.wait(5)
    assert client.post(f"/api/runs/{rid}/cancel").status_code == 202
    loop.release.set()
    r.wait_idle()
    st = client.get(f"/api/runs/{rid}").json()
    assert st["status"] == "cancelled" and st["gate"] is None


def test_make_runner_real_and_auto(env, monkeypatch):
    monkeypatch.setenv("STUDIO_RUNNER", "real")
    monkeypatch.delenv("STUDIO_AGENT", raising=False)
    assert isinstance(runner_mod.make_runner(), runner_mod.RealRunner)
    import os
    assert os.environ["STUDIO_AGENT"] == "wonder"
    monkeypatch.setenv("STUDIO_AGENT", "scripted")
    runner_mod.make_runner()
    assert os.environ["STUDIO_AGENT"] == "scripted"


def test_client_seed_swap(env, monkeypatch, tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "approved.jsonl").write_text(json.dumps(
        {"id": "m_1", "kind": "hard_rule", "text": "No hard sell.", "source": "human", "status": "approved"}) + "\n")
    monkeypatch.setenv("STUDIO_CLIENT_ID", "standin")
    monkeypatch.setenv("STUDIO_CLIENT_NAME", "Stand-in Co")
    monkeypatch.setenv("STUDIO_MEMORY_SEED", str(seed))
    from fastapi.testclient import TestClient
    from .helpers import PASSWORD
    with TestClient(web.app) as c:
        c.post("/api/login", json={"password": PASSWORD})
        assert {"id": "standin", "name": "Stand-in Co"} in c.get("/api/clients").json()
        mem = c.get("/api/clients/standin/memory").json()
        assert [m["text"] for m in mem] == ["No hard sell."]
        # a human edit must survive a restart (seed is not re-applied over existing memory)
        c.post("/api/clients/standin/memory", json={"kind": "preference", "text": "Short hooks"})
    with TestClient(web.app) as c:
        c.post("/api/login", json={"password": PASSWORD})
        assert len(c.get("/api/clients/standin/memory").json()) == 2

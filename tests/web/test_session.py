import asyncio
import json

from .helpers import answer, new_run


def test_full_scripted_session(client):
    rid = new_run(client)
    s = client.get(f"/api/runs/{rid}").json()
    assert s["status"] == "awaiting_human" and s["current_step"] == "brief"
    assert s["gate"]["card"]["type"] == "brief"
    assert [x["state"] for x in s["steps"]] == ["current"] + ["pending"] * 5

    # brief: steer -> revised brief, same step
    r = answer(client, rid, "steer", text="make it warmer")
    assert r.status_code == 202 and r.json() == {"status": "accepted"}
    s = client.get(f"/api/runs/{rid}").json()
    assert s["current_step"] == "brief" and "warmer" in s["gate"]["card"]["text"]
    # brief: edit
    assert answer(client, rid, "edit", fields={"text": "My own brief"}).status_code == 202
    assert client.get(f"/api/runs/{rid}").json()["gate"]["card"]["text"] == "My own brief"
    assert answer(client, rid).status_code == 202

    s = client.get(f"/api/runs/{rid}").json()
    assert s["current_step"] == "ideas" and len(s["gate"]["card"]["concepts"]) == 3
    assert s["artifacts"]["concepts"] and s["steps"][0]["state"] == "done"
    # approve without a valid choice is rejected, with a valid one accepted
    assert answer(client, rid, choice="zzz").status_code == 422
    assert answer(client, rid, choice="c2").status_code == 202

    s = client.get(f"/api/runs/{rid}").json()
    assert s["current_step"] == "preview" and len(s["artifacts"]["frames"]) == 3
    for u in s["artifacts"]["frames"]:
        assert client.get(u).status_code == 200
    assert answer(client, rid).status_code == 202

    s = client.get(f"/api/runs/{rid}").json()
    assert s["current_step"] == "edit" and set(s["artifacts"]["cuts"]) == {"9x16", "1x1", "16x9"}
    assert answer(client, rid, "edit", fields={"hook": "New hook", "captions": [{"text": "Cap A"}]}).status_code == 202
    s = client.get(f"/api/runs/{rid}").json()
    assert s["gate"]["card"]["hook"] == "New hook" and s["artifacts"]["captions"][0]["text"] == "Cap A"
    assert answer(client, rid).status_code == 202

    s = client.get(f"/api/runs/{rid}").json()
    assert s["current_step"] == "check" and s["artifacts"]["qc"]["pass"] is False
    assert answer(client, rid, "steer", text="apply fixes").status_code == 202
    s = client.get(f"/api/runs/{rid}").json()
    assert s["artifacts"]["qc"]["pass"] is True and len(s["gate"]["card"]["history"]) == 2
    assert answer(client, rid).status_code == 202

    s = client.get(f"/api/runs/{rid}").json()
    assert s["current_step"] == "ship" and s["artifacts"]["zip"]
    assert client.get(s["artifacts"]["zip"]).headers["content-type"] == "application/zip"
    assert answer(client, rid).status_code == 202

    s = client.get(f"/api/runs/{rid}").json()
    assert s["status"] == "done" and s["gate"] is None
    assert all(x["state"] == "done" for x in s["steps"])
    assert s["cost"]["usd"] > 0 and "muse-spark" in s["cost"]["by_tool"]
    assert any(x["run_id"] == rid and x["status"] == "done" for x in client.get("/api/runs").json())

    log = client.get(f"/api/runs/{rid}/log").json()
    assert len(log) > 10
    assert {"seq", "ts", "step", "kind", "actor", "text", "cost_usd", "memory_ids"} <= set(log[0])
    assert [r["seq"] for r in log] == sorted(r["seq"] for r in log)
    assert any(r["actor"] == "human" and "Steered" in r["text"] for r in log)
    assert any(r["cost_usd"] for r in log)


def test_back_marks_later_steps_stale_and_replays(client):
    rid = new_run(client)
    answer(client, rid); answer(client, rid, choice="c1"); answer(client, rid)   # now at edit
    assert client.get(f"/api/runs/{rid}").json()["current_step"] == "edit"
    r = answer(client, rid, "back", to="ideas")
    assert r.status_code == 202
    s = client.get(f"/api/runs/{rid}").json()
    assert s["current_step"] == "ideas" and s["gate"]["card"]["type"] == "ideas"
    states = {x["name"]: x["state"] for x in s["steps"]}
    assert states["brief"] == "done" and states["ideas"] == "current"
    assert states["preview"] in ("stale", "pending")
    assert answer(client, rid, choice="c3").status_code == 202       # re-approve works


def test_back_from_first_step_and_bad_target_422(client):
    rid = new_run(client)
    assert answer(client, rid, "back").status_code == 422
    answer(client, rid)
    assert answer(client, rid, "back", to="check").status_code == 422
    assert answer(client, rid, "back", to="nonsense").status_code == 422


def test_invalid_action_and_missing_text_422(client):
    rid = new_run(client)
    gid = client.get(f"/api/runs/{rid}").json()["gate"]["id"]
    assert client.post(f"/api/runs/{rid}/answer", json={"gate": gid, "action": "yolo"}).status_code == 422
    assert client.post(f"/api/runs/{rid}/answer", json={"gate": gid, "action": "steer"}).status_code == 422
    assert client.post(f"/api/runs/{rid}/answer", json={"gate": gid, "action": "edit"}).status_code == 422
    assert client.get(f"/api/runs/{rid}").json()["gate"]["id"] == gid   # untouched


def test_409_on_non_pending_gate(client):
    rid = new_run(client)
    old = client.get(f"/api/runs/{rid}").json()["gate"]["id"]
    assert answer(client, rid).status_code == 202
    r = client.post(f"/api/runs/{rid}/answer", json={"gate": old, "action": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "gate_not_pending"
    r = client.post(f"/api/runs/{rid}/answer", json={"gate": 99999, "action": "approve"})
    assert r.status_code == 409


def test_gate_of_other_run_is_409(client):
    a = new_run(client)
    other = client.get(f"/api/runs/{a}").json()["gate"]["id"]
    client.post(f"/api/runs/{a}/cancel")
    b = new_run(client)
    r = client.post(f"/api/runs/{b}/answer", json={"gate": other, "action": "approve"})
    assert r.status_code == 409


def test_cancel_blocks_answers(client):
    rid = new_run(client)
    assert client.post(f"/api/runs/{rid}/cancel").status_code == 202
    s = client.get(f"/api/runs/{rid}").json()
    assert s["status"] == "cancelled" and s["gate"] is None
    gid = 1
    assert client.post(f"/api/runs/{rid}/answer", json={"gate": gid, "action": "approve"}).status_code == 409


def test_unknown_run_404_with_error_shape(client):
    r = client.get("/api/runs/00000000-0000-4000-8000-000000000000")
    assert r.status_code == 404 and r.json()["error"]["code"] == "run_not_found"


def test_unknown_client_404(client):
    assert client.post("/api/runs", json={"client_id": "nobody"}).status_code == 404


def test_paid_tool_gate_enforced_by_code(client, monkeypatch):
    """If the runner tried to build paid artifacts without approval, the gate blocks it (409 gate_blocked)."""
    from adfarming import runner, web
    orig = runner.StubRunner._produce

    def sneaky(self, run_id, client_id, step, ans, steer, edited=None):
        if step == "ideas":
            return orig(self, run_id, client_id, "preview", ans, steer, edited)  # skip ideas approval
        return orig(self, run_id, client_id, step, ans, steer, edited)
    monkeypatch.setattr(runner.StubRunner, "_produce", sneaky)
    rid = new_run(client)
    r = answer(client, rid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "gate_blocked"
    assert client.get(f"/api/runs/{rid}").json()["status"] == "error"
    assert not (web.runner_mod.run_dir(rid) / "f1.webp").exists()


def test_cost_cap_run_402(client, monkeypatch):
    rid = new_run(client)
    monkeypatch.setenv("STUDIO_RUN_CAP_USD", "0.001")
    r = answer(client, rid)
    assert r.status_code == 402 and r.json()["error"]["code"] == "cost_cap"


def test_cost_cap_daily_402_on_create(client, monkeypatch):
    new_run(client)
    monkeypatch.setenv("STUDIO_DAILY_CAP_USD", "0.001")
    r = client.post("/api/runs", json={"client_id": "dharaa"})
    assert r.status_code == 402 and r.json()["error"]["code"] == "cost_cap"


def test_cost_cap_stops_paid_work_midway(client, monkeypatch):
    rid = new_run(client)
    answer(client, rid)                             # brief -> ideas (cost so far ~0.04)
    monkeypatch.setenv("STUDIO_RUN_CAP_USD", "0.10")
    answer(client, rid, choice="c1")                # preview would cost 0.12 -> over
    s = client.get(f"/api/runs/{rid}").json()
    assert s["status"] == "error" and s["cost"]["usd"] <= 0.10


def test_session_run_limit(client):
    for _ in range(5):
        rid = new_run(client)
        client.post(f"/api/runs/{rid}/cancel")
    r = client.post("/api/runs", json={"client_id": "dharaa"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "session_run_limit"


def test_one_running_run_at_a_time(client):
    from adfarming import runner
    rid = new_run(client)
    runner.set_status(rid, "running")
    r = client.post("/api/runs", json={"client_id": "dharaa"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "busy"


def test_startup_marks_running_runs_interrupted(env):
    from fastapi.testclient import TestClient
    from adfarming import runner, web
    from adfarming.studio import events
    with TestClient(web.app) as c:
        c.post("/api/login", json={"password": "pw-test"})
        rid = new_run(c)
        runner.set_status(rid, "running")   # simulate crash mid-work
    with TestClient(web.app) as c2:         # "restart"
        c2.post("/api/login", json={"password": "pw-test"})
        assert c2.get(f"/api/runs/{rid}").json()["status"] == "interrupted"
        assert events.rebuild_run_state(rid)["client_id"] == "dharaa"


def test_log_shows_memory_ids_used(client):
    c = client
    mid = c.post("/api/clients/dharaa/memory", json={"kind": "hard_rule", "text": "Never show prices"}).json()["id"]
    rid = new_run(c)
    log = c.get(f"/api/runs/{rid}/log").json()
    assert any(mid in r["memory_ids"] for r in log if r["kind"] == "memory_read")
    brief_gate = [r for r in log if r["kind"] == "gate_requested"][0]
    assert mid in brief_gate["memory_ids"]


def test_sse_generator_and_last_event_id(client):
    from adfarming import web
    rid = new_run(client)
    answer(client, rid)

    async def collect(last, **kw):
        out = []
        async for chunk in web.sse_stream(rid, last, poll_s=0.01, max_seconds=0.15, **kw):
            out.append(chunk)
        return "".join(out)

    full = asyncio.run(collect(0, heartbeat_s=100))
    assert "event: card_ready" in full and "event: cost_update" in full and "event: run_status" in full
    assert '"cap_usd": 1.5' in full
    ids = [int(l[4:]) for l in full.splitlines() if l.startswith("id: ")]
    assert ids == sorted(ids) and len(ids) > 5
    resumed = asyncio.run(collect(ids[-3], heartbeat_s=100))
    assert [int(l[4:]) for l in resumed.splitlines() if l.startswith("id: ")] == ids[-2:]
    hb = asyncio.run(collect(ids[-1], heartbeat_s=0.05))
    assert ": heartbeat" in hb and "event:" not in hb


def test_sse_route_streams_with_header(client):
    rid = new_run(client)
    with client.stream("GET", f"/api/runs/{rid}/events", headers={"Last-Event-ID": "0"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
            if "card_ready" in buf:
                break
    assert "event: card_ready" in buf


def test_sse_requires_auth(client):
    rid = new_run(client)
    client.cookies.clear()
    assert client.get(f"/api/runs/{rid}/events").status_code == 401

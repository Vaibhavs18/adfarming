PASSWORD = "pw-test"
MP4_BYTES = bytes(range(256)) * 8


def new_run(client, goal="Launch the new range"):
    r = client.post("/api/runs", json={"client_id": "dharaa", "goal": goal})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def answer(client, rid, action="approve", **kw):
    gate = client.get(f"/api/runs/{rid}").json()["gate"]
    return client.post(f"/api/runs/{rid}/answer", json={"gate": gate["id"], "action": action, **kw})

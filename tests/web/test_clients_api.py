import json

import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clientkit import make_client  # noqa: E402
from .helpers import answer


@pytest.fixture
def two(env):
    priv = env / "private"
    a = make_client(priv, "alpha", "Alpha Tea", "ALPHA BRIEF: tea for office workers.\n\nmore", [("a.mp4", 1)], asr=False)
    b = make_client(priv, "beta", "Beta Shoes", "BETA BRIEF: shoes for runners.", [("b.mp4", 1)], asr=False)
    (a / "rules.md").write_text("DO: show the cup\nDON'T: promise cures\n")
    items = [{"id": "s1", "kind": "brand_voice", "text": "Beta voice is energetic.", "source": "human",
              "status": "approved"}]
    (b / "seed_memory.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
    return priv


def test_clients_listed_and_seeded(two, anon):
    anon.post("/api/login", json={"password": "pw-test"})
    cl = anon.get("/api/clients").json()
    assert cl == [{"id": "alpha", "name": "Alpha Tea"}, {"id": "beta", "name": "Beta Shoes"}]
    a = [i["text"] for i in anon.get("/api/clients/alpha/memory").json()]
    b = [i["text"] for i in anon.get("/api/clients/beta/memory").json()]
    assert a == ["show the cup", "Never: promise cures"] and b == ["Beta voice is energetic."]


def test_seeding_is_idempotent_and_never_overwrites_edits(two, env, monkeypatch):
    from fastapi.testclient import TestClient
    from adfarming.web import app
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "pw-test"})
        mid = c.get("/api/clients/beta/memory").json()[0]["id"]
        c.patch(f"/api/clients/beta/memory/{mid}", json={"text": "Edited in UI"})
    with TestClient(app) as c:  # restart
        c.post("/api/login", json={"password": "pw-test"})
        texts = {i["text"]: i["status"] for i in c.get("/api/clients/beta/memory?status=all").json()}
        assert texts["Edited in UI"] == "approved" and texts["Beta voice is energetic."] == "superseded"
        assert len(texts) == 2
        assert len(c.get("/api/clients/alpha/memory").json()) == 2


def test_run_per_client_uses_own_brief_and_validates_client(two, client):
    assert client.post("/api/runs", json={"client_id": "ghost"}).status_code == 404
    assert client.post("/api/runs", json={"client_id": "../alpha"}).status_code == 404
    got = {}
    for cid in ("alpha", "beta"):
        r = client.post("/api/runs", json={"client_id": cid})
        assert r.status_code == 200, r.text
        s = client.get(f"/api/runs/{r.json()['run_id']}").json()
        assert s["client_id"] == cid
        got[cid] = s["gate"]["card"]["text"]
        mem = {m["text"] for m in s["memory_in_play"]}
        assert mem == ({"show the cup", "Never: promise cures"} if cid == "alpha" else {"Beta voice is energetic."})
        assert answer(client, r.json()["run_id"]).status_code == 202   # run proceeds
    assert got["alpha"].startswith("ALPHA BRIEF") and "BETA" not in got["alpha"]
    assert got["beta"].startswith("BETA BRIEF") and "ALPHA" not in got["beta"]


def test_flat_layout_lists_default_client(client):
    assert [c["id"] for c in client.get("/api/clients").json()] == ["dharaa"]
    r = client.post("/api/runs", json={"client_id": "dharaa", "goal": "G"})
    assert r.status_code == 200


def test_client_files_not_served_as_artifacts(two, client, env):
    from adfarming import web
    from pathlib import Path
    assert web._is_protected((env / "private" / "clients" / "alpha" / "brief.md").resolve())
    assert web._is_protected((env / "private" / "clients" / "alpha" / "footage" / "a.mp4").resolve())

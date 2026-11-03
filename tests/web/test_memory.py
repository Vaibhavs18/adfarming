from .helpers import answer, new_run


def test_add_list_patch_supersedes(client):
    m = client.post("/api/clients/dharaa/memory",
                    json={"kind": "brand_voice", "text": "Warm tone", "pinned": True}).json()
    assert m["status"] == "approved" and m["source"] == "human" and m["pinned"] is True
    assert set(m) == {"id", "kind", "text", "source", "status", "pinned", "evidence", "created_at"}
    n = client.patch(f"/api/clients/dharaa/memory/{m['id']}", json={"text": "Warm, honest tone"}).json()
    assert n["id"] != m["id"] and n["status"] == "approved" and n["text"] == "Warm, honest tone"
    approved = {i["id"] for i in client.get("/api/clients/dharaa/memory?status=approved").json()}
    assert n["id"] in approved and m["id"] not in approved
    allitems = {i["id"]: i["status"] for i in client.get("/api/clients/dharaa/memory?status=all").json()}
    assert allitems[m["id"]] == "superseded"


def test_bad_kind_and_unknown_items(client):
    assert client.post("/api/clients/dharaa/memory", json={"kind": "x", "text": "t"}).status_code == 422
    assert client.post("/api/clients/dharaa/memory", json={"kind": "asset", "text": " "}).status_code == 422
    assert client.get("/api/clients/nobody/memory").status_code == 404
    assert client.patch("/api/clients/dharaa/memory/m_nope", json={"text": "x"}).status_code == 404
    assert client.get("/api/clients/dharaa/memory?status=weird").status_code == 422


def test_steer_proposal_accept_appears_in_memory_in_play(client):
    rid = new_run(client)
    assert answer(client, rid, "steer", text="Always mention free shipping").status_code == 202
    prop = [i for i in client.get("/api/clients/dharaa/memory?status=proposed").json()
            if "free shipping" in i["text"]][0]
    assert prop["source"] == "agent_inferred"
    assert prop["id"] not in {m["id"] for m in client.get(f"/api/runs/{rid}").json()["memory_in_play"]}
    d = client.post(f"/api/clients/dharaa/memory/{prop['id']}/decide", json={"decision": "accept", "note": "yes"})
    assert d.status_code == 200 and d.json()["status"] == "approved"
    assert prop["id"] in {m["id"] for m in client.get(f"/api/runs/{rid}").json()["memory_in_play"]}


def test_reject_never_enters_memory_in_play(client):
    rid = new_run(client)
    answer(client, rid, "steer", text="Use more emojis")
    prop = client.get("/api/clients/dharaa/memory?status=proposed").json()[0]
    assert client.post(f"/api/clients/dharaa/memory/{prop['id']}/decide", json={"decision": "reject"}).json()["status"] == "rejected"
    assert prop["id"] not in {m["id"] for m in client.get(f"/api/runs/{rid}").json()["memory_in_play"]}
    # deciding twice -> 409; invalid decision -> 422
    assert client.post(f"/api/clients/dharaa/memory/{prop['id']}/decide", json={"decision": "accept"}).status_code == 409
    assert client.post(f"/api/clients/dharaa/memory/{prop['id']}/decide", json={"decision": "maybe"}).status_code == 422


def test_patch_proposed_creates_approved_human_row(client):
    rid = new_run(client)
    answer(client, rid, "steer", text="Shorter intros")
    prop = client.get("/api/clients/dharaa/memory?status=proposed").json()[0]
    n = client.patch(f"/api/clients/dharaa/memory/{prop['id']}", json={"text": "Intros under 2s"}).json()
    assert n["status"] == "approved" and n["source"] == "human"
    assert n["id"] in {m["id"] for m in client.get(f"/api/runs/{rid}").json()["memory_in_play"]}


def test_ship_card_lists_memory_proposals(client):
    rid = new_run(client)
    answer(client, rid); answer(client, rid, choice="c1"); answer(client, rid); answer(client, rid)
    answer(client, rid, "steer", text="fix it")
    answer(client, rid)
    card = client.get(f"/api/runs/{rid}").json()["gate"]["card"]
    assert card["type"] == "ship" and card["memory_proposals"]
    assert card["memory_proposals"][0]["status"] == "proposed"

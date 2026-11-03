from pathlib import Path

import pytest

from adfarming.studio import events, gates, memory

SEED = Path(__file__).resolve().parents[2] / "private/research/seed_memory_dharaa"


@pytest.mark.skipif(not SEED.exists(), reason="private seed not present")
def test_seed_dharaa():
    assert memory.load_seed("dharaa", SEED) == 12
    assert memory.load_seed("dharaa", SEED) == 0      # idempotent
    items = memory.read_memory("dharaa", 100000)
    assert len(items) == 12
    assert all(i["kind"] == "hard_rule" for i in items[:4])


def test_proposed_not_injected_until_approved():
    memory.add_human_memory("c", "brand_voice", "Warm tone")
    res = memory.propose_memory("c", "preference", "Avoid Diwali framing")
    assert [i["text"] for i in memory.read_memory("c")] == ["Warm tone"]
    memory.decide_memory(res["item_id"], "accept")
    assert {i["text"] for i in memory.read_memory("c")} == {"Warm tone", "Avoid Diwali framing"}


def test_dismissed_not_reproposed():
    res = memory.propose_memory("c", "preference", "No  Diwali!")
    memory.decide_memory(res["item_id"], "reject", note="nope")
    again = memory.propose_memory("c", "preference", "no diwali")
    assert again["status"] == "previously_dismissed"
    assert memory.list_memory("c", "proposed") == []
    assert memory.propose_memory("other", "preference", "no diwali")["status"] == "proposed"


def test_dismissed_steer_not_reproposed():
    r = events.create_run("c")
    g = gates.request_gate(r, "ideas")
    out = gates.answer_gate(g, "steer", text="Shorter hook")
    memory.decide_memory(out["memory_item"]["item_id"], "reject")
    gates.propose_artifact(r, "ideas", "concepts")
    g2 = gates.request_gate(r, "ideas")
    out2 = gates.answer_gate(g2, "steer", text="shorter hook")
    assert out2["memory_item"]["status"] == "previously_dismissed"
    assert [e["payload"]["text"] for e in events.list_events(r) if e["kind"] == "steer"] == \
        ["Shorter hook", "shorter hook"]


def test_budget_truncation_keeps_hard_rules():
    for i in range(5):
        memory.add_human_memory("c", "preference", f"pref {i} " + "x" * 200)
    hr = [memory.add_human_memory("c", "hard_rule", f"rule {i} " + "y" * 100) for i in range(3)]
    out = memory.read_memory("c", budget_chars=250)
    texts = [i["text"] for i in out]
    assert all(any(t.startswith(f"rule {i}") for t in texts) for i in range(3))
    assert len(hr) == 3 and out[0]["kind"] == "hard_rule"
    assert sum(len(t) for t in texts) <= 3 * 107 + 250
    assert not any(i["kind"] == "preference" for i in out)   # none fit after the rules
    # generous budget: hard rules first, human before agent_inferred
    full = memory.read_memory("c", budget_chars=10000)
    assert [i["kind"] for i in full[:3]] == ["hard_rule"] * 3 and len(full) == 8


def test_edit_supersede_and_undo():
    p = memory.propose_memory("c", "preference", "Short hooks")
    res = memory.decide_memory(p["item_id"], "edit", text="Hooks under 2 seconds")
    assert [i["text"] for i in memory.read_memory("c")] == ["Hooks under 2 seconds"]
    new = memory.read_memory("c")[0]
    assert new["source"] == "human" and new["edited_from"] == "Short hooks"
    memory.supersede_memory(res["new_item_id"], "preference", "Hooks under 1.5 seconds")
    assert [i["text"] for i in memory.read_memory("c")] == ["Hooks under 1.5 seconds"]
    memory.undo_last("c")                              # undo the supersede
    assert [i["text"] for i in memory.read_memory("c")] == ["Hooks under 2 seconds"]
    memory.undo_last("c")                              # undo the edit-accept
    assert memory.read_memory("c") == []
    assert len(memory.list_memory("c")) == 3           # nothing hard-deleted


def test_proposal_cannot_claim_human_source():
    with pytest.raises(ValueError):
        memory.propose_memory("c", "preference", "x", source="human")


def test_read_memory_logs_event():
    r = events.create_run("c")
    iid = memory.add_human_memory("c", "hard_rule", "No hard sell")
    memory.read_memory("c", run_id=r)
    ev = [e for e in events.list_events(r) if e["kind"] == "memory_read"]
    assert ev[0]["payload"]["ids"] == [iid]

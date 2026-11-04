"""RunState / log expose the composed-look fields (stub runner builds the canned card, see API_CONTRACT.md)."""
from adfarming.studio import events

from .helpers import answer, new_run


def test_preview_state_carries_look_and_label(client):
    rid = new_run(client)
    answer(client, rid)                         # brief
    answer(client, rid, choice="c1")            # ideas -> preview card
    s = client.get(f"/api/runs/{rid}").json()
    card = s["gate"]["card"]
    assert card["type"] == "preview" and card["frames_kind"] == "composed"
    look = card["look"]
    assert look["plan_id"] == "plan.v1" and look["summary"] and "real speaker" in look["label"]
    assert look["editable"] == ["hook", "cta", "title", "callout", "preset"]
    assert s["artifacts"]["look"] == look and s["artifacts"]["frames_label"] == look["label"]
    answer(client, rid, "steer", text="calmer background")
    s2 = client.get(f"/api/runs/{rid}").json()
    assert s2["gate"]["card"]["look"]["plan_id"] == "plan.v2"


def test_simple_look_has_no_look_block(client, monkeypatch):
    monkeypatch.setenv("STUDIO_LOOK", "simple")
    rid = new_run(client)
    answer(client, rid)
    answer(client, rid, choice="c1")
    s = client.get(f"/api/runs/{rid}").json()
    assert s["gate"]["card"]["look"] is None and s["gate"]["card"]["frames_kind"] == "storyboard"
    assert s["artifacts"]["look"] is None


def test_fallback_event_is_in_the_plain_language_log(client):
    rid = new_run(client)
    events.append_event(rid, "fallback", "system", {"step": "edit", "from": "composed", "to": "simple",
                                                     "reason": "composed_render_failed"})
    rows = client.get(f"/api/runs/{rid}/log").json()
    assert any("Fell back from composed to simple" in r["text"] for r in rows)

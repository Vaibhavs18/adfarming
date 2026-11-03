import json

import pytest
import requests

from adfarming.agent import AgentLoop, audit_gate_violations, tool_schemas
from adfarming.agent.loop import ApiError, WonderClient
from adfarming.agent.tools import STEP_TOOLS, Toolbox
from adfarming.studio import events, gates, memory

from .conftest import FakeMedia, FakeWonder, HAPPY, human, loop_for, new_run, pending, resp, script_for


def kinds(run, kind):
    return [e for e in events.list_events(run) if e["kind"] == kind]


def tool_done(run, tool=None):
    return [e["payload"] for e in kinds(run, "tool_done") if tool is None or e["payload"]["tool"] == tool]


def full_session(run, loop, steer_at=None):
    """Drive a whole session; returns list of advance() results."""
    results = [loop.advance(run)]
    plan = [("approve", {}), ("approve", {"choice": "c2"}), ("approve", {}), ("approve", {}), ("approve", {}),
            ("approve", {})]
    for kind, payload in plan:
        assert results[-1]["status"] == "awaiting_human", results[-1]
        human(run, kind, **payload)
        results.append(loop.advance(run))
    return results


# (a) hostile model --------------------------------------------------------------------
def test_a_hostile_build_before_approval_is_blocked_logged_and_loop_continues():
    run = new_run()
    script = [[("read_memory", {})], [("ask_human", {"summary": "brief"})]]
    loop, client, media = loop_for(script_for("brief"))
    assert loop.advance(run)["status"] == "awaiting_human"
    human(run, "approve")
    hostile = [[("build_cuts", {}), ("storyboard", {})],              # skip-the-gate attempts
               [("ask_human", {"summary": "no concepts yet"})],        # missing artifact
               [("write_concepts", {})], [("ask_human", {"summary": "3 concepts"})]]
    loop2, client2, media2 = loop_for(hostile, media)
    r = loop2.advance(run)
    assert r["status"] == "awaiting_human" and r["fallbacks"] == 0
    att = kinds(run, "gate_blocked_attempt")
    assert {e["payload"]["tool"] for e in att} == {"build_cuts", "storyboard"}
    assert media.builds == 0 and media.frames_calls == 0
    assert audit_gate_violations(run)["violations"] == []
    errs = [d.get("error") for d in tool_done(run)]
    assert "gate_blocked" not in errs                                    # blocked before tool_started
    outs = json.dumps(client2.requests[1]["input"])                      # model was told, and went on
    assert "gate_blocked" in outs and "missing_artifact" in json.dumps(client2.requests[2]["input"])


def test_a2_paid_tool_in_same_batch_as_ask_human_blocked_even_when_approved():
    run = new_run()
    loop, client, media = loop_for(script_for("brief", "ideas"))
    loop.advance(run); human(run, "approve"); loop.advance(run); human(run, "approve", choice="c2")
    loop2, client2, media2 = loop_for([[("ask_human", {"summary": "x"}), ("storyboard", {})],
                                       [("storyboard", {})], [("ask_human", {"summary": "frames"})]], media)
    # ask_human fails (no frames yet) AND the paid tool shares its batch -> blocked
    loop2.advance(run)
    reasons = [e["payload"]["reason"] for e in kinds(run, "gate_blocked_attempt")]
    assert any("same batch" in r for r in reasons)
    assert media.frames_calls == 1


# (b) happy path --------------------------------------------------------------------------
def test_b_full_happy_path(env):
    run = new_run()
    loop, client, media = loop_for(script_for("brief", "ideas", "preview", "edit", "check", "ship", "done"))
    res = full_session(run, loop)
    assert [r["status"] for r in res] == ["awaiting_human"] * 6 + ["done"]
    assert res[-1]["fallbacks"] == 0 and client.script == []
    st = events.rebuild_run_state(run)
    assert sorted(st["step_approval"]) == sorted(events.STEPS)
    assert audit_gate_violations(run) == {"violations": [], "blocked_attempts": 0}
    assert (env / "out" / run / "package.zip").is_file() and (env / "out" / run / "c2_9x16.mp4").is_file()
    assert st["cost_usd"] > 0 and {"muse-wonder", "build_cuts", "storyboard"} <= set(st["cost_by_tool"])
    # cards were built by code, per API_CONTRACT
    cards = {e["payload"]["step"]: e["payload"]["payload"]["card"] for e in kinds(run, "gate_requested")}
    assert cards["brief"]["type"] == "brief" and cards["brief"]["text"]
    assert [c["id"] for c in cards["ideas"]["concepts"]] == ["c1", "c2", "c3"] and cards["ideas"]["recommended"] == "c2"
    assert cards["preview"]["frames"][0].startswith(f"/api/runs/{run}/artifacts/frame_c2_")
    assert set(cards["edit"]["cuts"]) == {"9x16", "1x1", "16x9"} and cards["edit"]["captions"]
    assert cards["check"]["verdict"]["pass"] is True and cards["ship"]["zip"].endswith("package.zip")
    assert any(e["payload"]["text"] for e in kinds(run, "commentary"))
    assert len(kinds(run, "step_turn")) == 7 and kinds(run, "run_mode")[0]["payload"]["mode"] == "wonder"
    assert st["artifacts"]["frames"]["status"] == "approved"
    assert kinds(run, "agent_memory_proposal")


# (c) steer ----------------------------------------------------------------------------------
def test_c_steer_reaches_write_concepts_and_creates_memory_item():
    run = new_run()
    media = FakeMedia()
    steer = "shorter hook, no Diwali year"
    loop, client, _ = loop_for(script_for("brief") + [[("write_concepts", {})], [("ask_human", {"summary": "3"})]], media)
    loop.advance(run); human(run, "approve"); loop.advance(run)
    before = [c["hook"] for c in Toolbox(run, media.backends()).concepts_list()]
    out = human(run, "steer", text=steer)
    assert out["memory_item"]["status"] == "proposed"
    assert [m["text"] for m in memory.list_memory("dharaa", "proposed")] == [steer]
    # hostile: model tries to treat the steer as approval first, then applies it
    loop2, client2, _ = loop_for([[("ask_human", {"summary": "approved?"})], [("write_concepts", {"guidance": "tighten"})],
                                  [("ask_human", {"summary": "revised", "recommendation": "c1"})]], media)
    assert loop2.advance(run)["status"] == "awaiting_human"
    assert any("steer_not_applied" in json.dumps(r["input"]) for r in client2.requests[1:2])
    assert steer in media.guidance[-1] and "tighten" in media.guidance[-1]
    after = [c["hook"] for c in Toolbox(run, media.backends()).concepts_list()]
    assert after != before and all("2019" not in h for h in after) and len(after[0]) < len(before[0])
    # the fresh step turn was told the steer verbatim and that a steer is not approval
    first = json.dumps(client2.requests[0]["input"])
    assert steer in first and "NOT approval" in first
    assert "previous_response_id" not in client2.requests[0]
    assert "ideas" not in events.rebuild_run_state(run)["step_approval"]


# (d) back -----------------------------------------------------------------------------------
def test_d_back_invalidates_later_artifacts():
    run = new_run()
    loop, client, media = loop_for(script_for("brief", "ideas", "preview", "edit", "check"))
    for i, (k, p) in enumerate([("approve", {}), ("approve", {"choice": "c2"}), ("approve", {}), ("approve", {})]):
        loop.advance(run); human(run, k, **p)
    assert loop.advance(run)["step"] == "check"
    human(run, "back", text=None, to="ideas")
    st = events.rebuild_run_state(run)
    assert {"frames", "cuts", "qc"} <= set(st["stale_artifacts"]) and "ideas" not in st["step_approval"]
    for tool in ("storyboard", "build_cuts"):
        with pytest.raises(gates.GateBlocked):
            gates.assert_approved(run, tool)
    loop2, client2, _ = loop_for([[("storyboard", {})], [("ask_human", {"summary": "ideas again"})]], media)
    assert loop2.advance(run)["step"] == "ideas"
    first = json.dumps(client2.requests[0]["input"])
    assert '"answer":"back"' in first.replace("\\", "").replace(" ", "") and "frames" in first
    assert media.frames_calls == 1                                       # storyboard never re-ran
    assert audit_gate_violations(run)["violations"] == [] and kinds(run, "gate_blocked_attempt")


# (e) restart --------------------------------------------------------------------------------
def test_e_restart_new_instance_continues_from_events_only():
    run = new_run()
    loop, client, media = loop_for(script_for("brief"))
    assert loop.advance(run)["status"] == "awaiting_human"
    loop_b, client_b, _ = loop_for([], FakeMedia())                      # brand-new process: empty script
    again = loop_b.advance(run)
    assert again["status"] == "awaiting_human" and client_b.requests == []   # re-shows the same gate, no model call
    human(run, "approve")
    loop_c, client_c, media_c = loop_for(script_for("ideas"), FakeMedia())
    assert loop_c.advance(run)["status"] == "awaiting_human"
    body = client_c.requests[0]
    assert "previous_response_id" not in body and "CURRENT STEP: ideas" in json.dumps(body["input"])
    assert "approved" in json.dumps(body["input"]) and "No hard-sell" in json.dumps(body["input"])


# (f) previous_response_id expiry --------------------------------------------------------------
def test_f_expired_previous_response_id_falls_back_to_stateless_replay():
    run = new_run()
    loop, client, _ = loop_for(script_for("brief"), expire_prev=True)
    r = loop.advance(run)
    assert r["status"] == "awaiting_human" and r["fallbacks"] == 0
    assert kinds(run, "chain_fallback")
    replay = client.requests[-1]
    assert "previous_response_id" not in replay and replay["store"] is False
    types = [i.get("type") for i in replay["input"]]
    assert types.count("function_call") == 1 and types.count("function_call_output") == 1
    assert "reasoning.encrypted_content" in replay["include"]


# (g) text-only ----------------------------------------------------------------------------------
def test_g_text_only_nudged_once_then_recovers():
    run = new_run()
    loop, client, _ = loop_for(["I think we should start.", [("ask_human", {"summary": "brief"})]])
    r = loop.advance(run)
    assert r["status"] == "awaiting_human" and r["fallbacks"] == 0
    assert "must be a tool call" in json.dumps(client.requests[1]["input"])


def test_g2_text_only_twice_falls_back_to_scripted_for_that_turn():
    run = new_run()
    loop, client, _ = loop_for(["hello", "still no tool"])
    r = loop.advance(run)
    assert r["status"] == "awaiting_human" and r["fallbacks"] == 1
    fb = kinds(run, "fallback")[0]["payload"]
    assert fb["reason"] == "text_only" and fb["to"] == "scripted"
    assert [e["payload"]["driver"] for e in kinds(run, "step_turn")] == ["wonder", "scripted"]


# (h) cost cap ------------------------------------------------------------------------------------
def test_h_cost_cap(monkeypatch):
    run = new_run()
    monkeypatch.setenv("STUDIO_RUN_CAP_USD", "0.12")
    loop, client, media = loop_for(script_for("brief", "ideas", "preview") + [[("build_cuts", {})]])
    for k, p in [("approve", {}), ("approve", {"choice": "c2"})]:
        loop.advance(run); human(run, k, **p)
    loop.advance(run); human(run, "approve")
    toolbox = Toolbox(run, media.backends())
    assert toolbox.current_step() == "edit"
    r = toolbox.call("build_cuts", {})
    assert not r.ok and r.error["code"] == "cost_cap" and media.builds == 0
    assert not [d for d in tool_done(run, "build_cuts")] and audit_gate_violations(run)["violations"] == []


# (i) scripted fallback ------------------------------------------------------------------------------
def test_i_scripted_mode_completes_a_session_with_a_steer(monkeypatch):
    monkeypatch.setenv("STUDIO_AGENT", "scripted")
    run = new_run()
    loop, client, media = loop_for([])
    loop.advance(run); human(run, "approve")
    assert loop.advance(run)["step"] == "ideas"
    human(run, "steer", text="shorter hook, no Diwali year")
    assert loop.advance(run)["status"] == "awaiting_human"
    assert "shorter hook, no Diwali year" in media.guidance[-1]
    for p in [{"choice": "c1"}, {}, {}, {}]:
        human(run, "approve", **p)
        assert loop.advance(run)["status"] == "awaiting_human"
    human(run, "approve")
    res = loop.advance(run)
    assert res["status"] == "done" and client.requests == []
    assert audit_gate_violations(run)["violations"] == []
    assert kinds(run, "run_mode")[0]["payload"]["mode"] == "scripted"
    assert any("year references" in m["text"] for m in memory.list_memory("dharaa", "proposed"))


def test_i2_wonder_down_everywhere_still_completes_via_fallback():
    run = new_run()
    loop, client, media = loop_for([], fail_all=500)
    res = loop.advance(run)
    assert res["status"] == "awaiting_human" and res["fallbacks"] == 1
    assert kinds(run, "fallback")[0]["payload"]["reason"] == "error"


# loop robustness ------------------------------------------------------------------------------------
def test_unknown_tool_bad_json_and_validation_errors_go_back_to_the_model():
    run = new_run()
    loop, client, _ = loop_for([[("nope", {}), ("read_memory", "{not json")], [("ask_human", {})],
                                [("ask_human", {"summary": "brief"})]])
    assert loop.advance(run)["status"] == "awaiting_human"
    t1 = json.dumps(client.requests[1]["input"])
    assert "unknown_tool" in t1 and "bad_arguments" in t1
    assert "validation" in json.dumps(client.requests[2]["input"])


def test_calls_run_in_order_and_extra_calls_are_refused():
    run = new_run()
    calls = [("read_memory", {})] * 16
    loop, client, _ = loop_for([calls, [("ask_human", {"summary": "brief"})]])
    loop.advance(run)
    out = client.requests[1]["input"]
    assert len(out) == 16 and "too_many_calls" in out[15]["output"] and "too_many_calls" not in out[13]["output"]


def test_incomplete_empty_output_is_retried_with_bigger_budget():
    run = new_run()
    inc = lambda body: {"id": "x", "status": "incomplete", "output": [{"type": "reasoning", "summary": []}],
                        "usage": {"input_tokens": 10, "output_tokens": 10}}
    loop, client, _ = loop_for([inc, [("ask_human", {"summary": "brief"})]])
    assert loop.advance(run)["status"] == "awaiting_human"
    assert client.requests[1]["max_output_tokens"] == 2 * client.requests[0]["max_output_tokens"] >= 12000


def test_turn_cap_per_step_falls_back():
    run = new_run()
    loop, client, _ = loop_for([[("read_memory", {})]] * 8)
    r = loop.advance(run)
    assert r["fallbacks"] == 1 and kinds(run, "fallback")[0]["payload"]["reason"] == "cap"
    assert r["status"] == "awaiting_human" and len(client.requests) == 8


def test_tools_offered_per_step_hide_paid_tools_early():
    for step in ("brief", "ideas"):
        names = {t["name"] for t in tool_schemas(step)}
        assert not names & {"storyboard", "build_cuts", "track_subject", "finish"}
    for step, names in STEP_TOOLS.items():
        for t in tool_schemas(step):
            assert t["type"] == "function" and set(t) == {"type", "name", "description", "parameters"}
            import re
            assert re.fullmatch(r"[a-zA-Z0-9_.-]+", t["name"])


def test_request_shape():
    run = new_run()
    loop, client, _ = loop_for(script_for("brief"))
    loop.advance(run)
    b0, b1 = client.requests
    assert b0["model"] == "muse-wonder-1.0" and b0["reasoning"] == {"effort": "low"} and b0["max_output_tokens"] >= 6000
    assert b0["tools"] and "previous_response_id" not in b0 and b1["previous_response_id"] == "resp_1"
    assert b1["tools"] and "RULES" in b1["instructions"]
    assert "include" not in b1 and b0["include"] == ["reasoning.encrypted_content"]   # API: no include with previous_response_id


def test_wonder_client_retries_429_and_5xx_with_backoff(monkeypatch):
    monkeypatch.setattr("adfarming.llm._hdr", lambda: {"Authorization": "Bearer test"})
    class R:
        def __init__(s, c, j=None): s.status_code, s._j, s.text = c, j, "err"
        def json(s): return s._j
    seq = [R(429), R(503), R(200, {"ok": 1})]
    sleeps = []
    c = WonderClient(post=lambda *a, **k: seq.pop(0), sleep=sleeps.append)
    assert c.create({}) == {"ok": 1} and sleeps == [1, 2]
    seq = [R(500), R(500), R(500)]
    with pytest.raises(ApiError) as e:
        WonderClient(post=lambda *a, **k: seq.pop(0), sleep=lambda s: None).create({})
    assert e.value.status == 500
    def boom(*a, **k): raise requests.ConnectionError("x")
    with pytest.raises(ApiError):
        WonderClient(post=boom, sleep=lambda s: None).create({})
    seq = [R(400)]
    with pytest.raises(ApiError):
        WonderClient(post=lambda *a, **k: seq.pop(0), sleep=lambda s: None).create({})
    assert len(seq) == 0


def test_edit_answer_is_applied_verbatim_and_marks_cuts_stale():
    run = new_run()
    loop, client, media = loop_for(script_for("brief", "ideas", "preview", "edit"))
    for k, p in [("approve", {}), ("approve", {"choice": "c2"}), ("approve", {})]:
        loop.advance(run); human(run, k, **p)
    loop.advance(run)
    human(run, "edit", fields={"hook": "My own hook", "cta": "Visit the site"})
    loop2, client2, _ = loop_for([[("ask_human", {"summary": "again"})], [("build_cuts", {})],
                                  [("ask_human", {"summary": "rebuilt"})]], media)
    loop2.advance(run)
    wc = Toolbox(run, media.backends()).working_concept()
    assert wc["hook"] == "My own hook" and wc["cta"] == "Visit the site" and wc["beats"][0]["on_screen_text"] == "My own hook"
    assert media.builds == 2 and "steer_not_applied" not in json.dumps(client2.requests[1]["input"])
    assert '"answer":"edited"' in json.dumps(client2.requests[0]["input"]).replace(" ", "").replace("\\", "")

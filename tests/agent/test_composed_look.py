"""The composed look through the agent: fake media / matte / plan backends, real gates, real compose_plan repair.

No network, synthetic data only. The one real-ffmpeg test (preview stills built from a generated clip) skips when
ffmpeg is missing."""
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageStat

from adfarming import compose_plan
from adfarming.agent import AgentLoop, audit_gate_violations, tool_schemas
from adfarming.agent import compose_tools as ctools
from adfarming.agent.tools import Backends, Toolbox
from adfarming.studio import events, gates

from .conftest import FakeMedia, FakeWonder, human, new_run, pending


@pytest.fixture(autouse=True)
def composed(monkeypatch):
    monkeypatch.setenv("STUDIO_LOOK", "composed")


def png(path, size=(8, 8), colour=(120, 40, 60)):
    Image.new("RGB", size, colour).save(path)


class ComposedMedia(FakeMedia):
    """FakeMedia plus fake composition backends. Records every call so tests can assert what was (not) paid for."""

    def __init__(self, textile=True, hook_override=None, callout=False, **kw):
        super().__init__(**kw)
        self.textile, self.hook_override, self.callout = textile, hook_override, callout
        self.plan_calls, self.plates, self.stills, self.mattes = [], 0, 0, 0
        self.renders, self.previews, self.derived = [], [], []
        self.fail = set()      # {"matte_still", "matte_video", "render", "plate"}

    def backends(self) -> Backends:
        b = super().backends()
        b._over.update(plan_composition=self.plan, make_plate=self.plate, matte_still=self.matte_still,
                       matte_video=self.matte_video, render=self.render, lint=lambda plan, assets: [],
                       grab_frame=self.grab, still_video=self.still_video, derive_ratio=self.derive)
        return b

    cta = None

    def generate(self, run, brief, clips, refs, attempts=3, guidance=""):
        cs = super().generate(run, brief, clips, refs, attempts, guidance)
        if self.cta:
            for c in cs:
                c["cta"] = self.cta
        return cs

    def plan(self, run, concept, brief, memory_text, assets_info, guidance="", look=None, **kw):
        self.plan_calls.append({"guidance": guidance, "look": look, "hook": concept["beats"][0]["on_screen_text"]})
        run.trace("muse-spark", {}, {}, decision="fake plan")
        p = compose_plan.default_plan(concept, look="cutout_on_textile_plate" if self.textile else look)
        if self.callout:
            p["text"].append({"id": "callout", "role": "callout", "text": "Pure cotton", "region": "right_mid", "font": "latin",
                              "size": 40, "style": "callout_tag", "fill": "#111111", "stroke": None, "plate": "#fbbf24",
                              "pill_text": None, "rot": -4, "animation": "pop", "start": 6.5, "end": 8.5, "beat": 1})
        if self.hook_override:
            for t in p["text"]:
                if t["role"] == "hook":
                    t["text"] = self.hook_override      # a model that ignores the human's locked headline
        return p

    def plate(self, run, name, prompt, colors, out_path):
        if "plate" in self.fail:
            raise RuntimeError("image api down")
        self.plates += 1
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        png(out_path)
        run.trace("muse-image", {"plate": name}, {}, cost_usd=0.01)
        return str(out_path)

    def grab(self, clip, t, out_png):
        png(out_png)
        return str(out_png)

    def matte_still(self, image, out_png, prompt="woman"):
        self.stills += 1
        if "matte_still" in self.fail:
            return {"error": "no match", "matte": None, "cost_usd": 0.0}
        png(out_png)
        return {"matte": str(out_png), "cost_usd": 0.0025, "head_top_frac": 0.2,
                "subject_bbox_frac": [0.2, 0.18, 0.8, 1.0]}

    def still_video(self, frames, out_path, fps=5):
        Path(out_path).write_bytes(b"v")
        return str(out_path)

    def matte_video(self, run, video, out_path, prompt="woman", cache_dir=None):
        self.mattes += 1
        if "matte_video" in self.fail:
            run.trace("sam-3.1-matte", {}, {"error": "boom"}, status="error")
            return {"error": "no match", "matte": None, "cost_usd": 0.0}
        Path(out_path).write_bytes(b"matte")
        run.trace("sam-3.1-matte", {}, {}, cost_usd=0.051)
        return {"matte": str(out_path), "cost_usd": 0.051, "head_top_frac": 0.2, "frames": 100,
                "subject_bbox_frac": [0.2, 0.18, 0.8, 1.0]}

    def render(self, plan, assets, out_path, preview=False, workdir=None, size=None):
        if size is not None:
            self.derived.append((size, plan))
            if "ratio" in self.fail:
                raise RuntimeError("1:1 layout exploded")
            if "ratio_unsupported" in self.fail:
                return {"unsupported": True, "reason": "no 16:9 layout"}
            Path(out_path).write_bytes(b"composed-" + f"{size[0]}x{size[1]}".encode())
            return {"out": str(out_path)}
        if "render" in self.fail:
            raise RuntimeError("ffmpeg exploded")
        if preview:
            d = Path(out_path)
            d.mkdir(parents=True, exist_ok=True)
            frames = []
            for i in range(len(plan["background"])):
                p = d / f"beat{i + 1:02d}.png"
                png(p)
                frames.append(str(p))
            png(d / "contact.png")
            self.previews.append(plan)
            return {"out": str(d / "contact.png"), "frames": frames}
        self.renders.append(plan)
        Path(out_path).write_bytes(b"composed-video")
        return {"out": str(out_path), "seconds": 1.0, "render_seconds": 0.9}

    def revise(self, run, brief, concept, verdict, clips, guidance=""):
        c = super().revise(run, brief, concept, verdict, clips, guidance)
        c["beats"][0]["src_out"] = c["beats"][0]["src_out"] - 1.0      # a content steer moves the cut
        return c

    def derive(self, src, ratio, crop_y, out_path):
        self.derived.append((ratio, crop_y))
        Path(out_path).write_bytes(b"derived-" + ratio.encode())
        return str(out_path)


COMPOSED_SCRIPT = {
    "brief": [[("read_memory", {})], [("ask_human", {"summary": "Festive saree ad, warm, no hard sell."})]],
    "ideas": [[("write_concepts", {})], [("ask_human", {"summary": "3 concepts", "recommendation": "c2"})]],
    "preview": [[("plan_composition", {})], [("preview_composition", {})], [("ask_human", {"summary": "look preview"})]],
    "edit": [[("build_cuts", {})], [("render_composition", {})], [("ask_human", {"summary": "composed cut"})]],
    "check": [[("ask_human", {"summary": "QC passed"})]],
    "ship": [[("ask_human", {"summary": "package"})]],
    "done": [[("finish", {"summary": "all done"})]],
}


def script(*steps):
    return [t for s in steps for t in COMPOSED_SCRIPT[s]]


def loop_for(turns, media=None, **kw):
    media = media or ComposedMedia()
    client = FakeWonder(turns, **kw)
    return AgentLoop(client=client, backends=media.backends()), client, media


def kinds(run, kind):
    return [e for e in events.list_events(run) if e["kind"] == kind]


def card(run):
    return pending(run)["payload"]["card"]


def to_preview(run, loop):
    """brief and ideas approved; leaves the run at the pending Preview card."""
    loop.advance(run); human(run, "approve")
    loop.advance(run); human(run, "approve", choice="c2")
    return loop.advance(run)


# ------------------------------------------------------------------ happy path + card shapes + costs
def test_happy_path_cards_costs_and_audit(env):
    run = new_run()
    loop, client, media = loop_for(script("brief", "ideas", "preview", "edit", "check", "ship", "done"))
    r = loop.advance(run)
    ideas_card = None
    for kind, payload in [("approve", {}), ("approve", {"choice": "c2"}), ("approve", {}), ("approve", {}), ("approve", {}),
                          ("approve", {})]:
        assert r["status"] == "awaiting_human", r
        c = card(run)
        if c["type"] == "ideas":
            ideas_card = c
        if c["type"] == "preview":
            prev = c
        if c["type"] == "edit":
            edit = c
        human(run, kind, **payload)
        r = loop.advance(run)
    assert r["status"] == "done" and r["fallbacks"] == 0 and client.script == []
    # Ideas card: estimate of the PREVIEW step, itemised (plan + matte stills + plates)
    whats = " | ".join(i["what"] for i in ideas_card["cost_items"])
    assert "composition plan" in whats and "SAM" in whats and "Muse Image" in whats
    assert ideas_card["cost_estimate_usd"] == pytest.approx(sum(i["usd"] for i in ideas_card["cost_items"]), abs=1e-3)
    # Preview card contract: composed stills of the real speaker + look block
    assert prev["frames_kind"] == "composed" and prev["frames"] and all(u.endswith(".png") for u in prev["frames"])
    assert prev["look"]["plan_id"] == "plan.v1" and prev["look"]["summary"] and "real speaker" in prev["look"]["label"]
    assert prev["look"]["editable"] == ["hook", "cta", "title", "callout", "preset"] and prev["frames_label"] == prev["look"]["label"]
    # ...its estimate is the EDIT step (SAM video + render + QC), itemised
    assert any("SAM video" in i["what"] for i in prev["cost_items"]) and prev["cost_estimate_usd"] > 0.05
    # Edit card: composed cuts + look block
    assert edit["look"]["mode"] == "composed" and set(edit["cuts"]) == {"9x16", "1x1", "16x9"}
    out = env / "out" / run
    assert (out / "c2_9x16.mp4").read_bytes() == b"composed-video"
    assert (out / "c2_simple_9x16.mp4").is_file() and json.loads((out / "meta.json").read_text())["look"] == "composed"
    assert [d[0] for d in media.derived] == [(1080, 1080), (1920, 1080)]          # one render per ratio
    assert (out / "c2_1x1.mp4").read_bytes() == b"composed-1080x1080" and edit["look"].get("ratios") in (None, {"9x16": "composed", "1x1": "composed", "16x9": "composed"})
    # the render got the speech-timed captions and the CTA hold
    plan = media.renders[0]
    assert plan["captions"]["chunks"] == [{"text": "namaste", "start": 0.0, "end": 1.5}]
    assert plan["duration"] == pytest.approx(12.0 + ctools.HOLD_S)
    assert next(t for t in plan["text"] if t["role"] == "cta")["end"] == plan["duration"]
    # costs recorded under their own names
    st = events.rebuild_run_state(run)
    assert {"muse-spark", "sam-3.1-still", "sam-3.1-matte", "ffmpeg-render", "muse-image", "muse-wonder"} <= set(st["cost_by_tool"])
    assert st["cost_by_tool"]["sam-3.1-matte"] == pytest.approx(0.051)
    assert audit_gate_violations(run) == {"violations": [], "blocked_attempts": 0}
    assert sorted(st["step_approval"]) == sorted(events.STEPS)


# ------------------------------------------------------------------ hostile model
def test_hostile_model_calls_paid_composition_tools_before_approval():
    run = new_run()
    media = ComposedMedia()
    loop, _, _ = loop_for(script("brief"), media)
    loop.advance(run); human(run, "approve")
    # ideas step, nothing approved yet: both gated tools are refused
    tb = Toolbox(run, media.backends())
    for name in ("backdrops", "preview_composition"):
        r = tb.call(name, {})
        assert not r.ok and r.error["code"] == "gate_blocked"
    loop2, _, _ = loop_for(script("ideas"), media)
    loop2.advance(run); human(run, "approve", choice="c2")
    tb = Toolbox(run, media.backends())
    assert tb.current_step() == "preview"
    r = tb.call("render_composition", {})                       # needs the PREVIEW approval, which is not there
    assert not r.ok and r.error["code"] == "gate_blocked"
    r = tb.call("preview_composition", {"decision": "x"})        # allowed gate, but no plan yet: refused with a hint
    assert not r.ok and r.error["code"] == "missing_artifact"
    assert media.plates == media.stills == media.mattes == 0 and not media.renders and not media.previews
    blocked = [e["payload"]["tool"] for e in kinds(run, "gate_blocked_attempt")]
    assert blocked == ["backdrops", "preview_composition", "render_composition"]
    assert audit_gate_violations(run)["violations"] == []


def test_paid_composition_tool_in_same_batch_as_ask_human_is_blocked():
    run = new_run()
    media = ComposedMedia()
    loop, _, _ = loop_for(script("brief", "ideas") + [[("plan_composition", {})],
                                                      [("ask_human", {"summary": "x"}), ("preview_composition", {})],
                                                      [("preview_composition", {})], [("ask_human", {"summary": "ok"})]], media)
    loop.advance(run); human(run, "approve"); loop.advance(run); human(run, "approve", choice="c2")
    loop.advance(run)
    reasons = [e["payload"]["reason"] for e in kinds(run, "gate_blocked_attempt")]
    assert any("same batch" in x for x in reasons) and media.stills > 0
    assert audit_gate_violations(run)["violations"] == []


# ------------------------------------------------------------------ steer, edit, back
def _preview_loop(extra, **mkw):
    run = new_run()
    media = ComposedMedia(**mkw)
    loop, client, _ = loop_for(script("brief", "ideas", "preview") + extra, media)
    to_preview(run, loop)
    return run, loop, client, media


def test_steer_makes_a_new_plan_version_with_the_steer_verbatim():
    run, loop, client, media = _preview_loop([[("plan_composition", {"guidance": "calmer background"})],
                                              [("preview_composition", {})],
                                              [("ask_human", {"summary": "calmer look"})]])
    assert card(run)["look"]["plan_id"] == "plan.v1"
    human(run, "steer", text="calmer background, no callout")
    r = loop.advance(run)
    assert r["status"] == "awaiting_human" and r["fallbacks"] == 0
    assert card(run)["look"]["plan_id"] == "plan.v2" and card(run)["frames"][0].endswith("_v2_1.png")
    assert "calmer background, no callout" in media.plan_calls[-1]["guidance"]      # verbatim, from the log too
    out = Toolbox(run, media.backends()).dir
    assert (out / "plan.v1.json").is_file() and (out / "plan.v2.json").is_file()      # versions are kept
    st = events.rebuild_run_state(run)
    assert st["artifacts"]["plan"]["ref"] == "plan.v2.json" and st["artifacts"]["plan"]["status"] != "stale"


def test_steer_not_applied_is_refused_then_applied():
    run, loop, client, media = _preview_loop([[("ask_human", {"summary": "again"})],      # ignores the steer
                                              [("plan_composition", {})], [("preview_composition", {})],
                                              [("ask_human", {"summary": "ok"})]])
    human(run, "steer", text="bigger headline")
    r = loop.advance(run)
    assert r["status"] == "awaiting_human"
    outs = json.dumps(client.requests)
    assert "steer_not_applied" in outs
    assert card(run)["look"]["plan_id"] == "plan.v2"


def test_human_headline_edit_is_locked_and_survives_replan():
    run, loop, client, media = _preview_loop([[("plan_composition", {"guidance": "bigger headline"})],
                                              [("preview_composition", {})], [("preview_composition", {})],
                                              [("ask_human", {"summary": "ok"})]], hook_override="MODEL HOOK", callout=True)
    human(run, "edit", fields={"headline": "Meri apni hook", "callout": "Handloom"})
    r = loop.advance(run)                                        # consume_answer applies the edit by code
    assert r["status"] == "awaiting_human"
    tb = Toolbox(run, media.backends())
    rec = tb.plan_rec()
    hook = next(t for t in rec["plan"]["text"] if t["role"] == "hook")["text"]
    assert hook == "Meri apni hook" and rec["version"] >= 2
    assert tb._read("human_locks.json")["c2"]["hook"] == "Meri apni hook" and tb.plan_locks()["callout"] == "Handloom"
    # the planner now ignores the locked headline (hook_override): code still wins
    human(run, "steer", text="bigger headline")
    r = loop.advance(run)
    assert r["status"] == "awaiting_human" and media.plan_calls[-1]["hook"] == "Meri apni hook"
    assert next(t for t in tb.plan_rec()["plan"]["text"] if t["role"] == "hook")["text"] == "Meri apni hook"
    assert next(t for t in media.previews[-1]["text"] if t["role"] == "hook")["text"] == "Meri apni hook"


def test_edit_callout_and_preset_by_code():
    run, loop, client, media = _preview_loop([[("preview_composition", {})], [("ask_human", {"summary": "ok"})]],
                                             textile=True)
    tb = Toolbox(run, media.backends())
    base = tb.plan_rec()["plan"]
    assert base["preset"] == "cutout_on_textile_plate"
    human(run, "edit", fields={"look_preset": "cutout_on_brand_colour", "callout": ""})
    r = loop.advance(run)
    assert r["status"] == "awaiting_human"
    plan = tb.plan_rec()["plan"]
    assert plan["preset"] == "cutout_on_brand_colour" and all(b["kind"] == "gradient" for b in plan["background"])
    assert not any(t["role"] == "callout" for t in plan["text"])
    ans = tb.last_answer()
    assert ans["answer"] == "edited" and {"preset", "callout"} <= set(ans["applied"])


def test_back_marks_plan_and_frames_stale():
    run, loop, client, media = _preview_loop([])
    st = events.rebuild_run_state(run)
    assert {"plan", "frames"} <= set(st["artifacts"]) and not st["stale_artifacts"]
    human(run, "back", to="ideas")
    st = events.rebuild_run_state(run)
    assert {"plan", "frames"} <= set(st["stale_artifacts"]) and "ideas" not in st["step_approval"]
    tb = Toolbox(run, media.backends())
    assert tb.plan_current(st) is None                                 # a stale plan is never reused


def test_back_from_edit_makes_cuts_stale_and_render_needs_a_new_approval():
    run = new_run()
    media = ComposedMedia()
    loop, client, _ = loop_for(script("brief", "ideas", "preview", "edit"), media)
    loop.advance(run)
    for kind, payload in [("approve", {}), ("approve", {"choice": "c2"}), ("approve", {})]:
        human(run, kind, **payload)
        loop.advance(run)
    assert card(run)["type"] == "edit"
    human(run, "back", to="preview")
    st = events.rebuild_run_state(run)
    assert {"cuts", "qc"} <= set(st["stale_artifacts"]) and "preview" not in st["step_approval"]
    tb = Toolbox(run, media.backends())
    r = tb.call("render_composition", {})
    assert not r.ok and r.error["code"] == "gate_blocked"            # Preview must be approved again
    assert audit_gate_violations(run)["violations"] == []


# ------------------------------------------------------------------ fallbacks
def test_preview_matte_failure_falls_back_to_the_storyboard():
    run = new_run()
    media = ComposedMedia()
    media.fail.add("matte_still")
    loop, _, _ = loop_for(script("brief", "ideas", "preview"), media)
    r = to_preview(run, loop)
    assert r["status"] == "awaiting_human"
    c = card(run)
    assert c["frames_kind"] == "storyboard" and c["look"] is None and "stand-ins" in c["frames_label"]
    assert media.frames_calls == 1
    fb = [e["payload"] for e in kinds(run, "fallback") if e["payload"].get("scope") == "look"]
    assert fb and fb[0]["from"] == "composed" and fb[0]["step"] == "preview"
    assert Toolbox(run, media.backends()).look() == "simple"
    assert tool_schemas("edit", "simple") and "render_composition" not in {t["name"] for t in tool_schemas("edit", "simple")}
    assert audit_gate_violations(run)["violations"] == []


def test_preview_render_exception_also_falls_back():
    run = new_run()
    media = ComposedMedia()
    media.fail.add("render")
    loop, _, _ = loop_for(script("brief", "ideas", "preview"), media)
    to_preview(run, loop)
    assert card(run)["frames_kind"] == "storyboard" and media.frames_calls == 1


def test_edit_composed_failure_falls_back_to_simple_cuts_and_session_continues(env):
    run = new_run()
    media = ComposedMedia()
    media.fail.add("matte_video")
    loop, client, _ = loop_for(script("brief", "ideas", "preview", "edit", "check"), media)
    loop.advance(run)
    for kind, payload in [("approve", {}), ("approve", {"choice": "c2"}), ("approve", {}), ("approve", {})]:
        human(run, kind, **payload)
        r = loop.advance(run)
    assert r["status"] == "awaiting_human"
    chk = card(run)
    assert chk["type"] == "check" and chk["look"]["mode"] == "simple_fallback"
    assert chk["cuts"]["9x16"].endswith("c2_9x16.mp4")
    out = env / "out" / run
    assert (out / "c2_9x16.mp4").read_bytes().startswith(b"video") and not media.renders     # the simple cut
    fb = [e["payload"] for e in kinds(run, "fallback") if e["payload"].get("step") == "edit"]
    assert fb and fb[0]["reason"] == "composed_render_failed"
    assert chk["verdict"]["score"] == 5            # QC ran on the simple 9:16
    assert audit_gate_violations(run)["violations"] == []


def test_failed_plate_degrades_to_gradient_and_is_not_fatal():
    run = new_run()
    media = ComposedMedia()
    media.fail.add("plate")
    loop, _, _ = loop_for(script("brief", "ideas", "preview"), media)
    to_preview(run, loop)
    c = card(run)
    assert c["frames_kind"] == "composed" and any("unavailable" in n for n in c["look"]["notes"])
    assert all(b["kind"] == "gradient" for b in media.previews[-1]["background"])


# ------------------------------------------------------------------ cost caps
def test_cost_cap_blocks_paid_composition_tools(monkeypatch):
    run = new_run()
    media = ComposedMedia()
    loop, _, _ = loop_for(script("brief", "ideas", "preview", "edit"), media)
    to_preview(run, loop)
    spent = events.rebuild_run_state(run)["cost_usd"]
    monkeypatch.setenv("STUDIO_RUN_CAP_USD", str(round(spent + 0.005, 4)))
    tb = Toolbox(run, media.backends())
    plates0, plans0 = media.plates, len(media.plan_calls)
    r = tb.call("backdrops", {})
    assert not r.ok and r.error["code"] == "cost_cap"
    r = tb.call("plan_composition", {})
    assert not r.ok and r.error["code"] == "cost_cap"
    assert media.plates == plates0 and len(media.plan_calls) == plans0
    human(run, "approve")
    monkeypatch.setenv("STUDIO_RUN_CAP_USD", str(round(spent + 0.02, 4)))
    tb = Toolbox(run, media.backends())
    assert tb.call("render_composition", {}).error["code"] in ("cost_cap", "missing_artifact")


def test_render_cost_cap_is_checked_before_sam(monkeypatch):
    run = new_run()
    media = ComposedMedia()
    loop, _, _ = loop_for(script("brief", "ideas", "preview", "edit"), media)
    loop.advance(run)
    for kind, payload in [("approve", {}), ("approve", {"choice": "c2"}), ("approve", {})]:
        human(run, kind, **payload)
        loop.advance(run)
    assert card(run)["type"] == "edit"
    # now a stale composed render: any new render is refused when the cap leaves no room, SAM is not called again
    n = media.mattes
    spent = events.rebuild_run_state(run)["cost_usd"]
    human(run, "edit", fields={"hook": "New hook"})
    monkeypatch.setenv("STUDIO_RUN_CAP_USD", str(round(spent + 0.01, 4)))
    tb = Toolbox(run, media.backends())
    r = tb.call("render_composition", {})
    assert not r.ok and r.error["code"] == "cost_cap" and media.mattes == n


def test_matte_still_budget_is_checked_inside_the_tool(monkeypatch):
    run = new_run()
    media = ComposedMedia(textile=False)
    loop, _, _ = loop_for(script("brief", "ideas"), media)
    loop.advance(run); human(run, "approve"); loop.advance(run); human(run, "approve", choice="c2")
    tb = Toolbox(run, media.backends())
    assert tb.call("plan_composition", {}).ok
    # bypass the call-level estimate so the in-tool check is what stops the spend
    monkeypatch.setattr(Toolbox, "est_tool", lambda self, name, st=None: 0.0)
    monkeypatch.setenv("STUDIO_RUN_CAP_USD", str(round(tb.spent() + 0.001, 4)))
    r = tb.call("preview_composition", {})
    assert not r.ok and r.error["code"] == "cost_cap" and media.stills == 0 and not media.previews


# ------------------------------------------------------------------ STUDIO_LOOK=simple
def test_look_simple_hides_composition_tools_and_keeps_old_behaviour(monkeypatch):
    monkeypatch.setenv("STUDIO_LOOK", "simple")
    for step in ("preview", "edit", "check"):
        names = {t["name"] for t in tool_schemas(step)}
        assert not names & set(ctools.COMPOSE_TOOLS)
    assert {"plan_composition", "preview_composition"} <= {t["name"] for t in tool_schemas("preview", "composed")}
    run = new_run()
    media = ComposedMedia()
    tb_loop = AgentLoop(client=FakeWonder([]), backends=media.backends())
    loop, client, _ = loop_for([[("read_memory", {})]], media)
    # the scripted driver (no Wonder) walks the old storyboard/build_cuts path
    monkeypatch.setenv("STUDIO_AGENT", "scripted")
    r = tb_loop.advance(run)
    human(run, "approve"); tb_loop.advance(run); human(run, "approve", choice="c1"); r = tb_loop.advance(run)
    assert card(run)["type"] == "preview" and card(run)["look"] is None and media.frames_calls == 1
    tb = Toolbox(run, media.backends())
    r = tb.call("plan_composition", {})
    assert not r.ok and r.error["code"] == "not_allowed_in_step"


# ------------------------------------------------------------------ scripted driver (the Wonder-failure path)
def test_scripted_driver_runs_the_composed_flow_and_steers(monkeypatch):
    monkeypatch.setenv("STUDIO_AGENT", "scripted")
    run = new_run()
    media = ComposedMedia()
    loop = AgentLoop(client=FakeWonder([]), backends=media.backends())
    loop.advance(run); human(run, "approve"); loop.advance(run); human(run, "approve", choice="c2")
    r = loop.advance(run)
    assert r["status"] == "awaiting_human" and card(run)["frames_kind"] == "composed"
    human(run, "steer", text="calmer background please")
    loop.advance(run)
    assert card(run)["look"]["plan_id"] == "plan.v2" and "calmer background" in media.plan_calls[-1]["guidance"]
    human(run, "approve"); r = loop.advance(run)
    assert card(run)["type"] == "edit" and card(run)["look"]["mode"] == "composed"
    n_builds, n_mattes = media.builds, media.mattes
    human(run, "steer", text="bigger headline")                   # LOOK steer: new plan + re-render, master reused
    loop.advance(run)
    assert media.builds == n_builds and media.mattes == n_mattes + 1 and len(media.renders) == 2
    assert card(run)["look"]["plan_id"] == "plan.v3"
    human(run, "steer", text="shorter story please")             # CONTENT steer: revise_edit + rebuild + render
    loop.advance(run)
    assert media.builds == n_builds + 1 and media.revise_guidance and len(media.renders) == 3
    assert audit_gate_violations(run)["violations"] == []


# ------------------------------------------------------------------ pure helpers
def test_tint_dim_dims_bright_plates_and_tints():
    bright = Image.new("RGB", (40, 40), (240, 235, 230))
    out = ctools.tint_dim(bright, "#5a1230")
    luma = ImageStat.Stat(out.convert("L")).mean[0] / 255
    assert luma <= 0.41 and out.getpixel((0, 0))[0] > out.getpixel((0, 0))[2]
    dark = Image.new("RGB", (40, 40), (20, 20, 20))
    assert ctools.tint_dim(dark, None).getpixel((0, 0)) == (20, 20, 20)


def concept(beats=((0.0, 6.0), (7.0, 13.0))):
    return {"id": "c1", "title": "T", "hook": "Hook one", "cta": "See the saree on our website", "length_s": 12,
            "beats": [{"clip": "a.mov", "src_in": s, "src_out": e, "purpose": "x", "on_screen_text": "Hook one" if i == 0 else "",
                       "visual_note": ""} for i, (s, e) in enumerate(beats)]}


def test_locate_maps_output_time_to_source_time():
    c = concept()
    assert ctools.locate(c, 3.0) == (0, 3.0)
    assert ctools.locate(c, 9.0) == (1, 10.0)
    assert ctools.locate(c, 99.0)[0] == 1


def test_retime_plan_adds_hold_captions_and_keeps_locked_text():
    c = concept()
    p = compose_plan.default_plan(c)
    out = ctools.retime_plan(p, c, [{"text": "namaste", "start": 0.0, "end": 1.5}, {"text": "", "start": 2, "end": 3}])
    assert out["duration"] == 14.0 and out["captions"]["chunks"] == [{"text": "namaste", "start": 0.0, "end": 1.5}]
    cta = next(t for t in out["text"] if t["role"] == "cta")
    assert cta["end"] == 14.0 and cta["text"] == cta["cta_short"] and not ctools.cta_is_internal(cta["text"])
    assert len(cta["text"].split()) <= 4
    assert next(t for t in out["text"] if t["role"] == "hook")["text"] == "Hook one"


def test_degrade_and_force_preset():
    c = concept()
    p = compose_plan.default_plan(c, look="cutout_on_textile_plate")
    assert p["preset"] == "cutout_on_textile_plate"
    d = ctools.degrade_plates(p, ["plate1"], c)
    assert d["preset"] == "cutout_on_brand_colour" and all(b["kind"] == "gradient" for b in d["background"])
    back, ok = ctools.force_preset(d, c, "cutout_on_textile_plate")
    assert ok and back["preset"] == "cutout_on_textile_plate"
    same, ok = ctools.force_preset(d, c, "cutout_over_blurred_scene")      # needs client stills: refused, plan unchanged
    assert not ok and same["preset"] == "cutout_on_brand_colour"
    cut, ok = ctools.apply_callout(p, "")
    assert ok and not any(t["role"] == "callout" for t in cut["text"])


def test_estimates_are_itemised_and_add_up():
    usd, items = ctools.est_edit(14.0, 1)
    assert usd == pytest.approx(sum(i["usd"] for i in items), abs=1e-3)
    assert items[0]["usd"] == pytest.approx(210 * ctools.SAM_FRAME_USD, abs=1e-4)
    short, _ = ctools.est_edit(1.0)
    assert short >= 50 * ctools.SAM_FRAME_USD                     # the 50-frame minimum applies
    p, pi = ctools.est_preview(2, 1)
    assert p == pytest.approx(ctools.PLAN_USD + 2 * ctools.STILL_USD + ctools.PLATE_USD)


# ------------------------------------------------------------------ real ffmpeg + real preview renderer, fake SAM
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_preview_composition_with_real_ffmpeg_and_renderer(tmp_path):
    clip = tmp_path / "a.mov"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=540x960:r=30:d=14", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    run = new_run()

    def fake_matte(image, out_png, prompt="woman"):
        m = Image.new("L", Image.open(image).size, 0)
        w, h = m.size
        ImageDraw.Draw(m).ellipse([w * 0.2, h * 0.3, w * 0.8, h], fill=255)
        m.save(out_png)
        return {"matte": str(out_png), "cost_usd": 0.0025, "head_top_frac": 0.3}

    media = ComposedMedia(textile=False)
    b = media.backends()
    for k in ("grab_frame", "still_video", "render", "lint", "make_plate"):
        b._over.pop(k, None)                       # real ffmpeg frame grab, real still videos, real preview renderer
    b._over.update(matte_still=fake_matte,
                   load_clips=lambda: [{"file": "a.mov", "path": str(clip), "duration_s": 14.0, "transcript": "x", "turns": []}])
    loop = AgentLoop(client=FakeWonder(script("brief", "ideas", "preview")), backends=b)
    loop.advance(run); human(run, "approve"); loop.advance(run); human(run, "approve", choice="c2")
    r = loop.advance(run)
    assert r["status"] == "awaiting_human" and r["fallbacks"] == 0, r
    c = card(run)
    assert c["frames_kind"] == "composed" and not [e for e in kinds(run, "fallback") if e["payload"].get("scope") == "look"]
    out = Toolbox(run, b).dir
    for u in c["frames"]:
        im = Image.open(out / u.rsplit("/", 1)[1])
        assert im.size == (540, 960)


# ------------------------------------------------------------------ more fallbacks / plan freshness at render time
def _to_edit(media, extra=()):
    run = new_run()
    loop, client, _ = loop_for(script("brief", "ideas", "preview") + [[("build_cuts", {})]] + list(extra), media)
    loop.advance(run)
    for kind, payload in [("approve", {}), ("approve", {"choice": "c2"}), ("approve", {})]:
        human(run, kind, **payload)
        loop.advance(run)
    return run, loop


def test_render_inputs_rejected_by_lint_fall_back():
    media = ComposedMedia()
    media.backends_lint = ["background 'plate1' still contains a person (would ghost behind the speaker): pick a person-free image"]
    run = new_run()
    b = media.backends()
    b._over["lint"] = lambda plan, assets: media.backends_lint
    loop = AgentLoop(client=FakeWonder(script("brief", "ideas", "preview", "edit")), backends=b)
    loop.advance(run)
    for kind, payload in [("approve", {}), ("approve", {"choice": "c2"}), ("approve", {})]:
        human(run, kind, **payload)
        loop.advance(run)
    c = card(run)
    assert c["look"]["mode"] == "simple_fallback" and not media.renders
    fb = [e["payload"] for e in kinds(run, "fallback") if e["payload"].get("step") == "edit"]
    assert fb and "person" in fb[0]["detail"]


def test_render_without_a_master_is_refused_with_a_hint():
    media = ComposedMedia()
    run, loop = _to_edit(media)
    # build_cuts ran in the scripted turn above only up to the model's script; delete the master to simulate a stale one
    tb = Toolbox(run, media.backends())
    (tb.dir / "work" / "master_hold.mp4").unlink()
    r = tb.call("render_composition", {})
    assert not r.ok and r.error["code"] == "missing_artifact" and "build_cuts" in r.error["hint"]


# ------------------------------------------------------------------ F4: defects found by the reviews
DIRTY_CTA = "See this Maheshwari on our website - soft end card"


def _edit_loop(media, extra=()):
    run = new_run()
    loop, client, _ = loop_for(script("brief", "ideas", "preview", "edit") + list(extra), media)
    loop.advance(run)
    for kind, payload in [("approve", {}), ("approve", {"choice": "c2"}), ("approve", {}), ("approve", {})]:
        human(run, kind, **payload)
        loop.advance(run)
    return run, loop


@pytest.mark.parametrize("raw,ok", [
    (DIRTY_CTA, "Shop Maheshwari"),
    ("Shop Maheshwari now", "Shop Maheshwari now"),
    ("Soft end card", "Shop now"),
    ("Order today on our website for free delivery - fade", "Order today on our"),
    ("", "Shop now"),
])
def test_short_cta_never_carries_an_internal_direction(raw, ok):
    out = ctools.short_cta(raw)
    assert out == ok and len(out.split()) <= 4 and not ctools.cta_is_internal(out)


def test_internal_cta_direction_never_reaches_the_renderer_or_the_stills():
    media = ComposedMedia(textile=False)
    media.cta = DIRTY_CTA
    run, loop = _edit_loop(media)
    for plan in media.previews + media.renders:
        cta = next(t for t in plan["text"] if t["role"] == "cta")
        assert cta["text"] == cta["cta_short"] and not ctools.cta_is_internal(cta["text"]) and len(cta["text"].split()) <= 4
        assert "soft" not in json.dumps(plan["text"]).lower()
    assert media.renders and media.previews


def test_human_cta_edit_is_kept_verbatim_and_shown():
    media = ComposedMedia(textile=False)
    media.cta = DIRTY_CTA
    run, loop, client, media = _preview_loop([[("preview_composition", {})], [("ask_human", {"summary": "ok"})]],
                                             textile=False)
    media.cta = DIRTY_CTA
    human(run, "edit", fields={"cta": "Visit example dot com today"})
    loop.advance(run)
    tb = Toolbox(run, media.backends())
    cta = next(t for t in tb.plan_rec()["plan"]["text"] if t["role"] == "cta")
    assert cta["text"] == cta["cta_short"] == "Visit example dot com today"
    assert next(t for t in media.previews[-1]["text"] if t["role"] == "cta")["text"] == "Visit example dot com today"


def test_overlong_human_fields_are_ignored_with_a_reason_and_not_locked():
    run, loop, client, media = _preview_loop([[("preview_composition", {})], [("ask_human", {"summary": "ok"})]])
    long = "x" * 81
    human(run, "edit", fields={"hook": long, "cta": "Shop now", "title": long, "callout": long})
    loop.advance(run)
    tb = Toolbox(run, media.backends())
    ans = tb.last_answer()
    assert ans["applied"] == ["cta"]
    assert sum("too long" in i and "maximum is 80" in i for i in ans["ignored"]) == 3
    locks = tb._read("human_locks.json")["c2"]
    assert "hook" not in locks and "title" not in locks and locks["cta"] == "Shop now"
    assert "callout" not in tb.plan_locks()
    ok = "y" * 80
    rejected = tb._lock_fields("c2", {"hook": ok, "title": long})
    assert list(rejected) == ["title"] and "too long" in rejected["title"]
    assert tb._read("human_locks.json")["c2"]["hook"] == ok


def test_callout_and_preset_edits_are_reported_applied_only_when_applied():
    run, loop, client, media = _preview_loop([[("preview_composition", {})], [("ask_human", {"summary": "ok"})]],
                                             textile=False)           # default plan: no callout block
    human(run, "edit", fields={"callout": "Handloom", "preset": "cutout_on_textile_plate"})
    loop.advance(run)
    tb = Toolbox(run, media.backends())
    ans = tb.last_answer()
    assert "callout" not in ans["applied"] and any(i.startswith("callout (") for i in ans["ignored"])
    assert "callout" not in tb.plan_locks()
    assert ("preset" in ans["applied"]) == (tb.plan_rec()["plan"]["preset"] == "cutout_on_textile_plate")
    # with a callout block in the plan the same edit is applied
    run2, loop2, _, media2 = _preview_loop([[("preview_composition", {})], [("ask_human", {"summary": "ok"})]],
                                           textile=False, callout=True)
    human(run2, "edit", fields={"callout": "Handloom"})
    loop2.advance(run2)
    tb2 = Toolbox(run2, media2.backends())
    assert "callout" in tb2.last_answer()["applied"] and tb2.plan_locks()["callout"] == "Handloom"
    assert next(t for t in tb2.plan_rec()["plan"]["text"] if t["role"] == "callout")["text"] == "Handloom"


def test_head_box_from_the_matte_is_on_the_plan_before_preview_and_render():
    media = ComposedMedia(textile=False)
    run, loop = _edit_loop(media)
    for plan in media.previews + media.renders:
        head = plan["meta"]["head"]
        assert head["top_frac"] == pytest.approx(0.2) and head["bbox_frac"] == pytest.approx([0.2, 0.18, 0.8, 1.0])


def test_preview_time_uses_best_preview_time_else_the_headline_window(monkeypatch):
    from adfarming import compose
    c = concept()
    p0 = compose_plan.default_plan(c)
    h0 = next(t for t in p0["text"] if t["role"] == "hook")
    real = ctools.preview_time(p0, 0, 0.0, 6.0)               # the renderer's own choice when it has one
    assert h0["start"] <= real < h0["end"] and real == compose.best_preview_time(p0, 0.0, 6.0)
    monkeypatch.delattr(compose, "best_preview_time")           # renderer without it: headline window midpoint
    c = concept()
    p = compose_plan.default_plan(c)
    hook = next(t for t in p["text"] if t["role"] == "hook")
    s, e = 0.0, 6.0
    want = round((max(hook["start"], s) + min(hook["end"], e)) / 2, 3)
    assert ctools.preview_time(p, 0, s, e) == want and hook["start"] <= want <= hook["end"]
    monkeypatch.setattr(compose, "best_preview_time", lambda plan, a, b: 4.25, raising=False)
    assert ctools.preview_time(p, 0, s, e) == 4.25
    monkeypatch.setattr(compose, "best_preview_time", lambda plan, a, b: 99.0, raising=False)
    assert ctools.preview_time(p, 0, s, e) < e                     # always inside the beat


def test_preview_stills_are_cut_at_the_headline_time():
    media = ComposedMedia(textile=False)
    run, loop, client, _ = _preview_loop([], textile=False)
    tb = Toolbox(run, media.backends())
    plan = tb.plan_rec()["plan"]
    hook = next(t for t in plan["text"] if t["role"] == "hook")
    s, e = ctools.bg_ranges(plan)[0]
    assert hook["start"] <= ctools.preview_time(plan, 0, s, e) < hook["end"]


def test_simple_fallback_copies_are_refreshed_when_beats_change():
    media = ComposedMedia()
    run, loop = _edit_loop(media)
    tb = Toolbox(run, media.backends())
    first = (tb.dir / "c2_simple_9x16.mp4").read_bytes()
    assert first.startswith(b"video")
    # a content change: build_cuts makes new simple cuts (different bytes), then render again
    tb.call("build_cuts", {})
    tb.call("render_composition", {})
    second = (tb.dir / "c2_simple_9x16.mp4").read_bytes()
    assert second.startswith(b"video") and second != first
    meta = tb._read("meta.json")
    assert meta["simple_sig"] == meta["beats_sig"]


def test_fallback_event_and_result_carry_no_absolute_paths():
    media = ComposedMedia()
    media.fail.add("render")
    b = media.backends()

    def boom(*a, **k):
        raise RuntimeError("ffmpeg failed on /Users/secret/work/private/clip.mp4: bad")
    b._over["render"] = boom
    run = new_run()
    loop = AgentLoop(client=FakeWonder(script("brief", "ideas", "preview", "edit")), backends=b)
    loop.advance(run)
    for kind, payload in [("approve", {}), ("approve", {"choice": "c2"}), ("approve", {})]:
        human(run, kind, **payload)
        loop.advance(run)
    for e in kinds(run, "fallback"):
        assert "/Users" not in json.dumps(e["payload"]) and "secret" not in json.dumps(e["payload"])
    tb = Toolbox(run, b)
    assert "/Users" not in json.dumps(tb._read("look.json")) and "clip.mp4" in json.dumps(tb._read("look.json"))
    assert ctools.clean_detail("see /tmp/a/b/c.png and a/b") == "see c.png and a/b"


def test_each_ratio_is_rendered_with_its_own_size_and_a_bad_ratio_keeps_the_simple_cut():
    media = ComposedMedia(textile=False)
    media.fail.add("ratio")
    run, loop = _edit_loop(media)
    tb = Toolbox(run, media.backends())
    meta = tb._read("meta.json")
    assert meta["ratios"] == {"9x16": "composed", "1x1": "simple", "16x9": "simple"}
    assert (tb.dir / "c2_1x1.mp4").read_bytes().startswith(b"video") and meta["look"] == "composed"
    assert not list(tb.dir.glob(".*.part.mp4"))
    media2 = ComposedMedia(textile=False)
    media2.fail.add("ratio_unsupported")
    run2, _ = _edit_loop(media2)
    assert Toolbox(run2, media2.backends())._read("meta.json")["ratios"]["16x9"] == "simple"


def test_json_writes_are_atomic_and_leave_no_temp_files():
    run, loop, client, media = _preview_loop([])
    tb = Toolbox(run, media.backends())
    tb._write("look.json", {"mode": "simple"})
    assert tb._read("look.json") == {"mode": "simple"} and not list(tb.dir.glob(".*.tmp"))


def test_preview_edit_in_the_simple_look_makes_no_plan(monkeypatch):
    monkeypatch.setenv("STUDIO_LOOK", "simple")
    run = new_run()
    media = ComposedMedia()
    loop = AgentLoop(client=FakeWonder(script("brief", "ideas") + [[("storyboard", {})], [("ask_human", {"summary": "f"})]]),
                     backends=media.backends())
    to_preview(run, loop)
    human(run, "edit", fields={"hook": "Mera hook", "callout": "x", "preset": "cutout_on_brand_colour"})
    tb = Toolbox(run, media.backends())
    ans = tb._apply_edit("preview", {"fields": {"hook": "Mera hook", "callout": "x"}}, events.rebuild_run_state(run))
    assert "callout" in ans["ignored"] and "hook" in ans["applied"] and tb.plan_rec() is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_preview_renderer_gets_the_subject_from_held_still_master_and_matte(tmp_path):
    """Regression for the black preview stills: the preview master/matte are 5 fps held-still videos (first pts 0.1);
    the REAL renderer must put the cut-out subject on screen at the time the headline is visible."""
    from adfarming import compose
    c = concept()
    plan = compose_plan.default_plan(c, ["#3b0a2a", "#7a1d3c"])
    ranges = ctools.bg_ranges(plan)
    master_frames, matte_frames = [], []
    for i, (s, e) in enumerate(ranges):
        f = tmp_path / f"f{i}.png"
        Image.new("RGB", (1080, 1920), (0, 230, 0)).save(f)          # the 'speaker' is bright green
        m = Image.new("L", (1080, 1920), 0)
        ImageDraw.Draw(m).ellipse([240, 500, 840, 1920], fill=255)
        mp = tmp_path / f"m{i}.png"
        m.save(mp)
        master_frames.append((str(f), e - s))
        matte_frames.append((str(mp), e - s))
    master = ctools.d_still_video(master_frames, tmp_path / "prev_master.mp4")
    matte = ctools.d_still_video(matte_frames, tmp_path / "prev_matte.mp4")
    plan = ctools.set_head(plan, [{"head_top_frac": 0.29, "subject_bbox_frac": [0.22, 0.26, 0.78, 1.0]}])
    out = compose.render_composition(ctools.render_view(plan), {"master": master, "matte": matte, "backdrops": {},
                                                                "pip_images": {}, "clips": {}},
                                     tmp_path / "stills", preview=True, workdir=tmp_path / "work")
    assert out["frames"]
    for p in out["frames"]:
        im = Image.open(p).convert("RGB")
        px = im.getpixel((im.width // 2, int(im.height * 0.72)))      # inside the ellipse, below the head
        assert sum(px) > 120 and px[1] > px[0] + 40, (p, px)           # the speaker's pixels, not black / background
    # the still time is where the headline is on screen
    hook = next(t for t in plan["text"] if t["role"] == "hook")
    s, e = ranges[0]
    t_head = ctools.preview_time(plan, 0, s, e)
    assert hook["start"] <= t_head < hook["end"]
    # the seek into the held-still master must give the subject at ANY time (headline time, t=7.5 and mid-beat),
    # not only at lucky multiples of the beat length
    for t in (t_head, 1.7, 2.5, 7.5, 9.0):
        sp = tmp_path / f"subj_{t}.png"
        compose._subject_png(master, matte, t, compose_plan_with_size(plan), sp)
        px = Image.open(sp).convert("RGBA").getpixel((540, 1400))
        assert px[3] > 200 and sum(px[:3]) > 120 and px[1] > px[0] + 40, (t, px)


def compose_plan_with_size(plan):
    p = json.loads(json.dumps(plan))
    p["size"], p["fps"] = [1080, 1920], 30          # what render_composition sets before it builds the subject chain
    return p


# ---------------------------------------------------------------- D3: plan timeline vs master length
def _plan_with_callout(c):
    p = compose_plan.default_plan(c)
    p["text"].append({"id": "callout", "role": "callout", "text": "Perfect for festive", "region": "right_mid", "font": "latin",
                      "size": 40, "style": "callout_tag", "fill": "#111111", "stroke": None, "plate": "#fbbf24",
                      "pill_text": None, "rot": 0, "animation": "pop", "weight": "black", "start": 5.0, "end": 8.0, "beat": 1})
    p = ctools.retime_plan(p, c, [])
    assert any(t["role"] == "callout" for t in p["text"])
    return p


def test_rescale_stretches_plan_to_master_and_clamps_cta():
    c = concept()
    p = _plan_with_callout(c)
    old = p["duration"]                                    # 14.0: beats + hold
    call = next(t for t in p["text"] if t["role"] == "callout")
    s0, e0 = call["start"], call["end"]
    master = round(old + 2.6, 2)                           # master longer than the plan
    out = ctools.rescale_to_master(copy.deepcopy(p), master)
    assert out["duration"] == master and out["meta"]["rescaled"] == {"from": old, "to": master}
    co = next(t for t in out["text"] if t["role"] == "callout")
    f = master / old
    assert co["start"] == pytest.approx(s0 * f, abs=0.02) and co["end"] == pytest.approx(e0 * f, abs=0.02)
    assert all(t["end"] <= master + 1e-9 for t in out["text"])
    cta = next(t for t in out["text"] if t["role"] == "cta")
    assert cta["end"] == master and cta["start"] >= master - ctools.CTA_WINDOW_S - 1e-9
    assert [b["start"] for b in out["background"]][0] == 0
    assert out["background"][-1]["start"] > p["background"][-1]["start"]


def test_rescale_noop_when_close_and_ignores_unprobed_master():
    c = concept()
    p = _plan_with_callout(c)
    same = ctools.rescale_to_master(copy.deepcopy(p), p["duration"] + 0.01)
    assert [(t["start"], t["end"]) for t in same["text"] if t["role"] != "cta"] == \
           [(t["start"], t["end"]) for t in p["text"] if t["role"] != "cta"]
    assert "rescaled" not in same["meta"]
    assert ctools.rescale_to_master(copy.deepcopy(p), None)["duration"] == p["duration"]
    assert ctools.probe_duration("/nonexistent/none.mp4") is None


def test_rescale_shrinks_long_cta_window_to_last_three_seconds():
    c = concept()
    p = _plan_with_callout(c)
    cta = next(t for t in p["text"] if t["role"] == "cta")
    cta["start"] = 2.0
    out = ctools.rescale_to_master(p, p["duration"])
    cta = next(t for t in out["text"] if t["role"] == "cta")
    assert cta["end"] - cta["start"] <= ctools.CTA_WINDOW_S + 1e-9


def test_set_subject_luma_records_meta_and_ignores_garbage():
    p = ctools.set_subject_luma({"meta": {"head": {"top_frac": 0.3}}}, 0.4321)
    assert p["meta"]["subject"]["luma"] == 0.4321 and p["meta"]["head"]["top_frac"] == 0.3
    assert "subject" not in ctools.set_subject_luma({}, None).get("meta", {})
    assert "subject" not in ctools.set_subject_luma({}, 7).get("meta", {})
    assert ctools.subject_luma("/nonexistent/m.mp4", "/nonexistent/k.mp4") is None


# ---------------------------------------------------------------- D8: QC prompt knows the soft end-card pill
def test_qc_prompt_exempts_onscreen_cta_pill_and_judges_spoken_cta():
    from adfarming import qc
    text = qc.PROMPT
    assert "end-card pill" in text and "BY DESIGN" in text
    assert "SPOKEN CTA" in text and "discount, price or" in text
    text.format(brief="b", concept="c", captions="x")      # still a valid template


def test_subject_luma_measures_masked_pixels_and_probe_has_duration(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    master, matte = tmp_path / "m.mp4", tmp_path / "k.mp4"
    # left half of the frame mid-grey (the subject), right half white; the matte selects only the left half
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=160x160:d=2:r=10", "-vf",
                    "drawbox=x=0:y=0:w=80:h=160:color=0x808080:t=fill", "-pix_fmt", "yuv420p", str(master)], check=True, timeout=60)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x160:d=2:r=10", "-vf",
                    "drawbox=x=0:y=0:w=80:h=160:color=white:t=fill", "-pix_fmt", "yuv420p", str(matte)], check=True, timeout=60)
    assert ctools.probe_duration(master) == pytest.approx(2.0, abs=0.2)
    lum = ctools.subject_luma(master, matte)
    assert lum is not None and lum == pytest.approx(compose_plan._lum("#808080"), abs=0.04)   # not the white background


# ------------------------------------------------------------------ safety net: planned blocks are never dropped
def test_dropped_planned_block_is_re_added_and_logged(monkeypatch):
    media = ComposedMedia(textile=False)
    media.callout = True
    real = ctools.rescale_to_master

    def dropping(plan, dur):
        p = real(plan, dur)
        p["text"] = [t for t in p["text"] if t["role"] != "callout"]     # a pipeline step silently loses the callout
        return p

    monkeypatch.setattr(ctools, "rescale_to_master", dropping)
    run, loop = _edit_loop(media)
    assert media.renders
    for plan in media.renders:
        assert any(t["role"] == "callout" and t["text"] == "Pure cotton" for t in plan["text"])
    ev = [e["payload"] for e in kinds(run, "plan_dropped_block")]
    assert ev and ev[0]["block"] == "callout"
    comp = Toolbox(run, media.backends())._read("composition.json")
    assert any(t["role"] == "callout" for t in comp["plan"]["text"])


def test_no_event_when_nothing_dropped():
    media = ComposedMedia(textile=False)
    media.callout = True
    run, loop = _edit_loop(media)
    assert media.renders and not kinds(run, "plan_dropped_block")


def test_readd_planned_blocks_unit():
    approved = {"text": [{"role": "hook", "text": "H"}, {"role": "callout", "text": "C", "end": 99}, {"role": "caption"}],
                "duration": 10}
    plan = {"text": [{"role": "hook", "text": "H"}], "duration": 10}
    fixed, names = ctools.readd_planned_blocks(approved, plan)
    assert names == ["callout"] and [t["role"] for t in fixed["text"]] == ["hook", "callout"]
    assert fixed["text"][1]["end"] == 10 and not ctools.missing_planned_blocks(approved, fixed)

"""Fakes for the agent tests: a scripted /v1/responses, fake media backends, a scripted human."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import pytest

from adfarming.agent import AgentLoop, Backends
from adfarming.agent.loop import ApiError
from adfarming.studio import events, gates, memory


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADFARMING_DB", str(tmp_path / "studio.db"))
    monkeypatch.setenv("ADFARMING_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("ADF_PRIVATE", str(tmp_path / "private"))
    monkeypatch.delenv("STUDIO_RUN_CAP_USD", raising=False)
    monkeypatch.delenv("STUDIO_AGENT", raising=False)
    return tmp_path


def _concept(i, hook, cta="See the saree on our website"):
    return {"id": f"c{i}", "title": f"Concept {i}", "angle": "story", "hook": hook, "cta": cta, "length_s": 15,
            "beats": [{"clip": "a.mov", "src_in": 0.0, "src_out": 6.0, "purpose": "hook",
                       "on_screen_text": hook, "visual_note": "woman talks"},
                      {"clip": "a.mov", "src_in": 7.0, "src_out": 13.0, "purpose": "proof", "on_screen_text": "",
                       "visual_note": "closeup"}],
            "why_it_fits": "fits", "risks": "none"}


class FakeMedia:
    """Fake concepts/storyboard/cuts/QC backends. Records calls so tests can inspect guidance etc."""

    def __init__(self, verdicts=None, build_cost=0.05):
        self.guidance, self.revise_guidance, self.builds, self.frames_calls = [], [], 0, 0
        self.verdicts = list(verdicts or [])
        self.build_cost = build_cost

    def backends(self) -> Backends:
        return Backends(load_clips=lambda: [{"file": "a.mov", "duration_s": 20.0, "transcript": "x", "turns": []}],
                        load_brief=lambda: "Brief: festive saree ad for Dharaa.", load_subject=lambda: "a woman in a saree",
                        load_refs=lambda: "", generate=self.generate, revise=self.revise, frames=self.frames,
                        build=self.build, track=lambda run, p, prompt="woman": {"frames": 0},
                        review=self.review, interpret_steer=lambda s, t, c: {"concept_id": None, "guidance": "tighten"},
                        propose_rules=lambda steers: [{"kind": "preference",
                                                       "text": "Keep hooks short and avoid year references."}])

    def generate(self, run, brief, clips, refs, attempts=3, guidance=""):
        self.guidance.append(guidance)
        run.trace("muse-spark", {}, {}, decision="fake concepts")
        short = "shorter" in guidance.lower()
        return [_concept(i, ("Hook" if short else "A long hook about Diwali 2019") + f" {i}") for i in (1, 2, 3)]

    def revise(self, run, brief, concept, verdict, clips, guidance=""):
        self.revise_guidance.append(guidance)
        run.trace("muse-spark", {}, {}, decision="fake revise")
        c = json.loads(json.dumps(concept))
        c["hook"] = "Revised short hook"
        c["beats"][0]["on_screen_text"] = c["hook"]
        return c

    def frames(self, run, concept, subject):
        self.frames_calls += 1
        d = run.dir / "frames"
        d.mkdir(exist_ok=True)
        out = []
        for i, _ in enumerate(concept["beats"], 1):
            p = d / f"{concept['id']}_beat{i}.webp"
            p.write_bytes(b"webp")
            out.append(f"frames/{p.name}")
            run.trace("muse-image", {}, {}, cost_usd=0.01)
        return out

    def build(self, run, concept, clips, track=None):
        self.builds += 1
        work = run.dir / "work"
        work.mkdir(exist_ok=True)
        master = work / "master_hold.mp4"
        master.write_bytes(b"m")
        if track:
            track(str(master))
        cuts = {}
        for r in ("9x16", "1x1", "16x9"):
            n = f"{concept['id']}_{r}.mp4"
            (run.dir / n).write_bytes(b"video" + bytes([self.builds]))
            cuts[r] = n
        run.trace("ffmpeg", {}, {}, cost_usd=self.build_cost)
        return {"cuts": cuts, "captions": [{"start": 0.0, "end": 1.5, "text": "namaste"}], "titles": [],
                "master": str(master)}

    def review(self, run, brief, concept, captions, video):
        v = self.verdicts.pop(0) if self.verdicts else {"pass": True, "score": 5, "issues": [], "fixes": [],
                                                       "summary": "good"}
        run.trace("muse-wonder", {}, v, cost_usd=0.02)
        return v


def resp(n, calls=(), text=None, commentary=None, usage=None):
    out = [{"type": "reasoning", "summary": [], "encrypted_content": "enc"}]
    if commentary:
        out.append({"type": "message", "phase": "commentary", "role": "assistant",
                    "content": [{"type": "output_text", "text": commentary}]})
    for i, (name, args) in enumerate(calls):
        out.append({"type": "function_call", "call_id": f"call_{n}_{i}", "name": name,
                    "arguments": args if isinstance(args, str) else json.dumps(args), "status": "completed"})
    if text:
        out.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    return {"id": f"resp_{n}", "status": "completed", "output": out,
            "usage": usage or {"input_tokens": 1000, "output_tokens": 100}}


class FakeWonder:
    """Scripted /v1/responses. script = list of turns; a turn is a list of (tool, args) calls, a str (text-only)
    or a callable(body) -> response dict. Unknown previous_response_id -> 404 like the real API."""

    def __init__(self, script, expire_prev=False, fail_all=None):
        self.script, self.requests, self.n = list(script), [], 0
        self.known, self.expire_prev, self.fail_all = set(), expire_prev, fail_all

    def create(self, body):
        self.requests.append(json.loads(json.dumps(body)))
        if self.fail_all is not None:
            raise ApiError(self.fail_all, "boom")
        prev = body.get("previous_response_id")
        if prev and (self.expire_prev or prev not in self.known):
            raise ApiError(404, "previous_response_id not found")
        if not self.script:
            raise ApiError(400, "script exhausted")
        self.n += 1
        t = self.script.pop(0)
        r = t(body) if callable(t) else resp(self.n, text=t) if isinstance(t, str) else resp(self.n, calls=t, commentary="ok")
        r["id"] = f"resp_{self.n}"
        self.known.add(r["id"])
        return r


def pending(run_id):
    return events.rebuild_run_state(run_id)["pending_gate"]


def human(run_id, kind, text=None, **payload):
    g = pending(run_id)
    assert g, "no pending gate"
    return gates.answer_gate(g["gate_id"], kind, payload or None, text=text)


def new_run(media=None):
    memory.add_human_memory("dharaa", "hard_rule", "No hard-sell and no fake urgency.", pinned=True)
    memory.add_human_memory("dharaa", "brand_voice", "Warm first-person Hinglish.")
    return events.create_run("dharaa", brief="Karwa Chauth push")


def loop_for(script, media=None, **kw):
    media = media or FakeMedia()
    client = FakeWonder(script, **kw)
    return AgentLoop(client=client, backends=media.backends()), client, media


HAPPY = {
    "brief": [[("read_memory", {})], [("ask_human", {"summary": "Festive saree ad, warm, no hard sell."})]],
    "ideas": [[("write_concepts", {"memory_ids_used": []})], [("ask_human", {"summary": "3 concepts", "recommendation": "c2"})]],
    "preview": [[("storyboard", {})], [("ask_human", {"summary": "frames"})]],
    "edit": [[("build_cuts", {})], [("ask_human", {"summary": "cuts built"})]],
    "check": [[("ask_human", {"summary": "QC passed"})]],
    "ship": [[("propose_memory", {"kind": "preference", "text": "Prefer short hooks without year references."})],
             [("ask_human", {"summary": "package"})]],
    "done": [[("finish", {"summary": "all done"})]],
}


def script_for(*steps):
    out = []
    for s in steps:
        out += HAPPY[s]
    return out

"""Two clients through the fake-media agent loop: each run sees only its own brief, subject, clips, memory."""
import json
import subprocess

import pytest

from adfarming.agent import AgentLoop, Backends
from adfarming.studio import events, memory

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clientkit import make_client  # noqa: E402
from .conftest import FakeMedia, FakeWonder, human, script_for


class SpyMedia(FakeMedia):
    def __init__(self):
        super().__init__()
        self.seen = []

    def generate(self, run, brief, clips, refs, attempts=3, guidance=""):
        self.seen.append({"run": run.run_id, "brief": brief, "clips": [c["file"] for c in clips],
                          "paths": [c["path"] for c in clips], "refs": refs})
        return super().generate(run, brief, clips, refs, attempts, guidance)

    def frames(self, run, concept, subject):
        self.seen.append({"run": run.run_id, "subject": subject})
        return super().frames(run, concept, subject)

    def build(self, run, concept, clips, track=None):
        self.seen.append({"run": run.run_id, "build_paths": [c["path"] for c in clips]})
        return super().build(run, concept, clips, track)

    def review(self, run, brief, concept, captions, video):
        self.seen.append({"run": run.run_id, "review_brief": brief})
        return super().review(run, brief, concept, captions, video)

    def real_loader_backends(self):
        over = {k: v for k, v in super().backends()._over.items() if not k.startswith("load_")}
        return Backends(**{**over, "generate": self.generate, "frames": self.frames,
                           "build": self.build, "review": self.review})  # load_* = real client-aware defaults


def drive(loop, run):
    loop.advance(run)
    for kind, payload in [("approve", {}), ("approve", {"choice": "c2"}), ("approve", {}), ("approve", {})]:
        human(run, kind, **payload)
        loop.advance(run)


def test_two_clients_do_not_cross_data(env):
    priv = env / "private"
    make_client(priv, "alpha", "Alpha Tea", "ALPHA BRIEF: sell tea.", [("a1.mp4", 2), ("a2.mp4", 2)], "an alpha chef")
    make_client(priv, "beta", "Beta Shoes", "BETA BRIEF: sell shoes.", [("b1.mov", 3)], "a beta runner")
    (priv / "clients" / "beta" / "refs.md").write_text("BETA REFS")
    memory.add_human_memory("alpha", "hard_rule", "ALPHA rule: never mention coffee.")
    memory.add_human_memory("beta", "hard_rule", "BETA rule: never mention laces.")
    media = SpyMedia()
    runs = {}
    for cid in ("alpha", "beta"):
        runs[cid] = events.create_run(cid, brief="goal " + cid)
        loop = AgentLoop(client=FakeWonder(script_for("brief", "ideas", "preview", "edit")),
                         backends=media.real_loader_backends())
        drive(loop, runs[cid])
    by_run = {}
    for s in media.seen:
        by_run.setdefault(s["run"], []).append(s)
    for cid, other in (("alpha", "beta"), ("beta", "alpha")):
        blob = json.dumps(by_run[runs[cid]])
        assert f"{cid.upper()} BRIEF" in blob and f"{other.upper()} BRIEF" not in blob
        assert f"/clients/{cid}/footage/" in blob and f"/clients/{other}/" not in blob
        mine = {i["id"] for i in memory.list_memory(cid)}
        theirs = {i["id"] for i in memory.list_memory(other)}
        read = {i for e in events.list_events(runs[cid]) if e["kind"] == "memory_read" for i in e["payload"]["ids"]}
        assert read == mine and not (read & theirs)
    gen = {s["run"]: s for s in media.seen if "clips" in s}
    assert gen[runs["alpha"]]["clips"] == ["a1.mp4", "a2.mp4"] and gen[runs["beta"]]["clips"] == ["b1.mov"]
    assert gen[runs["beta"]]["refs"] == "BETA REFS" and gen[runs["alpha"]]["refs"] == ""
    subj = {s["run"]: s["subject"] for s in media.seen if "subject" in s}
    assert "an alpha chef" in subj[runs["alpha"]] and "a beta runner" in subj[runs["beta"]]
    builds = {s["run"]: s["build_paths"] for s in media.seen if "build_paths" in s}
    assert all("/clients/alpha/" in p for p in builds[runs["alpha"]])
    assert all("/clients/beta/" in p for p in builds[runs["beta"]])
    rev = {s["run"]: s["review_brief"] for s in media.seen if "review_brief" in s}
    assert "BETA BRIEF" in rev[runs["beta"]] and "ALPHA rule" not in rev[runs["beta"]]
    assert "ALPHA rule" in rev[runs["alpha"]]
    # each client's ASR caches were written in its own directory only
    assert sorted(p.name for p in (priv / "clients" / "alpha" / "asr").iterdir()) == ["a1.json", "a2.json"]
    assert sorted(p.name for p in (priv / "clients" / "beta" / "asr").iterdir()) == ["b1.json"]


def test_legacy_zero_arg_loader_overrides_still_work(env):
    """FakeMedia's load_clips=lambda: [...] (no client arg) keeps working."""
    media = FakeMedia()
    run = events.create_run("dharaa", brief="x")
    from adfarming.agent.tools import Toolbox
    tb = Toolbox(run, media.backends())
    assert tb.clips()[0]["file"] == "a.mov"
    assert tb.b.for_client("load_brief", "dharaa").startswith("Brief:")

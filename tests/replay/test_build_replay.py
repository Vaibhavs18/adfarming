"""build_replay.py: schema of fixtures.json and the checks mock.js relies on. Synthetic data only, no network."""
import importlib.util
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("build_replay", ROOT / "scripts" / "build_replay.py")
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)

RID = "run-synth-1"
T0 = "2026-01-01T10:00:{:02d}+00:00"


def _row(sec, kind, actor="agent", text="", step=None, cost=None, mem=None):
    return {"seq": 0, "ts": T0.format(sec), "step": step, "kind": kind, "actor": actor, "text": text, "cost_usd": cost, "memory_ids": mem or []}


def make_src(tmp: Path, with_frames=True) -> Path:
    src = tmp / "src"
    (src / "out" / RID).mkdir(parents=True)
    (src / "api").mkdir()
    if with_frames:
        from PIL import Image
        Image.new("RGB", (720, 1280), (200, 120, 90)).save(src / "out" / RID / "frame_c1_1.webp", "WEBP")
    url = lambda n: f"/api/runs/{RID}/artifacts/{n}"
    cards = [
        {"type": "brief", "text": "Synthetic brief.", "memory_cited": ["m1"], "cost_estimate_usd": 0.01},
        {"type": "ideas", "concepts": [{"id": "c1", "hook": "long hook here"}], "recommended": "c1", "memory_cited": ["m1"], "cost_estimate_usd": 0.02},
        {"type": "ideas", "concepts": [{"id": "c1", "hook": "short"}], "recommended": "c1", "memory_cited": ["m1"], "cost_estimate_usd": 0.02},
        {"type": "ship", "cuts": {"9x16": url("c1_9x16.mp4")}, "zip": url("package.zip"), "memory_proposals": [{"id": "leak", "text": "other run"}]},
    ]
    cards[2]["frames"] = [url("frame_c1_1.webp")]
    answers = [{"kind": "approve"}, {"kind": "steer", "text": "shorter"}, {"kind": "approve", "payload": {"choice": "c1"}}, {"kind": "approve"}]
    steps = ["brief", "ideas", "ideas", "ship"]
    db = sqlite3.connect(src / "studio.db")
    db.executescript("""
        CREATE TABLE clients(id TEXT, name TEXT, created_at TEXT);
        CREATE TABLE runs(id TEXT, client_id TEXT, brief TEXT, created_at TEXT);
        CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, kind TEXT, actor TEXT, payload TEXT, reason TEXT);
        CREATE TABLE gates(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, step TEXT, status TEXT, requested_at TEXT, answered_at TEXT, answer TEXT);
    """)
    db.execute("INSERT INTO clients VALUES('acme','Acme Sarees','x')")
    db.execute("INSERT INTO runs VALUES(?,?,?,?)", (RID, "acme", "goal", "x"))
    for i, (c, a, st) in enumerate(zip(cards, answers, steps), start=1):
        db.execute("INSERT INTO gates(id,run_id,step,status,requested_at,answered_at,answer) VALUES(?,?,?,?,?,?,?)", (i, RID, st, "approved", "x", "y", json.dumps(a)))
        db.execute("INSERT INTO events(run_id,ts,kind,actor,payload) VALUES(?,?,?,?,?)", (RID, "x", "gate_requested", "agent",
                   json.dumps({"gate_id": i, "payload": {"card": c, "memory_refs": ["m1"]}})))
    db.commit(); db.close()
    log = [
        _row(0, "run_created", "system", "Started a session"),
        _row(1, "run_status", "system", "Run is now running"),
        _row(2, "memory_read", "system", "Read 1 item", mem=["m1"]),
        _row(3, "cost", "system", "Spent $0.02 on muse-wonder", cost=0.02),
        _row(4, "commentary", "agent", "Writing the brief."),
        _row(5, "gate_requested", "agent", "Asked you to review the brief step", "brief", mem=["m1"]),
        _row(10, "gate_answered", "human", "Approved the brief step", "brief"),
        _row(11, "memory_read", "system", "Read 1 item", mem=["m1"]),
        _row(12, "commentary", "system", "Server restarted while this run was working; it was interrupted."),
        _row(15, "cost", "system", "Spent $0.01 on write_concepts", cost=0.01),
        _row(16, "gate_requested", "agent", "Asked you to review the ideas step", "ideas"),
        _row(20, "gate_answered", "human", "Steered the ideas step", "ideas"),
        _row(20, "steer", "human", "Steer note: shorter", "ideas"),
        _row(24, "cost", "system", "Spent $0.01 on revise_concept", cost=0.01),
        _row(25, "gate_requested", "agent", "Asked you to review the ideas step", "ideas"),
        _row(26, "gate_answered", "human", "Approved the ideas step", "ideas"),
        _row(28, "memory_proposed", "agent", "Proposed a client memory (preference): Prefer short hooks.", mem=["mp1"]),
        _row(30, "gate_requested", "agent", "Asked you to review the ship step", "ship"),
        _row(31, "gate_answered", "human", "Approved the ship step", "ship"),
        _row(35, "cost", "system", "Spent $0.02 on muse-wonder", cost=0.02),
        _row(36, "commentary", "agent", "Wrapping up."),
    ]
    (src / "api" / f"{RID}.log.json").write_text(json.dumps(log))
    (src / "api" / f"{RID}.run.json").write_text(json.dumps({
        "run_id": RID, "client_id": "acme", "cost": {"usd": 0.06, "cap_usd": 1.5, "by_tool": {"muse-wonder": 0.04}},
        "artifacts": {"frames": [url("frame_c1_1.webp")], "cuts": {"9x16": url("c1_9x16.mp4")}, "captions": [{"start": 0, "end": 1, "text": "hi"}],
                      "qc": {"pass": True, "score": 5, "issues": [], "fixes": [], "summary": "ok"}, "zip": url("package.zip")}}))
    mem = [
        {"id": "m1", "kind": "hard_rule", "text": "No hard-sell.", "source": "human", "status": "approved", "pinned": True, "created_at": "x",
         "evidence": {"doc_ref": "brief.md#tone"}},
        {"id": "mp1", "kind": "preference", "text": "Prefer short hooks.", "source": "agent_inferred", "status": "proposed", "pinned": False, "created_at": "x",
         "evidence": {"human_quote": "shorter", "run_id": RID, "doc_ref": "agent_proposal"}},
        {"id": "other", "kind": "preference", "text": "From another run.", "source": "agent_inferred", "status": "proposed", "pinned": False, "created_at": "x",
         "evidence": {"run_id": "some-other-run"}},
    ]
    (src / "api" / "memory.json").write_text(json.dumps(mem))
    return src


def test_fixtures_schema_and_content(tmp_path):
    src = make_src(tmp_path)
    out = tmp_path / "out"
    fx = br.build(RID, src, out, media=False)
    on_disk = json.loads((out / "fixtures.json").read_text())
    assert on_disk == json.loads(json.dumps(fx))
    br.validate(on_disk)
    assert on_disk["schema"] == "adfarming-replay/1"
    assert on_disk["banner"] == "Replay of a real session on Acme Sarees. Nothing here calls any AI."
    assert on_disk["clients"] == [{"id": "acme", "name": "Acme Sarees"}]
    g = on_disk["gates"]
    assert [x["step"] for x in g] == ["brief", "ideas", "ideas", "ship"]
    assert [x["answer"]["kind"] for x in g] == ["approve", "steer", "approve", "approve"]
    assert g[1]["answer"]["text"] == "shorter" and g[2]["answer"]["choice"] == "c1"
    # the steer shows a different set of concepts before and after
    assert g[1]["card"]["concepts"][0]["hook"] == "long hook here" and g[2]["card"]["concepts"][0]["hook"] == "short"


def test_timings_costs_and_log(tmp_path):
    src = make_src(tmp_path)
    fx = br.build(RID, src, tmp_path / "out", media=False)
    g = fx["gates"]
    assert [x["work_s"] for x in g] == [5.0, 6.0, 5.0, 4.0]  # request time minus previous answer (or start)
    assert g[0]["cost"] == {"muse-wonder": 0.02} and g[1]["cost"] == {"write_concepts": 0.01} and g[2]["cost"] == {"revise_concept": 0.01}
    assert fx["tail"]["cost"] == {"muse-wonder": 0.02} and fx["tail"]["work_s"] == 5.0
    assert fx["run"]["total_cost_usd"] == pytest.approx(0.06)
    assert fx["run"]["total_real_s"] == 36.0
    all_rows = [r for x in g for r in x["log_q"] + x["log_a"]] + fx["tail"]["log"]
    kinds = {r["kind"] for r in all_rows}
    assert "run_status" not in kinds
    assert not any("Server restarted" in (r["text"] or "") for r in all_rows)
    assert sum(r["kind"] == "memory_read" for r in all_rows) == 1  # identical re-reads collapsed
    assert any(r["kind"] == "steer" for r in g[1]["log_a"]) and any(r["kind"] == "gate_answered" for r in g[0]["log_a"])
    assert g[1]["commentary"] == [] and g[0]["commentary"] == ["Writing the brief."]
    assert g[0]["in_play"] == ["m1"]


def test_memory_only_this_runs_proposals(tmp_path):
    fx = br.build(RID, make_src(tmp_path), tmp_path / "out", media=False)
    ids = {m["id"]: m for m in fx["memory"]}
    assert set(ids) == {"m1", "mp1"}  # other run's proposal never leaks in
    assert ids["mp1"]["appears_at_gate"] == 3  # shows up in the ship segment
    assert ids["m1"]["evidence"]["quote"].startswith("From the client's own brief")
    assert ids["mp1"]["evidence"]["quote"] == "shorter"


def test_urls_rewritten_zip_dropped_proposals_emptied(tmp_path):
    fx = br.build(RID, make_src(tmp_path), tmp_path / "out", media=False)
    ship = fx["gates"][3]["card"]
    assert ship["cuts"] == {"9x16": "media/9x16.mp4"}
    assert ship["zip"] is None and ship["memory_proposals"] == []
    assert fx["gates"][2]["card"]["frames"] == ["media/frame_1.webp"]
    blob = json.dumps(fx)
    assert "/api/runs/" not in blob and "package.zip" not in blob


def test_frames_downscaled(tmp_path):
    from PIL import Image
    src = make_src(tmp_path / "b")
    br.make_frame(src / "out" / RID / "frame_c1_1.webp", tmp_path / "f.webp")
    im = Image.open(tmp_path / "f.webp")
    assert im.width == 360 and im.height == 640


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_cut_reencoded_small(tmp_path):
    src = tmp_path / "in.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=25:duration=1",
                    "-f", "lavfi", "-i", "sine=duration=1", "-shortest", "-pix_fmt", "yuv420p", str(src)], check=True)
    dst = tmp_path / "o.mp4"
    size = br.make_cut(src, dst, "9x16")
    assert 0 < size <= br.MAX_CUT_BYTES
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(dst)],
                           capture_output=True, text=True).stdout.strip()
    assert probe == "540,960"


def test_mismatched_log_and_db_refuses(tmp_path):
    src = make_src(tmp_path)
    log = json.loads((src / "api" / f"{RID}.log.json").read_text())
    log = [r for r in log if not (r["kind"] == "gate_requested" and r["step"] == "ship")]
    (src / "api" / f"{RID}.log.json").write_text(json.dumps(log))
    with pytest.raises(SystemExit):
        br.build(RID, src, tmp_path / "out", media=False)


def test_validate_rejects_bad_fixtures(tmp_path):
    fx = br.build(RID, make_src(tmp_path), tmp_path / "out", media=False)
    bad = json.loads(json.dumps(fx))
    bad["gates"][0]["card"]["type"] = "ideas"
    with pytest.raises(ValueError):
        br.validate(bad)
    bad = json.loads(json.dumps(fx))
    del bad["tail"]
    with pytest.raises(ValueError):
        br.validate(bad)


def test_refuses_tracked_output_dir(tmp_path):
    # a path inside the repo that git does not ignore must be refused
    assert br._git_tracked_roots(ROOT / "web" / "replay-output-should-never-exist") is True
    assert br._git_tracked_roots(ROOT / "private" / "replay" / "anything") is False
    assert br._git_tracked_roots(tmp_path / "x") is False

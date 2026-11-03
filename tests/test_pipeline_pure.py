"""Pure-function tests for the pipeline (no network, no private footage). Run: .venv/bin/python -m pytest tests/test_pipeline_pure.py"""
import copy
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from adfarming import concepts as C, cuts, llm, qc, trace  # noqa: E402
from adfarming.trace import Run  # noqa: E402

SPEECH = {"A.mov": [(0.0, 4.0), (4.5, 9.0), (9.6, 14.0), (14.5, 20.0)],
          "B.mov": [(0.0, 10.0)]}
TEXT_A = "mujhe yeh saree bahut pasand hai kyunki yeh bahut comfortable hai aur sundar bhi"


@pytest.fixture
def clips():
    cs = [{"file": "A.mov", "duration_s": 20.0, "transcript": TEXT_A,
           "turns": [{"start": 0.0, "end": 20.0, "text": TEXT_A}]},
          {"file": "B.mov", "duration_s": 10.0, "transcript": "x", "turns": [{"start": 0.0, "end": 10.0, "text": "one two three four five six seven eight"}]}]
    cuts._speech_cache.update(SPEECH)  # skip ffmpeg silencedetect
    return cs


@pytest.fixture
def by(clips):
    return {c["file"]: c for c in clips}


@pytest.fixture
def run(tmp_path):
    return Run("t-pure", tmp_path)


def beat(clip, a, b, **kw):
    return {"clip": clip, "src_in": a, "src_out": b, "purpose": "p", "visual_note": "v", **kw}


def concept(beats, cid="cx", cta="CTA"):
    return {"id": cid, "title": "t", "angle": "story", "hook": "h", "beats": beats, "cta": cta, "length_s": 15}


# ---------- validate ----------
def test_validate_ok(clips):
    C.validate([concept([beat("A.mov", 1, 7), beat("B.mov", 0, 8)])], clips)


@pytest.mark.parametrize("beats", [
    [beat("nope.mov", 0, 5)],                                   # unknown clip
    [beat("A.mov", 1, 6), beat("A.mov", 5, 11)],                # overlap
    [],                                                         # empty
    [beat("A.mov", 1, 1.4), beat("B.mov", 0, 9)],               # beat too short
    [beat("A.mov", 0, 20), beat("B.mov", 0, 10)],               # 30s total
    [beat("A.mov", 1, 4)],                                      # 3s total
])
def test_validate_rejects(clips, beats):
    with pytest.raises(ValueError):
        C.validate([concept(beats)], clips)


def test_validate_none_src_in_is_typeerror_caught_by_callers(clips):
    with pytest.raises(TypeError):
        C.validate([concept([beat("A.mov", None, 6)])], clips)


def test_validate_duplicate_ids(clips):
    c = concept([beat("A.mov", 0, 12)], "c1")
    with pytest.raises(ValueError):
        C.validate([c, copy.deepcopy(c)], clips)


def test_validate_fills_missing_purpose_and_clamps(clips):
    c = concept([{"clip": "A.mov", "src_in": 12, "src_out": 20.03}, beat("B.mov", 0, 5)])
    C.validate([c], clips)
    assert c["beats"][0]["purpose"] == "" and c["beats"][0]["src_out"] == 20.0


# ---------- speech structure / word times ----------
def test_word_times_monotonic_and_char_weighted(clips):
    wt = cuts.word_times(clips[0], clips[0]["turns"][0])
    assert len(wt) == len(TEXT_A.split())
    assert all(wt[i][2] <= wt[i + 1][1] + 1e-9 for i in range(len(wt) - 1))
    long_w, short_w = next(w for w in wt if w[0] == "comfortable"), next(w for w in wt if w[0] == "hai")
    assert long_w[2] - long_w[1] > short_w[2] - short_w[1]


def test_word_times_empty_and_silent(clips):
    assert cuts.word_times(clips[0], {"start": 0, "end": 5, "text": ""}) == []
    assert cuts.word_times(clips[0], {"start": 4.0, "end": 4.4, "text": "hello there"}) == []


def test_silencedetect_negative_and_unterminated(monkeypatch):
    cuts._speech_cache.pop("N.mov", None)
    stderr = ("silence_start: -0.0012\nsilence_end: 0.9 | silence_duration: 0.9\n"
              "silence_start: 5.0\nsilence_end: 5.5 | silence_duration: 0.5\nsilence_start: 9.0\n")

    class P:
        pass
    p = P()
    p.stderr = stderr
    monkeypatch.setattr(cuts.subprocess, "run", lambda *a, **k: p)
    iv = cuts.speech_intervals({"file": "N.mov", "duration_s": 10.0})
    assert iv == [(0.9, 5.0), (5.5, 9.0)]
    cuts._speech_cache.pop("N.mov", None)


# ---------- trim_dangling ----------
def test_trim_dangling_keeps_hinglish_to(run):
    f = {"file": "F.mov", "duration_s": 10.0, "transcript": "x",
         "turns": [{"start": 0.0, "end": 10.0, "text": "mujhe saree pehenna bahut pasand hai kyunki mujhe yeh pata hai to"}]}
    cuts._speech_cache["F.mov"] = [(0.0, 10.0)]
    c = concept([beat("F.mov", 0.0, 10.0)])
    cuts.trim_dangling(run, c, {"F.mov": f})
    assert c["beats"][0]["src_out"] == 10.0


def test_trim_dangling_cuts_trailing_and(run):
    f = {"file": "F.mov", "duration_s": 10.0, "transcript": "x",
         "turns": [{"start": 0.0, "end": 10.0, "text": "one two three four five six seven and"}]}
    cuts._speech_cache["F.mov"] = [(0.0, 10.0)]
    c = concept([beat("F.mov", 0.0, 10.0)])
    cuts.trim_dangling(run, c, {"F.mov": f})
    assert c["beats"][0]["src_out"] < 10.0


# ---------- snap ----------
def test_snap_idempotent_inside_clip_and_no_overlap(run, by):
    c = concept([beat("A.mov", 4.2, 9.3), beat("A.mov", 9.3, 19.8)])
    cuts.snap_beats(run, c, by)
    first = [(b["src_in"], b["src_out"]) for b in c["beats"]]
    assert c["beats"][1]["src_in"] >= c["beats"][0]["src_out"] - 1e-9
    assert all(0 <= a < z <= 20.0 for a, z in first)
    cuts.snap_beats(run, c, by)
    again = [x for b in c["beats"] for x in (b["src_in"], b["src_out"])]
    assert again == pytest.approx([x for pair in first for x in pair], abs=0.2)


# ---------- captions / titles ----------
def test_caption_chunks_ordered_and_inside(by):
    c = concept([beat("A.mov", 0.0, 9.0), beat("B.mov", 0.0, 6.0)])
    ch = cuts.caption_chunks(c, by)
    total = 15.0
    assert ch and all(ch[i]["end"] <= ch[i + 1]["start"] for i in range(len(ch) - 1))
    assert all(0 <= x["start"] < x["end"] <= total + 0.05 for x in ch)


def test_caption_chunks_empty_turns(by):
    by2 = {"A.mov": {**by["A.mov"], "turns": []}}
    assert cuts.caption_chunks(concept([beat("A.mov", 0, 5)]), by2) == []


def test_title_items_cta_after_content_and_none_ok():
    t = cuts.title_items(concept([beat("A.mov", 0, 5, on_screen_text="Hook!"), beat("A.mov", 6, 9)], cta="Visit"))
    assert t[0]["text"] == "Hook!" and t[-1] == {"text": "Visit", "start": 8.1, "end": 10.0}
    assert cuts.title_items(concept([beat("A.mov", 0, 5, on_screen_text=None)], cta=None)) == []


# ---------- fonts / rendering ----------
def test_bundled_font_present_and_used():
    assert (ROOT / "assets/fonts/NotoSansDevanagari-Medium.ttf").exists()
    assert (ROOT / "assets/fonts/OFL-NotoSansDevanagari.txt").exists()
    assert Path(cuts.DEVA_FONT).exists() and Path(cuts.LATIN_FONT).exists()


def test_render_mixed_script_has_ink_and_no_tofu(tmp_path):
    p = tmp_path / "c.png"
    cuts.render_caption("मैंने साड़ी पहनी Maheshwari saree", "9x16", p)
    im = Image.open(p)
    bbox = im.getchannel("A").getbbox()
    assert bbox and bbox[0] >= 0 and bbox[2] <= im.size[0]
    face = cuts.Face(60)
    assert {f.getname()[0] for _, f in face.runs("साड़ी saree")} >= {"Noto Sans Devanagari", "Noto Sans"} \
        or Path(cuts.DEVA_FONT).suffix == ".ttc"


def test_render_long_word_stays_in_frame(tmp_path):
    p = tmp_path / "l.png"
    cuts.render_caption("x" * 80 + " tail", "9x16", p)
    assert Image.open(p).size[0] == 1080


# ---------- llm._extract_json ----------
@pytest.mark.parametrize("text,expect", [
    ('{"a":1}', {"a": 1}),
    ('```json\n{"a":1}\n```', {"a": 1}),
    ('{"a":1}\nNote: use {curly} sparingly', {"a": 1}),
    ('Thinking {draft: no} final: {"a":1}', {"a": 1}),
    ('{"a":1} {"b":2}', {"a": 1}),
])
def test_extract_json(text, expect):
    assert llm._extract_json(text) == expect


def test_extract_json_none():
    with pytest.raises(ValueError):
        llm._extract_json("no json here {")


# ---------- trace / contracts ----------
def test_trace_rejects_bad_records():
    bad = {"run_id": "r", "step": 1, "tool": "t", "started_at": "2026-11-03T00:00:00+00:00", "duration_ms": 1,
           "status": "weird"}
    assert list(trace._TRACE.iter_errors(bad))


def test_contract_allows_human_and_orchestrator():
    ok = {"run_id": "r", "step": 1, "from_role": "human", "to_role": "orchestrator", "input": {}, "output": {},
          "created_at": "2026-11-03T00:00:00+00:00"}
    assert not list(trace._CONTRACT.iter_errors(ok))
    assert list(trace._CONTRACT.iter_errors({**ok, "step": 0}))


def test_run_resumes_step_counter(tmp_path):
    r1 = Run("a", tmp_path)
    r1.trace("t", {}, {})
    r1.trace("t", {}, {})
    assert Run("a", tmp_path).step == 2


@pytest.mark.parametrize("rid", ["../x", "a/b", "", "x" * 65, "a b"])
def test_run_id_validated(tmp_path, rid):
    with pytest.raises(ValueError):
        Run(rid, tmp_path)


# ---------- QC keyframes / degrade ----------
def test_keyframes_sample_cta_hold(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(qc.subprocess, "check_output", lambda *a, **k: "20.67\n")

    def fake_run(cmd, **k):
        seen.append(float(cmd[cmd.index("-ss") + 1]))
        Path(cmd[-1]).write_bytes(b"jpg")
    monkeypatch.setattr(qc.subprocess, "run", fake_run)
    urls = qc.keyframes(tmp_path / "v.mp4")
    assert len(urls) == 4 and seen[-1] >= 20.67 - 2.0 and seen[-1] < 20.67  # inside the 2s CTA hold


def test_qc_review_degrades_instead_of_crashing(monkeypatch, tmp_path, run):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(qc, "_call", boom)
    v = qc.review(run, "brief", {"id": "c1"}, [{"start": 0, "end": 1, "text": "x"}], tmp_path / "v.mp4")
    assert v["pass"] is None and v["score"] is None and v["issues"] == ["qc unavailable"]
    rec = [__import__("json").loads(l) for l in (run.dir / "trace.jsonl").read_text().splitlines()]
    assert rec[-1]["status"] == "error"


def test_qc_pass_string_false_is_not_pass(monkeypatch, tmp_path, run):
    monkeypatch.setattr(qc, "_call", lambda *a, **k: ({"pass": "false", "score": 2, "summary": "s"}, {}))
    qc.review(run, "b", {"id": "c1"}, [], tmp_path / "v.mp4")
    last = __import__("json").loads((run.dir / "trace.jsonl").read_text().splitlines()[-1])
    assert "FAIL" in last["decision"]

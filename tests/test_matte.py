"""Matte extractor tests: synthetic masks through parse/decode/placement/interpolation/feather; SAM is mocked (no network)."""
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from adfarming import matte as M  # noqa: E402
from adfarming.trace import Run  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
codec = pytest.importorskip("meta_sam_parser._mask_codec")


def token(frame, box, raster, mx=0, my=0, fw=64, fh=96):
    """One synthetic SAM token; raster is a BOX-sized 0/1 array."""
    h, w = raster.shape
    payload = codec._encode_raster(raster.flatten().tolist(), w, h)
    x1, y1, x2, y2 = box
    pre = f"<{frame}f>0" if frame is not None else ""
    return f"{pre}<|box;x1={x1};y1={y1};x2={x2};y2={y2};w={fw};h={fh}|><|mask;x={mx};y={my};data={h},{w},{payload}|>"


def blob(h, w):
    r = np.zeros((h, w), np.uint8)
    r[2:h - 2, 3:w - 3] = 1
    return r


def test_parse_and_decode_places_raster_at_box_origin():
    ras = blob(20, 12)
    toks = M.parse_masks(token(3, (10, 30, 22, 50), ras))
    assert len(toks) == 1 and toks[0]["frame"] == 3 and toks[0]["box"] == (10, 30, 22, 50)
    ch = M.select_largest(toks)
    out = M.decode_tokens(ch, procs=1)[3]
    assert out.shape == (96, 64)
    assert out.max() == 255
    ys, xs = np.nonzero(out)
    assert (xs.min(), xs.max()) == (10 + 3, 10 + 12 - 4) and (ys.min(), ys.max()) == (30 + 2, 30 + 20 - 3)
    assert out.sum() // 255 == ras.sum()  # nothing lost, nothing at frame origin


def test_mask_offset_is_relative_to_box_and_clipped():
    ras = np.ones((20, 12), np.uint8)
    full = M.place_mask(ras, (60, 90, 72, 110), 0, 0, 64, 96)  # box hangs off the frame
    assert full.shape == (96, 64) and full[90:, 60:].all() and full.sum() == 6 * 4
    neg = M.place_mask(ras, (0, 0, 12, 20), -5, -5, 64, 96)
    assert neg[:15, :7].all() and neg.sum() == 7 * 15
    assert M.place_mask(ras, (200, 200, 212, 220), 0, 0, 64, 96).sum() == 0


def test_largest_box_wins_and_multiprocess_matches_serial():
    small, big = blob(10, 8), blob(30, 20)
    text = token(0, (0, 0, 8, 10), small) + token(0, (20, 40, 40, 70), big) + token(1, (5, 5, 13, 15), small)
    ch = M.select_largest(M.parse_masks(text))
    assert ch[0]["box"] == (20, 40, 40, 70) and set(ch) == {0, 1}
    a, b = M.decode_tokens(ch, procs=1), M.decode_tokens(ch, procs=2)
    assert all((a[k] == b[k]).all() for k in a)
    assert a[0].sum() // 255 == big.sum()


def test_image_response_without_frame_prefix():
    toks = M.parse_masks(token(None, (4, 4, 16, 24), blob(20, 12)))
    assert toks[0]["frame"] is None
    assert M.decode_tokens(M.select_largest(toks), procs=1)[0].max() == 255


def test_fit_masks_resizes_mismatch():
    m = {0: np.full((96, 64), 255, np.uint8), 1: np.full((48, 32), 255, np.uint8)}
    out = M.fit_masks(m, (64, 96))
    assert out[0].shape == out[1].shape == (96, 64) and out[1].min() == 255


def test_interpolation_blend_hold_and_gaps():
    z, o = np.zeros((4, 4), np.uint8), np.full((4, 4), 200, np.uint8)
    masks = {0: z, 2: o}  # frame 1 missing (gap) -> linear across the gap
    assert M.interp_alpha(masks, 1.0, (4, 4))[0, 0] == pytest.approx(100)
    assert M.interp_alpha(masks, 0.5, (4, 4))[0, 0] == pytest.approx(50)
    assert M.interp_alpha(masks, 9.0, (4, 4))[0, 0] == 200   # past the sampled range: hold last
    assert M.interp_alpha(masks, -1.0, (4, 4))[0, 0] == 0
    assert M.interp_alpha({}, 1.0, (4, 4)).sum() == 0


def test_feather_still_softens_edge_and_keeps_size(tmp_path):
    m = np.zeros((96, 54), np.uint8)
    m[:, 27:] = 255  # hard vertical edge at SAM resolution
    out = tmp_path / "a.png"
    M.feather_still(m, (1080, 1920), out)
    a = np.asarray(Image.open(out))
    assert a.shape == (1920, 1080) and a.dtype == np.uint8
    row = a[960]
    assert row[0] == 0 and row[-1] == 255
    assert 0 < (row[(row > 0) & (row < 255)]).size <= 14  # a soft ramp, not a stair-step, but narrow


def make_video(path, frames=20, fps=10, size="64x96"):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}",
                    "-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def sam_text(frames, fw=32, fh=48, shift=0):
    r = blob(20, 14)
    return "".join(token(f, (8 + shift * f, 10, 22 + shift * f, 30), r, fw=fw, fh=fh) for f in frames)


@pytest.fixture
def run(tmp_path):
    return Run("matte-test", tmp_path / "out")


@pytest.fixture
def mocked(monkeypatch):
    calls = []
    monkeypatch.setattr(M, "_upload", lambda p: "file_x")
    monkeypatch.setattr(M, "_delete_file", lambda f: None)
    return calls


def test_extract_matte_end_to_end_aligned_and_cached(tmp_path, run, monkeypatch, mocked):
    vid = tmp_path / "v.mp4"
    make_video(vid, frames=20, fps=10)  # 2 s source @10fps, 64x96
    calls = []

    def fake(part, prompt, deadline, mask_encoding="one_bit"):
        calls.append(prompt)
        return sam_text(range(10), shift=1)  # SAM sampled at 5 fps -> 10 frames, size 32x48 (width=32)

    monkeypatch.setattr(M, "_sam_call", fake)
    res = M.extract_matte(run, vid, tmp_path / "m.mp4", sample_fps=5, width=32, cache_dir=tmp_path / "c", procs=1)
    assert "error" not in res, res
    assert res["frames"] == 20 and res["sampled_frames"] == 10 and res["head_top_frac"] == pytest.approx(10 / 48, abs=1e-3)
    assert res["cost_usd"] == pytest.approx(50 * M.COST_PER_FRAME_USD)  # 50-frame minimum
    probe = M._probe(tmp_path / "m.mp4")
    assert (probe["w"], probe["h"], probe["frames"]) == (64, 96, 20)  # source size, frame-for-frame
    # subject moves right over time: alpha centroid at the end is right of the start
    def cx(i):
        f = tmp_path / f"f{i}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(tmp_path / "m.mp4"), "-vf", f"select='eq(n,{i})'", "-frames:v", "1", str(f)], check=True)
        a = np.asarray(Image.open(f).convert("L")).astype(float)
        return (a.sum(0) * np.arange(a.shape[1])).sum() / a.sum()
    assert cx(18) > cx(1) + 4
    again = M.extract_matte(run, vid, tmp_path / "m2.mp4", sample_fps=5, width=32, cache_dir=tmp_path / "c", procs=1)
    assert again["cached"] is True and again["cost_usd"] == 0.0 and len(calls) == 1
    assert (tmp_path / "m2.mp4").read_bytes() == (tmp_path / "m.mp4").read_bytes()
    # different params must not hit the cache
    M.extract_matte(run, vid, tmp_path / "m3.mp4", prompt="girl", sample_fps=5, width=32, cache_dir=tmp_path / "c", procs=1)
    assert len(calls) == 2


def test_clip_longer_than_sampled_range_holds_last_mask(tmp_path, run, monkeypatch, mocked):
    vid = tmp_path / "v.mp4"
    make_video(vid, frames=20, fps=10)
    monkeypatch.setattr(M, "_sam_call", lambda *a, **k: sam_text(range(3)))  # SAM only covered 0.6 s
    res = M.extract_matte(run, vid, tmp_path / "m.mp4", sample_fps=5, width=32, cache_dir=tmp_path / "c", procs=1)
    assert "error" not in res and res["frames"] == 20 and res["sampled_frames"] == 3


def test_prompt_retry_with_person_then_no_match_error(tmp_path, run, monkeypatch, mocked):
    vid = tmp_path / "v.mp4"
    make_video(vid)
    seen = []

    def retry(part, prompt, deadline, mask_encoding="one_bit"):
        seen.append(prompt)
        return "" if prompt == "woman" else sam_text(range(10))

    monkeypatch.setattr(M, "_sam_call", retry)
    res = M.extract_matte(run, vid, tmp_path / "m.mp4", sample_fps=5, width=32, cache_dir=tmp_path / "c1", procs=1)
    assert "error" not in res and seen == ["woman", "person"] and res["prompt"] == "person"
    assert res["cost_usd"] == pytest.approx(2 * 50 * M.COST_PER_FRAME_USD)

    monkeypatch.setattr(M, "_sam_call", lambda *a, **k: "")
    res = M.extract_matte(run, vid, tmp_path / "n.mp4", sample_fps=5, width=32, cache_dir=tmp_path / "c2", procs=1)
    assert "no match" in res["error"] and res["matte"] is None and not (tmp_path / "n.mp4").exists()


def test_sam_failure_and_missing_file_never_raise(tmp_path, run, monkeypatch, mocked):
    vid = tmp_path / "v.mp4"
    make_video(vid)

    def boom(*a, **k):
        raise M.MatteError("time budget exceeded while streaming SAM response")

    monkeypatch.setattr(M, "_sam_call", boom)
    res = M.extract_matte(run, vid, tmp_path / "m.mp4", cache_dir=tmp_path / "c")
    assert "budget" in res["error"]
    assert "error" in M.extract_matte(run, tmp_path / "nope.mp4", tmp_path / "m.mp4", cache_dir=tmp_path / "c")


def test_extract_matte_still_mocked(tmp_path, monkeypatch):
    img = Image.new("RGB", (80, 120), (200, 50, 50))
    img.save(tmp_path / "s.png")
    sent = {}

    def fake(part, prompt, deadline, mask_encoding="one_bit"):
        sent["part"] = part["type"]
        sent["url"] = part["image_url"][:23]
        return token(None, (8, 10, 22, 30), blob(20, 14), fw=80, fh=120)

    monkeypatch.setattr(M, "_sam_call", fake)
    res = M.extract_matte_still(tmp_path / "s.png", tmp_path / "a.png", cutout_png=tmp_path / "c.png")
    assert "error" not in res and sent == {"part": "input_image", "url": "data:image/jpeg;base64,"}
    a = Image.open(tmp_path / "a.png")
    assert a.mode == "L" and a.size == (80, 120) and a.getextrema() == (0, 255)
    assert Image.open(tmp_path / "c.png").mode == "RGBA"
    monkeypatch.setattr(M, "_sam_call", lambda *a, **k: "")
    err = M.extract_matte_still(tmp_path / "s.png", tmp_path / "b.png")
    assert "no match" in err["error"] and err["matte"] is None
    assert "error" in M.extract_matte_still(tmp_path / "missing.png", tmp_path / "b.png")


# ---------------------------------------------------------------- F3: stats, cost on failure, procs, atomic cache

def test_subject_stats_median_bbox_and_head_size():
    toks = {}
    for f, (x1, y1, x2, y2) in enumerate([(8, 10, 22, 30), (8, 12, 22, 32), (10, 14, 24, 34)]):
        toks[f] = {"box": (x1, y1, x2, y2), "w": 32, "h": 48}
    st = M.subject_stats(toks)
    assert st["subject_bbox_frac"] == [round(8 / 32, 4), round(12 / 48, 4), round(22 / 32, 4), round(32 / 48, 4)]
    assert st["head_top_frac"] == st["subject_bbox_frac"][1] == round(12 / 48, 4)
    assert st["head_size_frac"] == round(20 / 48, 4)
    none = M.subject_stats({})
    assert none["subject_bbox_frac"] is None and none["head_top_frac"] is None and none["head_size_frac"] is None
    clamp = M.subject_stats({0: {"box": (-5, -5, 40, 60), "w": 32, "h": 48}})
    assert clamp["subject_bbox_frac"] == [0.0, 0.0, 1.0, 1.0]


def test_extract_matte_reports_bbox_and_head_size_and_caches_them(tmp_path, run, monkeypatch, mocked):
    vid = tmp_path / "v.mp4"
    make_video(vid)
    monkeypatch.setattr(M, "_sam_call", lambda *a, **k: sam_text(range(10)))
    res = M.extract_matte(run, vid, tmp_path / "m.mp4", sample_fps=5, width=32, cache_dir=tmp_path / "c", procs=1)
    assert res["subject_bbox_frac"] == [0.25, round(10 / 48, 4), round(22 / 32, 4), round(30 / 48, 4)]
    assert res["head_size_frac"] == round(20 / 48, 4)
    again = M.extract_matte(run, vid, tmp_path / "m2.mp4", sample_fps=5, width=32, cache_dir=tmp_path / "c", procs=1)
    assert again["cached"] and again["subject_bbox_frac"] == res["subject_bbox_frac"] and again["head_size_frac"] == res["head_size_frac"]
    assert not list((tmp_path / "c").rglob("*.tmp"))  # atomic writes leave no temp files


def test_cost_recorded_on_failure_including_person_retry(tmp_path, run, monkeypatch, mocked):
    vid = tmp_path / "v.mp4"
    make_video(vid)
    monkeypatch.setattr(M, "_sam_call", lambda *a, **k: "")
    res = M.extract_matte(run, vid, tmp_path / "m.mp4", sample_fps=5, width=32, cache_dir=tmp_path / "c")
    assert "no match" in res["error"] and res["sam_calls"] == 2
    assert res["cost_usd"] == pytest.approx(2 * 50 * M.COST_PER_FRAME_USD)

    seen = []

    def second_fails(part, prompt, deadline, mask_encoding="one_bit"):
        seen.append(prompt)
        if prompt == "woman":
            return ""
        raise M.MatteError("SAM HTTP 500")

    monkeypatch.setattr(M, "_sam_call", second_fails)
    res = M.extract_matte(run, vid, tmp_path / "m.mp4", sample_fps=5, width=32, cache_dir=tmp_path / "c2")
    assert seen == ["woman", "person"] and res["cost_usd"] == pytest.approx(2 * 50 * M.COST_PER_FRAME_USD)
    # the trace carries the same cost (spend is visible in the run ledger)
    assert any(abs((e.get("cost_usd") or 0) - res["cost_usd"]) < 1e-9 for e in _trace_events(run))


def _trace_events(run):
    import json
    out = []
    for f in Path(run.dir).rglob("*.jsonl"):
        out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return out


def test_still_cost_on_failure_and_stats(tmp_path, monkeypatch):
    Image.new("RGB", (80, 120), (9, 9, 9)).save(tmp_path / "s.png")
    monkeypatch.setattr(M, "_sam_call", lambda *a, **k: "")
    err = M.extract_matte_still(tmp_path / "s.png", tmp_path / "a.png")
    assert err["cost_usd"] == pytest.approx(2 * M.COST_PER_IMAGE_USD)
    monkeypatch.setattr(M, "_sam_call", lambda *a, **k: token(None, (8, 10, 22, 30), blob(20, 14), fw=80, fh=120))
    ok = M.extract_matte_still(tmp_path / "s.png", tmp_path / "a.png")
    assert ok["subject_bbox_frac"] == [0.1, round(10 / 120, 4), 0.275, round(30 / 120, 4)]
    assert ok["head_top_frac"] == round(10 / 120, 4) and ok["head_size_frac"] == round(20 / 120, 4)


def test_decode_procs_env_default_and_affinity_cap(monkeypatch):
    monkeypatch.delenv("ADF_DECODE_PROCS", raising=False)
    monkeypatch.setattr(M, "available_cpus", lambda: 8)
    assert M.decode_procs() == 2                      # default 2
    monkeypatch.setenv("ADF_DECODE_PROCS", "4")
    assert M.decode_procs() == 4
    monkeypatch.setenv("ADF_DECODE_PROCS", "junk")
    assert M.decode_procs() == 2
    monkeypatch.setenv("ADF_DECODE_PROCS", "6")
    monkeypatch.setattr(M, "available_cpus", lambda: 2)  # 2 vCPU container: never oversubscribe
    assert M.decode_procs() == 2 and M.decode_procs(8) == 2
    assert M.decode_procs(1) == 1


def test_available_cpus_uses_affinity(monkeypatch):
    monkeypatch.setattr(M.os, "sched_getaffinity", lambda pid: {0, 1, 2}, raising=False)
    assert M.available_cpus() == 3
    monkeypatch.delattr(M.os, "sched_getaffinity", raising=False)
    assert M.available_cpus() >= 1


def test_atomic_write_replaces_and_leaves_no_temp(tmp_path):
    d = tmp_path / "x" / "f.bin"
    M._atomic_write(d, data=b"one")
    M._atomic_write(d, data=b"two")
    assert d.read_bytes() == b"two" and [p.name for p in d.parent.iterdir()] == ["f.bin"]


def test_failed_matte_leaves_no_partial_output_or_cache(tmp_path, run, monkeypatch, mocked):
    vid = tmp_path / "v.mp4"
    make_video(vid)
    monkeypatch.setattr(M, "_sam_call", lambda *a, **k: sam_text(range(10)))

    def bad_write(*a, **k):
        raise M.MatteError("ffmpeg matte encode failed: boom")

    monkeypatch.setattr(M, "_write_matte", bad_write)
    res = M.extract_matte(run, vid, tmp_path / "m.mp4", sample_fps=5, width=32, cache_dir=tmp_path / "c", procs=1)
    assert "boom" in res["error"] and not (tmp_path / "m.mp4").exists()
    assert not list((tmp_path / "c").rglob("meta.json"))

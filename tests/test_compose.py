"""Composition renderer tests: synthetic data only, no network, no client footage."""
import copy
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from adfarming import compose, compose_text as ct  # noqa: E402


def ff(*a):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *a], check=True)


@pytest.fixture(scope="module")
def assets(tmp_path_factory):
    d = tmp_path_factory.mktemp("cmp")
    ff("-f", "lavfi", "-i", "testsrc2=s=270x480:r=30:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "master.mp4"))
    m = Image.new("L", (270, 480), 0)
    ImageDraw.Draw(m).ellipse([60, 120, 210, 480], fill=255)
    m.save(d / "m.png")
    ff("-loop", "1", "-framerate", "30", "-t", "3", "-i", str(d / "m.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "matte.mp4"))
    Image.new("RGB", (400, 700), (200, 190, 180)).save(d / "wall.png")   # bright plate: must be auto-dimmed
    Image.new("RGB", (300, 400), (30, 120, 200)).save(d / "pip.png")
    return {"master": str(d / "master.mp4"), "matte": str(d / "matte.mp4"), "backdrops": {"wall": str(d / "wall.png")},
            "pip_images": {"pip": str(d / "pip.png")}, "dir": d}


def make_plan():
    return {"version": 1, "preset": "cutout_on_brand_colour", "size": [1080, 1920], "fps": 30, "duration": 3,
            "subject": {"segments": [{"beat": 0, "clip": "a", "in": 0, "out": 3}], "mask": {"prompt": "woman", "feather": 2.0, "edge": [0.35, 0.65]},
                        "transform": {"scale": 1.0, "x": 0, "y": 0}, "shadow": 0.5},
            "background": [
                {"id": "bg1", "kind": "gradient", "colors": ["#3b0a2a", "#c2410c"], "src": None, "prompt": None, "blur": 0, "zoom": 1.0, "dim": 1.0, "start": 0, "transition_in": {"type": "cut", "dur": 0}},
                {"id": "bg2", "kind": "blur_still", "colors": ["#2a1020"], "src": "wall", "prompt": None, "blur": 10, "zoom": 1.0, "dim": 1.0, "start": 1.5, "transition_in": {"type": "fade", "dur": 0.6}}],
            "text": [{"id": "hook", "role": "hook", "text": "हैंडलूम साड़ी में क्या देखें?", "region": "top_center", "font": "devanagari", "size": 108,
                      "style": "pill_headline", "fill": "#ffffff", "stroke": None, "plate": "#1c0714", "pill_text": "BUYING GUIDE", "rot": 0,
                      "animation": "pop", "start": 0.1, "end": 2.8}],
            "captions": {"style": "pop_captions", "source": "plan", "chunks": [{"text": "पहचानिए असली बुनाई", "start": 0.5, "end": 2.5}],
                         "fill": "#ffffff", "highlight": "#fde047", "stroke": [9, "#1a0a14"], "scrim": 0.62},
            "overlays": [{"id": "pip", "src": "asset:pip", "region": "right_mid", "size": [270, 350], "rot": 5, "animation": "slide_right", "start": 1.0, "end": 2.8}],
            "audio": {"source": "subject", "music": None}}


def test_plan_validation_and_lint_clean(assets):
    p = make_plan()
    assert compose.validate_plan(p) == []
    assert compose.lint_render_inputs(p, assets) == []
    bad = copy.deepcopy(p)
    del bad["subject"]
    assert compose.validate_plan(bad)


def test_lint_problems(assets):
    p = make_plan()
    a = dict(assets, backdrops={}, matte="/nope.mp4", flags={})
    probs = " | ".join(compose.lint_render_inputs(p, a))
    assert "missing asset 'matte'" in probs and "backdrop image" in probs
    p["text"][0]["box"] = [54, 100, 1026, 400]       # custom box poking into the Reels header zone
    p["text"][0]["fill"] = "#222222"                  # dark on dark panel
    assert any("safe zone" in s for s in compose.lint_render_inputs(p, assets))
    assert any("low contrast" in s for s in compose.lint_render_inputs(p, assets))
    q = make_plan()
    q["background"][1]["has_person"] = True
    assert any("contains a person" in s for s in compose.lint_render_inputs(q, assets))
    assets2 = dict(assets, flags={"wall": {"person": True}})
    assert any("contains a person" in s for s in compose.lint_render_inputs(make_plan(), assets2))
    r = make_plan()
    r["captions"]["scrim"] = 0.1
    assert any("scrim" in s for s in compose.lint_render_inputs(r, assets))


def test_safe_zone_logic():
    assert ct.SAFE["top"] == 230 and ct.SAFE["bottom"] == 1574
    assert all(ct.box_in_safe(r[:4]) for r in ct.REGIONS.values())
    assert not ct.box_in_safe((54, 100, 900, 300))
    assert "Reels header" in ct.box_overflow((54, 100, 900, 300))[0]
    assert "caption/UI" in ct.box_overflow((54, 1500, 900, 1700))[0]


def test_contrast_function():
    assert ct.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert ct.contrast_ratio("#777777", "#777777") == pytest.approx(1.0)
    assert ct.contrast_ratio((255, 255, 255), "#1c0714") > 15
    assert ct.contrast_ratio("#fde047", "#fbbf24") < 1.5


def test_text_wrapping_devanagari():
    txt = "हैंडलूम साड़ी में क्या देखें और कैसे पहचानें असली ज़री"
    lines = ct.wrap_text(txt, 80, 500)
    assert len(lines) >= 3
    assert " ".join(lines) == txt                      # nothing lost or reordered
    f = ct.face(80)
    assert all(f.length(ct._DUMMY, l) <= 500 for l in lines)
    # a single overlong word is hard-broken but never starts a line with a combining mark
    for l in ct.wrap_text("असाधारणरूपसेलम्बाशब्दजोकहींनहींटूटता", 80, 300):
        assert not ct._is_mark(l[0])
    lines2, size = ct.fit_text(txt, 108, 600, 300, 3)
    assert size < 108 and len(lines2) <= 3
    img, _, info = ct.render_block({"text": "असली ज़री बॉर्डर", "region": "left_mid", "style": "callout_tag", "plate": "#fbbf24",
                                    "fill": "#2a0a1a", "stroke": None, "rot": -6, "size": 64})
    assert info["fits"] and img.getbbox() and info["tail"] == "down_right"


def test_preview_stills(assets, tmp_path):
    r = compose.render_composition(make_plan(), assets, tmp_path / "prev", preview=True)
    assert len(r["frames"]) == 2
    for f in r["frames"]:
        assert Image.open(f).size == (540, 960)
    assert Image.open(r["out"]).size[1] == 960
    # the subject is cut out: some pixel differs from the bare gradient/background at the ellipse centre
    im = Image.open(r["frames"][0]).convert("RGB")
    bare = compose.prep_bg(make_plan()["background"][0], assets).resize((540, 960)).convert("RGB")
    diff = sum(abs(a - b) for a, b in zip(im.getpixel((270, 400)), bare.getpixel((270, 400))))
    assert diff > 30
    # bright plate was auto-dimmed
    assert compose._mean_luma(compose.prep_bg(make_plan()["background"][1], assets)) <= compose.MAX_BG_LUMA + 0.02


def test_render_deterministic_and_audio_copied(assets, tmp_path):
    p = make_plan()
    outs = []
    for i in range(2):
        r = compose.render_composition(p, assets, tmp_path / f"o{i}.mp4", size=(270, 480), workdir=tmp_path / f"w{i}")
        outs.append(Path(r["out"]))
    h = [hashlib.sha256(o.read_bytes()).hexdigest() for o in outs]
    assert h[0] == h[1]
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "csv", str(outs[0])],
                          capture_output=True, text=True).stdout
    assert "h264,video,270,480" in info and "aac,audio" in info
    assert {x["kind"] for x in r["layers"]} == {"scrim", "caption", "text", "overlay"} and r["seconds"] > 0

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


# ---------- F1 fixes: pixels, head-aware layout, ratios, hardening ----------
def _held_assets(d, secs=9):
    """5 fps held-still master/matte whose first frame sits at pts 0.1 (the real-run shape that gave black stills)."""
    # built like agent d_still_video: looped stills held at 5 fps and concatenated (first pts offset, sparse frames)
    for nm, col in (("a", "0x2060c0"), ("b", "0x2060c0")):
        Image.new("RGB", (270, 480), (32, 96, 192)).save(d / f"s{nm}.png")
    m = Image.new("L", (270, 480), 0)
    ImageDraw.Draw(m).ellipse([40, 100, 230, 480], fill=255)
    m.save(d / "hmat.png")
    Image.new("L", (270, 480), 0).save(d / "blank.png")
    for out, img in (("hm.mp4", "sa.png"), ("hmatte.mp4", "hmat.png")):
        ff("-loop", "1", "-framerate", "5", "-t", str(secs / 2), "-i", str(d / img), "-loop", "1", "-framerate", "5", "-t", str(secs / 2 + 0.3), "-i", str(d / img),
           "-filter_complex", "[0:v]scale=270:480,format=yuv420p,setsar=1[a];[1:v]scale=270:480,format=yuv420p,setsar=1[b];[a][b]concat=n=2:v=1:a=0,setpts=PTS+0.1/TB[o]",
           "-map", "[o]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "5", str(d / out))
    return {"master": str(d / "hm.mp4"), "matte": str(d / "hmatte.mp4"), "backdrops": {}, "pip_images": {}}


def test_preview_subject_pixels_on_held_still_master(tmp_path):
    a = _held_assets(tmp_path)
    p = make_plan()
    p["duration"] = 9
    p["subject"]["segments"][0]["out"] = 9
    p["background"] = p["background"][:1]
    p["text"] = [{**p["text"][0], "start": 6.5, "end": 8.5, "plate": None, "stroke": None}]
    p["captions"]["chunks"] = []
    p["overlays"] = []
    p["captions"]["scrim"] = 0.5
    t = compose.best_preview_time(p, 0)
    assert 6.5 <= t <= 8.5                       # the headline is on screen at the preview time
    r = compose.render_composition(p, a, tmp_path / "pv", preview=True)
    im = Image.open(r["frames"][0]).convert("RGB")
    cx = im.getpixel((270, 700))                 # inside the ellipse, below the headline
    assert cx[2] > 120 and cx[2] > cx[0] + 40    # the blue subject, not black / bare background
    p2 = copy.deepcopy(p)
    subj = tmp_path / "s.png"
    compose._subject_png(a["master"], a["matte"], 7.5, p2, subj)
    assert Image.open(subj).convert("RGBA").getpixel((540, 1400))[3] > 200


def test_head_aware_text_and_callout():
    lay = ct.get_layout("9x16")
    head = (300.0, 520.0, 780.0, 940.0)
    hook = {"text": "Beginner thi, phir bhi comfortable", "region": "top_center", "style": "pill_headline", "size": 96,
            "plate": None, "stroke": [6, "#2a0a1a"], "pill_text": "STORY", "keyword": "comfortable"}
    img, c, info = ct.render_block(hook, lay, head)
    assert info["box"][3] <= head[1] - ct.HEAD_GAP + 2 and info["box"][1] >= 230
    assert info["hl"] is not None and info["lines"][-1].split()[-1] == "comfortable"
    tag = {"text": "Perfect for festive", "region": "right_mid", "style": "callout_tag", "plate": "#fbbf24", "fill": "#2a0a1a", "stroke": None, "size": 64}
    for h in (head, (150.0, 520.0, 630.0, 940.0)):
        _, _, ci = ct.render_block(tag, lay, h)
        b = ci["box"]
        assert not (b[0] < h[2] and b[2] > h[0] and b[1] < h[3] and b[3] > h[1])
        assert ci["tail"] in ("down_left", "down_right") and ci["box"][1] >= h[3] - 1 or ci["box"][3] <= 1574
    # cta shows the short field, never the internal direction
    _, _, k = ct.render_block({"text": "See this on our website - soft end card", "cta_short": "Shop Maheshwari", "region": "cta_top",
                               "style": "cta_pill", "plate": "#e11d48"}, lay, None)
    assert k["lines"] == ["Shop Maheshwari"]


def test_caption_baseline_and_soft_scrim():
    lay = ct.get_layout("9x16")
    box = tuple(lay.cap_box)
    st = {**ct.ROLE_STYLE["caption"], "stroke": [9, "#1a0a14"]}
    i1, c1, _ = ct.render_caption("nahi aata", st, box, None, lay)
    i2, c2, _ = ct.render_caption("mere liye perfect saree ab", st, box, None, lay)
    assert round(c1[1] + i1.height / 2) == round(c2[1] + i2.height / 2)      # same bottom edge: no jumping
    img, _ = ct.render_scrim(box, 0.9, lay)
    a = img.getchannel("A")
    assert img.height >= 400 and max(a.getdata()) <= round(255 * ct.SCRIM_CAP) + 1
    col = [a.getpixel((10, y)) for y in range(img.height)]
    assert max(abs(col[i + 1] - col[i]) for i in range(len(col) - 1)) <= 8   # no hard edge
    assert ct.render_scrim(tuple(ct.get_layout("16x9").cap_box), 0.5, ct.get_layout("16x9")) is None


def test_scale_beats_and_events():
    lay = ct.get_layout("9x16")
    p = make_plan()
    p["subject"]["segments"] = [{"beat": 0, "clip": "a", "in": 0, "out": 1.5}, {"beat": 1, "clip": "b", "in": 0, "out": 1.5, "subject_scale": 1.1,
                                "transition_in": {"type": "push", "dur": 0.25}}]
    assert compose.scale_at(p, lay, 0.5) == pytest.approx(1.0)
    assert compose.scale_at(p, lay, 1.5) == pytest.approx(1.1 * 1.1, rel=1e-3)          # push-in starts 10 percent big
    assert compose.scale_at(p, lay, 2.5) == pytest.approx(1.1, rel=1e-3)                # settled
    assert "pow(" in compose._scale_expr(p, lay)


def test_ratio_layouts_render_and_flash(assets, tmp_path):
    p = make_plan()
    p["meta"] = {"head": {"top_frac": 0.25, "bbox_frac": [0.1, 0.25, 0.9, 1.0]}}
    p["subject"]["segments"] = [{"beat": 0, "clip": "a", "in": 0, "out": 1.5}, {"beat": 1, "clip": "b", "in": 0, "out": 1.5, "subject_scale": 1.08,
                                "transition_in": {"type": "flash", "dur": 0.2}}]
    p["subject"]["rim"] = {"enabled": True, "px": 2, "color": "#ffe9d2"}
    for name, size, expect in (("1x1", (540, 540), "1x1"), ("16x9", (960, 540), "16x9"), ("9x16", (270, 480), "9x16")):
        r = compose.render_composition(p, assets, tmp_path / f"{name}.mp4", size=size, workdir=tmp_path / f"w{name}")
        assert r["layout"] == expect
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height", "-of", "csv=p=0", r["out"]],
                               capture_output=True, text=True).stdout.strip()
        assert probe == f"{size[0]},{size[1]}"
        pv = compose.render_composition(p, assets, tmp_path / f"pv{name}", size=size, preview=True)
        assert Image.open(pv["frames"][0]).size == (ct.layout_for_size(size).w // 2, ct.layout_for_size(size).h // 2)


def test_hardening(assets, tmp_path, monkeypatch):
    assert compose._assets_get({"backdrops": {}}, "backdrops", str(assets["master"])) is None   # no raw-path fallback
    bad = make_plan()
    del bad["subject"]
    with pytest.raises(ValueError, match="invalid composition plan"):
        compose.render_composition(bad, assets, tmp_path / "x.mp4")
    assert len(compose._sha1("frame:a@1")) == 12 and compose._sha1("a") == compose._sha1("a")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 1)
    monkeypatch.setattr(compose.subprocess, "run", boom)
    with pytest.raises(compose.ComposeFail, match="timed out"):
        compose._run(["ffmpeg"], 1)
    monkeypatch.undo()
    out = tmp_path / "fail.mp4"
    monkeypatch.setattr(compose, "FULL_TIMEOUT", 0.01)
    with pytest.raises(compose.ComposeFail):
        compose.render_composition(make_plan(), assets, out)
    assert not out.exists()

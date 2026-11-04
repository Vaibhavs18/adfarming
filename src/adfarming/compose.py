"""Deterministic composition renderer: speaker cut out (SAM matte) over changing backgrounds, stylised text in named
regions, pop-in captions on a scrim, a PiP card. The AI decides (the composition plan JSON, see
schemas/composition_plan.schema.json); this module only executes it. No network, no paid call here.

    render_composition(plan, assets, out_path, *, size=(1080,1920), fps=30, preview=False, workdir=None)
    lint_render_inputs(plan, assets)            -> list[str]   (human-readable problems)
    validate_plan(plan)                         -> list[str]   (schema errors)

assets = {"master": mp4 (speech-cut 9:16 with audio), "matte": grayscale mp4 aligned to master,
          "backdrops": {name: image path}, "pip_images": {name: image path},
          "clips": {clip name: video path}   # only needed for pip src "frame:<clip>@<sec>"
          "flags": {backdrop name: {"person": true}}   # optional: lint rejects person-bearing background stills}

Technique (proved by the compose spike): every animated layer is a short PNG sequence on a shared canvas, held with
tpad and timed with overlay enable. Background stills get a slow Ken Burns zoompan; beats dissolve with xfade.
Subject = master + matte through scale, gblur, curves (feather), alphamerge, with a quarter-res drop shadow.
Audio is stream-copied from the master. Design space is 1080x1920; another `size` just rescales the output.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

from . import compose_text as ct
from .compose_text import H, W

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas/composition_plan.schema.json"
KB_ZOOM = 0.06            # Ken Burns: slow push-in over a background's life (1.0 -> 1.06)
MAX_BG_LUMA = 0.42        # plates brighter than this (mean luma 0..1) are dimmed so a dark subject keeps separation
TINT_STRENGTH = 0.22
FADE_OUT = 0.2
TRANSITIONS = {"fade": "fade", "slide_left": "slideleft", "slide_right": "slideright", "cut": "fade"}

# Inline copy of the report schema, used ONLY when schemas/composition_plan.schema.json is absent.
INLINE_SCHEMA = {
    "type": "object", "required": ["version", "duration", "subject", "background"],
    "properties": {
        "version": {"const": 1}, "duration": {"type": "number", "minimum": 1, "maximum": 90},
        "subject": {"type": "object"},
        "background": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["id", "kind", "start"]}},
        "text": {"type": "array", "items": {"type": "object", "required": ["id", "text", "region", "start", "end"]}},
        "captions": {"type": "object"}, "overlays": {"type": "array"}},
}


# ---------- helpers ----------
def _threads() -> int:
    try:
        return max(1, int(os.environ.get("ADF_FFMPEG_THREADS", "2")))
    except ValueError:
        return 2


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"{cmd[0]} failed ({p.returncode}): {p.stderr.strip()[-1500:]}")
    return p


def probe_duration(path: str | Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def _load_schema() -> dict:
    if SCHEMA_PATH.exists():
        return json.loads(SCHEMA_PATH.read_text())
    return INLINE_SCHEMA


def validate_plan(plan: dict) -> list[str]:
    """Schema errors (schemas/composition_plan.schema.json when present, else the inline report schema)."""
    from jsonschema import Draft202012Validator
    v = Draft202012Validator(_load_schema())
    return [f"{'/'.join(str(x) for x in e.absolute_path) or '<plan>'}: {e.message}" for e in sorted(v.iter_errors(plan), key=lambda e: list(map(str, e.absolute_path)))]


def _hex(c) -> str:
    return c if isinstance(c, str) else "#%02x%02x%02x" % tuple(c)


def _bgs(plan: dict) -> list[dict]:
    bgs = plan.get("background") or plan.get("backgrounds") or []
    return sorted(bgs, key=lambda b: b.get("start", 0))


def _beat_ranges(plan: dict) -> list[tuple[float, float]]:
    bgs, D = _bgs(plan), float(plan["duration"])
    return [(b["start"], bgs[i + 1]["start"] if i + 1 < len(bgs) else D) for i, b in enumerate(bgs)]


def _assets_get(assets: dict, group: str, name: str | None) -> str | None:
    if not name:
        return None
    m = (assets.get(group) or {})
    if name in m:
        return str(m[name])
    if name.startswith("asset:") and name[6:] in m:
        return str(m[name[6:]])
    if Path(name).exists():
        return name
    return None


def _backdrop_path(b: dict, assets: dict) -> str | None:
    return _assets_get(assets, "backdrops", b.get("src")) or _assets_get(assets, "backdrops", b.get("id"))


# ---------- backgrounds ----------
def _gradient(colors: list[str]) -> Image.Image:
    cs = [ct.hexc(c) for c in (colors or ["#3b0a2a", "#c2410c"])]
    if len(cs) == 1:
        c = cs[0]
        cs = [tuple(int(v * 0.55) for v in c), tuple(min(255, int(v * 1.25)) for v in c)]
    n = len(cs) - 1
    col = Image.new("RGB", (1, H))
    px = []
    for y in range(H):
        t = y / (H - 1) * n
        i = min(int(t), n - 1)
        f = t - i
        px.append(tuple(int(cs[i][j] + (cs[i + 1][j] - cs[i][j]) * f) for j in range(3)))
    col.putdata(px)
    return col.resize((W, H))


def _mean_luma(im: Image.Image) -> float:
    return sum(i * c for i, c in enumerate(im.convert("L").resize((64, 114)).histogram())) / (64 * 114 * 255.0)


def prep_bg(b: dict, assets: dict) -> Image.Image:
    """The background still at 1080x1920 BEFORE Ken Burns: cover-fit, static zoom, blur, dim (+ auto-dim of bright
    plates), brand tint, vignette. Deterministic."""
    kind = b["kind"]
    if kind == "gradient" or not _backdrop_path(b, assets):
        im = _gradient(b.get("colors") or [])
        return _vignette(im, 0.18)
    im = Image.open(_backdrop_path(b, assets)).convert("RGB")
    s = max(W / im.width, H / im.height) * float(b.get("zoom") or 1.0)
    im = im.resize((max(W, round(im.width * s)), max(H, round(im.height * s))), Image.LANCZOS)
    l, t = (im.width - W) // 2, (im.height - H) // 2
    im = im.crop((l, t, l + W, t + H))
    if b.get("blur"):
        im = im.filter(ImageFilter.GaussianBlur(float(b["blur"])))
    dim = float(b.get("dim") or 1.0)
    if dim < 1:
        im = Image.eval(im, lambda v: int(v * dim))
    luma = _mean_luma(im)
    if luma > MAX_BG_LUMA:   # plate too light/low contrast against a dark subject: pull it down
        k = MAX_BG_LUMA / luma
        im = Image.eval(im, lambda v: int(v * k))
    cols = b.get("colors") or []
    if cols:   # tint toward the brand colour so a generated plate sits in the palette
        im = Image.blend(im, Image.new("RGB", im.size, ct.hexc(cols[0])), TINT_STRENGTH)
    return _vignette(im, 0.35)


def _vignette(im: Image.Image, strength: float) -> Image.Image:
    if strength <= 0:
        return im
    m = Image.radial_gradient("L").resize(im.size, Image.BICUBIC)   # 0 centre -> 255 edge
    m = m.point(lambda v: int(max(0, v - 90) * strength * 255 / 165))
    return Image.composite(Image.new("RGB", im.size, (0, 0, 0)), im, m)


def _kb_frame(img2x: Image.Image, z: float) -> Image.Image:
    """Centre crop of a 2x prepared still at zoom z, resized to 1080x1920 (the preview twin of ffmpeg zoompan)."""
    w, h = img2x.size
    cw, ch = w / z, h / z
    box = ((w - cw) / 2, (h - ch) / 2, (w + cw) / 2, (h + ch) / 2)
    return img2x.resize((W, H), Image.LANCZOS, box=box)


# ---------- layers ----------
class Layer:
    def __init__(self, id_: str, kind: str, start: float, end: float, paths: list[Path], xywh: tuple[int, int, int, int], info: dict | None = None):
        self.id, self.kind, self.start, self.end, self.paths, self.xywh, self.info = id_, kind, start, end, paths, xywh, info or {}

    def frame_at(self, t: float, fps: int) -> Image.Image | None:
        if t < self.start or t > self.end:
            return None
        k = min(len(self.paths) - 1, int((t - self.start) * fps))
        im = Image.open(self.paths[k]).convert("RGBA")
        left = self.end - t
        if left < FADE_OUT:
            a = max(0.0, left / FADE_OUT)
            im.putalpha(im.getchannel("A").point(lambda v: int(v * a)))
        return im


def _pip_source(o: dict, assets: dict, workdir: Path) -> Path | None:
    src = o["src"]
    if src.startswith("frame:"):
        clip, _, sec = src[6:].partition("@")
        vid = _assets_get(assets, "clips", clip)
        if not vid:
            return None
        out = workdir / f"pipframe_{abs(hash(src)) % 10**8}.png"
        if not out.exists():
            _run(["ffmpeg", "-y", "-v", "error", "-ss", sec, "-i", vid, "-frames:v", "1", str(out)])
        return out
    p = _assets_get(assets, "pip_images", src)
    return Path(p) if p else None


def build_layers(plan: dict, assets: dict, workdir: Path) -> list[Layer]:
    """Render every text/caption/scrim/PiP layer to PNG sequences (same code feeds the full render and the stills)."""
    D = float(plan["duration"])
    layers: list[Layer] = []
    cap = plan.get("captions") or {}
    chunks = [c for c in (cap.get("chunks") or []) if c["end"] > c["start"]]
    cbox = ct.REGIONS["bottom_caption"][:4]
    # scrim behind the captions: one band for the whole caption span
    if chunks and float(cap.get("scrim", ct.SCRIM_ALPHA)) > 0:
        s, e = max(0.0, chunks[0]["start"] - 0.2), min(D, chunks[-1]["end"] + 0.2)
        img, c = ct.render_scrim(cbox, float(cap.get("scrim", ct.SCRIM_ALPHA)))
        paths, xywh = ct.save_sequence(ct.anim_frames(img, c, "fade", 6), workdir, "scrim")
        layers.append(Layer("scrim", "scrim", s, e, paths, xywh))
    cst = {**ct.ROLE_STYLE["caption"]}
    cst["fill"] = cap.get("fill") or cst["fill"]
    cst["hl"] = cap.get("highlight") or cst["hl"]
    if cap.get("stroke") is not None:
        cst["stroke"] = cap["stroke"]
    cst["stroke"] = [int(cst["stroke"][0]), cst["stroke"][1]]
    for i, ch in enumerate(chunks):
        img, c, info = ct.render_caption(ch["text"], cst, cbox, ch.get("hl"))
        paths, xywh = ct.save_sequence(ct.anim_frames(img, c, "pop", 8), workdir, f"cap{i:02d}")
        layers.append(Layer(f"cap{i}", "caption", ch["start"], min(D, ch["end"]), paths, xywh, info))
    for t in plan.get("text") or []:
        img, c, info = ct.render_block(t)
        anim = (ct.resolve_style(t).get("animation") or "pop")
        paths, xywh = ct.save_sequence(ct.anim_frames(img, c, anim), workdir, f"txt_{t['id']}")
        layers.append(Layer(t["id"], "text", t["start"], min(D, t["end"]), paths, xywh, info))
    for o in plan.get("overlays") or []:
        sp = _pip_source(o, assets, workdir)
        if sp is None:
            continue
        card = ct.render_pip(Image.open(sp), o.get("crop"), tuple(o["size"]), float(o.get("rot") or 0))
        x0, y0, x1, y1 = ct.REGIONS[o.get("region", "right_mid")][:4]
        al = ct.REGIONS[o.get("region", "right_mid")][4]
        cx = x0 + card.width / 2 - 48 if al == "left" else x1 - card.width / 2 + 48
        cy = (y0 + y1) / 2
        paths, xywh = ct.save_sequence(ct.anim_frames(card, (cx, cy), o.get("animation") or "slide_right"), workdir, f"pip_{o['id']}")
        info = {"box": (cx - card.width / 2 + 48, cy - card.height / 2 + 48, cx + card.width / 2 - 48, cy + card.height / 2 - 48)}
        layers.append(Layer(o["id"], "overlay", o["start"], min(D, o["end"]), paths, xywh, info))
    return layers


# ---------- subject chain (shared by the render and the preview stills) ----------
def _subject_chain(vi: str, mi: str, plan: dict, fps: int, dur: float) -> tuple[list[str], int, int]:
    """filter lines producing [shd] (drop shadow) and [sub] (cutout) plus the (x, y) to overlay the subject at."""
    sj = plan["subject"]
    feather = float((sj.get("mask") or {}).get("feather", 2.0))
    lo, hi = (sj.get("mask") or {}).get("edge", [0.35, 0.65])
    tr = sj.get("transform") or {}
    sc = float(tr.get("scale", 1.0))
    sw, sh = int(round(W * sc / 2)) * 2, int(round(H * sc / 2)) * 2
    ox = int(round((W - sw) / 2 + float(tr.get("x", 0))))
    oy = int(round(H - sh + float(tr.get("y", 0))))
    shadow = float(sj.get("shadow", 0.5))
    half = f"{W // 2}:{H // 2}"
    f = [
        # feather at half-res (cheap), upscale bicubic, smoothstep the edge -> clean antialiased alpha
        f"[{mi}]scale={half}:flags=bicubic,format=gray,gblur=sigma={feather / 2:.3f},scale={sw}:{sh}:flags=bicubic,"
        f"curves=all='0/0 {lo}/0 {hi}/1 1/1',format=gray,split[al][al2]",
        f"[{vi}]scale={sw}:{sh}:flags=bicubic,format=yuv420p[sv]",
        "[sv][al]alphamerge[sub]",
        # shadow: quarter-res blurred alpha, scaled by strength, black plate
        f"[al2]scale={sw // 4}:{sh // 4},gblur=sigma=5,lut=y=val*{shadow:.3f},scale={sw}:{sh}:flags=bilinear[sa]",
        f"color=c=black:s={sw}x{sh}:r={fps}:d={dur:.3f},format=yuv420p[blk]",
        "[blk][sa]alphamerge[shd]",
    ]
    return f, ox, oy


def _subject_png(master: str, matte: str, t: float, plan: dict, out: Path) -> None:
    """RGBA still of shadow+cutout at time t through the SAME filter chain as the render."""
    chain, ox, oy = _subject_chain("0:v", "1:v", plan, 1, 1)
    fc = ";".join(chain + [
        f"color=c=black@0.0:s={W}x{H}:r=1:d=1,format=rgba[base]",
        f"[base][shd]overlay={ox}:{oy + 14}:format=auto[a]",
        f"[a][sub]overlay={ox}:{oy}:format=auto,format=rgba[out]"])
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", master, "-ss", f"{t:.3f}", "-i", matte,
          "-filter_complex", fc, "-map", "[out]", "-frames:v", "1", str(out)])


# ---------- lint ----------
def lint_render_inputs(plan: dict, assets: dict) -> list[str]:
    """Human-readable problems that would make the render fail or look bad. Empty list = good to go."""
    probs: list[str] = []
    D = float(plan.get("duration", 0))
    for key in ("master", "matte"):
        p = assets.get(key)
        if not p or not Path(p).exists():
            probs.append(f"missing asset '{key}': {p or 'not provided'}")
    master = assets.get("master")
    if master and Path(master).exists():
        md = probe_duration(master)
        if md and md + 0.1 < D:
            probs.append(f"master is {md:.1f}s but the plan duration is {D:.1f}s; the render will be cut to {md:.1f}s")
    bgs = _bgs(plan)
    if not bgs:
        probs.append("plan has no background")
    elif abs(bgs[0].get("start", 0)) > 1e-6:
        probs.append(f"first background '{bgs[0]['id']}' starts at {bgs[0]['start']}s, not 0")
    flags = assets.get("flags") or {}
    preps: list[Image.Image | None] = []
    for b in bgs:
        if b["kind"] in ("image", "blur_still", "generated"):
            p = _backdrop_path(b, assets)
            if not p:
                probs.append(f"missing asset: background '{b['id']}' ({b['kind']}) needs a backdrop image '{b.get('src') or b['id']}'")
            elif (flags.get(b.get("src") or "", {}) or flags.get(b["id"], {}) or {}).get("person") or b.get("has_person"):
                probs.append(f"background '{b['id']}' still contains a person (would ghost behind the speaker): pick a person-free image")
        try:
            preps.append(prep_bg(b, assets))
        except Exception as e:   # unreadable image
            probs.append(f"background '{b['id']}': cannot prepare image ({e})")
            preps.append(None)
    ranges = _beat_ranges(plan) if bgs else []
    for b, (s, e) in zip(bgs, ranges):
        d = (b.get("transition_in") or {}).get("dur", 0)
        if e - s < d + 0.3:
            probs.append(f"background '{b['id']}' lasts {e - s:.1f}s, too short for its {d}s dissolve")

    def bg_at(t: float) -> Image.Image | None:
        for im, (s, e) in zip(preps, ranges):
            if s <= t < e or (e == ranges[-1][1] and t >= s):
                return im
        return preps[0] if preps else None

    def region_color(box, t: float) -> tuple[int, int, int] | None:
        im = bg_at(t)
        if im is None:
            return None
        x0, y0, x1, y1 = (int(v) for v in box)
        return im.crop((x0, y0, x1, y1)).resize((1, 1), Image.BOX).getpixel((0, 0))

    for t in plan.get("text") or []:
        tid = t.get("id", "?")
        if t.get("region", "top_center") not in ct.REGIONS and not t.get("box"):
            probs.append(f"text '{tid}': unknown region '{t.get('region')}'")
            continue
        if t["end"] <= t["start"]:
            probs.append(f"text '{tid}': end {t['end']}s is not after start {t['start']}s")
        if t["end"] > D + 1e-6:
            probs.append(f"text '{tid}': ends at {t['end']}s, after the {D}s video")
        _, _, info = ct.render_block(t)
        if not info["fits"] or info["size"] < 0.8 * ct.resolve_style(t)["size"]:
            probs.append(f"text '{tid}': does not fit its region at the requested size (shrunk {ct.resolve_style(t)['size']} -> {info['size']}px, {len(info['lines'])} lines)")
        over = ct.box_overflow(info["box"])
        if over:
            probs.append(f"text '{tid}' is outside the safe zone: " + "; ".join(over))
        st = ct.resolve_style(t)
        mid = (t["start"] + t["end"]) / 2
        local = region_color(info["box"], mid)
        bgc = st.get("bg")
        if bgc:
            eff = bgc["fill"] if int(bgc.get("alpha", 255)) >= 250 else (ct.blend(bgc["fill"], local, bgc.get("alpha", 255) / 255) if local else bgc["fill"])
        else:
            eff = local
        if eff is not None:
            ratio = ct.contrast_ratio(st["fill"], eff)
            if not bgc and st["stroke"][0] >= 5:   # a thick outline carries legibility on its own
                ratio = max(ratio, ct.contrast_ratio(st["fill"], st["stroke"][1]))
            need = 3.0 if info["size"] >= 60 else 4.5
            if ratio < need:
                probs.append(f"text '{tid}': low contrast {ratio:.1f}:1 (needs {need}:1) for fill {st['fill']} over {_hex(eff)}")
    cap = plan.get("captions") or {}
    scrim = float(cap.get("scrim", ct.SCRIM_ALPHA))
    for i, c in enumerate(cap.get("chunks") or []):
        if c["end"] <= c["start"]:
            probs.append(f"caption {i}: end is not after start")
        if c["end"] > D + 0.05:
            probs.append(f"caption {i}: ends at {c['end']}s, after the {D}s video")
        if i and c["start"] < cap["chunks"][i - 1]["end"] - 1e-6:
            probs.append(f"caption {i} overlaps caption {i - 1}")
    if cap.get("chunks"):
        if scrim < 0.35:
            probs.append(f"caption scrim {scrim} is under 0.35: captions will be hard to read over busy saree/hands")
        cbox = ct.REGIONS["bottom_caption"][:4]
        cst = ct.ROLE_STYLE["caption"]
        local = region_color(cbox, cap["chunks"][0]["start"])
        if local:
            eff = ct.blend((12, 4, 10), local, scrim)
            ratio = ct.contrast_ratio(cap.get("fill") or cst["fill"], eff)
            if ratio < 3.0:
                probs.append(f"captions: low contrast {ratio:.1f}:1 even on the scrim: raise scrim or darken the background")
        for i, c in enumerate(cap["chunks"]):
            _, _, info = ct.render_caption(c["text"], {**cst, "stroke": cap.get("stroke") or cst["stroke"]}, cbox)
            if not info["fits"]:
                probs.append(f"caption {i} '{c['text'][:24]}' does not fit two lines at the caption size")
    for o in plan.get("overlays") or []:
        oid = o.get("id", "?")
        src = o.get("src", "")
        if src.startswith("frame:"):
            if not _assets_get(assets, "clips", src[6:].partition("@")[0]):
                probs.append(f"missing asset for overlay '{oid}': {src}")
        elif not _assets_get(assets, "pip_images", src):
            probs.append(f"missing asset for overlay '{oid}': {src}")
        if o["end"] > D + 1e-6 or o["end"] <= o["start"]:
            probs.append(f"overlay '{oid}': bad times {o['start']}..{o['end']}")
        r = ct.REGIONS.get(o.get("region", "right_mid"))
        if r is None:
            probs.append(f"overlay '{oid}': unknown region")
        else:
            w, h = o["size"]
            if w + 28 > r[2] - r[0] or h + 28 > r[3] - r[1]:
                probs.append(f"overlay '{oid}': card {w}x{h} larger than region {o.get('region')}")
    # visible-at-once rule from the schema: at most 2 text blocks (captions included)
    spans = [(t["start"], t["end"]) for t in (plan.get("text") or [])] + [(c["start"], c["end"]) for c in (cap.get("chunks") or [])]
    for s, e in spans:
        probe = (s + e) / 2
        n = sum(1 for a, b in spans if a <= probe < b)
        if n > 2:
            probs.append(f"{n} text blocks are visible together around {probe:.1f}s (max 2)")
            break
    return probs


# ---------- render ----------
def _prep_assets(plan: dict, assets: dict, workdir: Path) -> tuple[list[dict], list[Path]]:
    """Per-background: dict(prepared still path, kb flag). Stills are 2x for Ken Burns, 1x for gradients."""
    out, paths = [], []
    for i, b in enumerate(_bgs(plan)):
        im = prep_bg(b, assets)
        kb = b["kind"] != "gradient" and bool(_backdrop_path(b, assets))
        p = workdir / f"bg{i}.png"
        (im.resize((W * 2, H * 2), Image.LANCZOS) if kb else im).save(p)
        out.append({"path": p, "kb": kb, "img": im})
        paths.append(p)
    return out, paths


def render_composition(plan: dict, assets: dict, out_path: str | Path, *, size: tuple[int, int] = (1080, 1920),
                       fps: int = 30, preview: bool = False, workdir: str | Path | None = None) -> dict:
    """Execute a composition plan. preview=True -> one PNG still per beat (540x960) + a contact sheet and returns
    {"out": contact sheet, "frames_dir", "frames", "seconds", "layers"}; else renders the mp4 and returns
    {"out", "seconds", "layers", "frames_dir"}. out_path in preview mode: a directory (or a file path whose stem
    becomes the directory)."""
    t0 = time.time()
    errs = [e for e in lint_render_inputs(plan, assets) if e.startswith("missing asset") or "no background" in e]
    if errs:
        raise ValueError("cannot render: " + "; ".join(errs))
    own_tmp = None
    if workdir is None:
        own_tmp = tempfile.TemporaryDirectory(prefix="adf_compose_")
        wd = Path(own_tmp.name)
    else:
        wd = Path(workdir)
        wd.mkdir(parents=True, exist_ok=True)
    try:
        plan = copy.deepcopy(plan)
        plan["size"], plan["fps"] = [W, H], fps
        master, matte = str(assets["master"]), str(assets["matte"])
        md = probe_duration(master)
        D = float(plan["duration"])
        if md and md + 0.1 < D:
            D = round(md, 3)
            plan["duration"] = D
        layers = build_layers(plan, assets, wd)
        if preview:
            return _preview(plan, assets, master, matte, layers, Path(out_path), wd, fps, t0)
        return _full(plan, assets, master, matte, layers, Path(out_path), wd, size, fps, t0, D)
    finally:
        if own_tmp:
            own_tmp.cleanup()


def _full(plan, assets, master, matte, layers, out: Path, wd: Path, size, fps, t0, D) -> dict:
    th = _threads()
    out.parent.mkdir(parents=True, exist_ok=True)
    bgs = _bgs(plan)
    preps, _ = _prep_assets(plan, assets, wd)
    ins: list[str] = []
    n = 0

    def add(args: list[str]) -> int:
        nonlocal n
        ins.extend(args)
        n += 1
        return n - 1

    vi = add(["-t", f"{D:.3f}", "-i", master])
    mi = add(["-t", f"{D:.3f}", "-i", matte])
    # background stills; each stream must outlive its own beat + the next dissolve
    offs, durs = [], []
    for i, b in enumerate(bgs):
        d = 0.0 if i == 0 else max(0.04, float((b.get("transition_in") or {}).get("dur", 0.6)))
        offs.append(b["start"] - d / 2)
        durs.append(d)
    bi, fc = [], []
    for i, b in enumerate(bgs):
        end_t = (offs[i + 1] + durs[i + 1]) if i + 1 < len(bgs) else D
        length = max(1.0, end_t - offs[i] + 0.5)
        idx = add(["-loop", "1", "-framerate", str(fps), "-t", f"{length:.3f}", "-i", str(preps[i]["path"])])
        if preps[i]["kb"]:
            nfr = max(1, int(round(length * fps)))
            fc.append(f"[{idx}:v]zoompan=z='1+{KB_ZOOM}*on/{nfr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps},"
                      f"format=yuv420p,setsar=1[b{i}]")
        else:
            fc.append(f"[{idx}:v]scale={W}:{H},format=yuv420p,fps={fps},setsar=1[b{i}]")
        bi.append(f"[b{i}]")
    cur = bi[0]
    for i in range(1, len(bgs)):
        tr = (bgs[i].get("transition_in") or {}).get("type", "fade")
        fc.append(f"{cur}{bi[i]}xfade=transition={TRANSITIONS.get(tr, 'fade')}:duration={durs[i]:.3f}:offset={offs[i]:.3f}[bgx{i}]")
        cur = f"[bgx{i}]"
    chain, ox, oy = _subject_chain(f"{vi}:v", f"{mi}:v", plan, fps, D)
    fc.extend(chain)
    fc.append(f"{cur}format=yuv420p[bgf];[bgf][shd]overlay={ox}:{oy + 14}:format=auto[l0];[l0][sub]overlay={ox}:{oy}:format=auto[l1]")
    cur = "[l1]"
    # animated layers
    for li, L in enumerate(layers):
        ii = add(["-framerate", str(fps), "-i", str(wd / f"{L.paths[0].stem[:-3]}_%02d.png")])
        x, y, cw, ch = L.xywh
        dur = L.end - L.start
        hold = max(0.0, dur - len(L.paths) / fps) + 0.1
        fc.append(f"[{ii}:v]format=rgba,tpad=stop_mode=clone:stop_duration={hold:.3f},"
                  f"fade=t=out:st={max(0.0, dur - FADE_OUT):.3f}:d={FADE_OUT}:alpha=1,setpts=PTS-STARTPTS+{L.start:.3f}/TB[a{li}]")
        fc.append(f"{cur}[a{li}]overlay={x}:{y}:enable='between(t,{L.start:.3f},{L.end:.3f})':format=auto:eof_action=pass[o{li}]")
        cur = f"[o{li}]"
    scale = f"scale={size[0]}:{size[1]}:flags=lanczos," if tuple(size) != (W, H) else ""
    fc.append(f"{cur}{scale}format=yuv420p[vout]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-threads", str(th), "-filter_complex_threads", str(th), *ins,
           "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", f"{vi}:a?", "-t", f"{D:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(fps),
           "-c:a", "copy", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
           "-movflags", "+faststart", str(out)]
    t1 = time.time()
    _run(cmd)
    return {"out": str(out), "seconds": round(time.time() - t0, 2), "render_seconds": round(time.time() - t1, 2),
            "layers": [{"id": L.id, "kind": L.kind, "start": L.start, "end": L.end} for L in layers],
            "frames_dir": str(wd), "duration": D, "size": list(size), "fps": fps}


def _preview(plan, assets, master, matte, layers, out_path: Path, wd: Path, fps: int, t0: float) -> dict:
    fdir = out_path if not out_path.suffix else out_path.with_suffix("")
    fdir.mkdir(parents=True, exist_ok=True)
    bgs = _bgs(plan)
    ranges = _beat_ranges(plan)
    frames = []
    for i, (b, (s, e)) in enumerate(zip(bgs, ranges)):
        mid = round((s + e) / 2, 3)
        # background at mid: Ken Burns zoom of the beat's own still
        life = max(0.5, e - s)
        z = 1 + KB_ZOOM * ((mid - s) / life) if (b["kind"] != "gradient" and _backdrop_path(b, assets)) else 1.0
        base = prep_bg(b, assets)
        if z > 1:
            base = _kb_frame(base.resize((W * 2, H * 2), Image.LANCZOS), z)
        im = base.convert("RGBA")
        subj = wd / f"subj_{i}.png"
        _subject_png(master, matte, mid, plan, subj)
        im.alpha_composite(Image.open(subj).convert("RGBA"))
        for L in layers:
            fr = L.frame_at(mid, fps)
            if fr is not None:
                im.alpha_composite(fr, (L.xywh[0], L.xywh[1]))
        p = fdir / f"beat{i + 1:02d}_{mid:05.2f}s.png"
        im.convert("RGB").resize((540, 960), Image.LANCZOS).save(p)
        frames.append(p)
    sheet = fdir / "contact.png"
    cs = Image.new("RGB", (540 * len(frames) + 12 * (len(frames) - 1), 960), (24, 24, 24))
    for k, p in enumerate(frames):
        cs.paste(Image.open(p), (k * 552, 0))
    cs.save(sheet)
    return {"out": str(sheet), "seconds": round(time.time() - t0, 2), "frames_dir": str(fdir),
            "frames": [str(p) for p in frames], "layers": [{"id": L.id, "kind": L.kind, "start": L.start, "end": L.end} for L in layers]}

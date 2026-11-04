"""Deterministic composition renderer: speaker cut out (SAM matte) over changing backgrounds, stylised text in named
regions, pop-in captions on a soft scrim, a PiP card. The AI decides (the composition plan JSON, see
schemas/composition_plan.schema.json); this module only executes it. No network, no paid call here.

    render_composition(plan, assets, out_path, *, size=(1080,1920), fps=30, preview=False, workdir=None)
    lint_render_inputs(plan, assets, size=None) -> list[str]   (human-readable problems)
    validate_plan(plan)                         -> list[str]   (schema errors)
    best_preview_time(plan, beat)               -> float       (a time where a headline/callout is on screen)

assets = {"master": mp4 (speech-cut 9:16 with audio), "matte": grayscale mp4 aligned to master,
          "backdrops": {name: image path}, "pip_images": {name: image path},
          "clips": {clip name: video path}   # only needed for pip src "frame:<clip>@<sec>"
          "flags": {backdrop name: {"person": true}}   # optional: lint rejects person-bearing background stills}

Ratios: the layout (safe zones, text regions, caption box, where the cut-out sits) is chosen from `size`:
9:16 (1080x1920), 1:1 (1080x1080) and 16:9 (1920x1080) each get their OWN layout from the same plan. `size` of any
other scale (e.g. 270x480) only rescales the finished frame. Head-aware: plan["meta"]["head"] = {"top_frac", "bbox_frac"}
keeps headlines above the head and callouts off the face; without it the legacy fixed regions are used.

Technique (proved by the compose spike): every animated layer is a short PNG sequence on a shared canvas, held with
tpad and timed with overlay enable. Background stills get a slow Ken Burns zoompan; beats dissolve with xfade.
Subject = master + matte through scale, gblur, curves (feather), alphamerge, with a quarter-res drop shadow, a light rim
on dark backgrounds, a per-beat scale (head-size normalisation) and quick push-in / flash on beat boundaries.
Audio is stream-copied from the master.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageFilter

from . import compose_text as ct
from .compose_text import H, W

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas/composition_plan.schema.json"
KB_ZOOM = 0.06            # Ken Burns: slow push-in over a background's life (1.0 -> 1.06)
MAX_BG_LUMA = 0.42        # plates brighter than this (mean luma 0..1) are dimmed so a dark subject keeps separation
TINT_STRENGTH = 0.22
FADE_OUT = 0.2
RIM_LUMA = 0.2            # backgrounds darker than this get a light rim / 2 px outer glow around the cut-out
PUSH_AMP = 0.10           # a push-in transition starts the incoming beat 10 percent big and settles in 0.25 s
PUSH_DUR = 0.25
FLASH_PEAK = 0.55
TRANSITIONS = {"fade": "fade", "slide_left": "slideleft", "slide_right": "slideright", "cut": "fade",
               "dissolve": "fade", "push": "fade", "flash": "fade"}
FULL_TIMEOUT = 600        # seconds: one full ffmpeg render
HELPER_TIMEOUT = 120      # seconds: any helper ffmpeg/ffprobe call
HEAD_W_FRAC = 0.46        # head box width as a fraction of the master frame width
HEAD_H_FRAC = 0.22        # head box height (hair top to chin) as a fraction of the master frame height
DEFAULT_HEAD_TOP = 0.29
HOOK_TARGET = 84          # px: the headline size the layout makes room for above the head (never below 72 once shifted)
HOOK_MAX_SHIFT = {"9x16": 80.0, "1x1": 40.0}   # px the cut-out may be pushed down to give a two-line hook room
CTA_MAX_S = 3.0           # the on-screen CTA pill lives in the last 3 s only
RESCALE_TOL = 0.15        # master vs plan length difference (s) below which the plan timeline is left alone
HEAD_TOP_SLACK = 0.03    # hair flyaways / pose changes put the real top this far above the matte's median head top


class ComposeFail(RuntimeError):
    """The composed look could not be produced (ffmpeg error or timeout): the caller falls back to the simple cut."""


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


def _run(cmd: list[str], timeout: float = HELPER_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ComposeFail(f"{cmd[0]} timed out after {int(timeout)}s") from None
    except OSError as e:
        raise ComposeFail(f"{cmd[0]} could not start: {e}") from None
    if p.returncode:
        raise ComposeFail(f"{cmd[0]} failed ({p.returncode}): {p.stderr.strip()[-1500:]}")
    return p


def probe_duration(path: str | Path) -> float:
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=HELPER_TIMEOUT)
        return float(p.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
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
    """Only names the caller supplied in assets[group]; arbitrary paths from a plan are never opened."""
    if not name:
        return None
    m = (assets.get(group) or {})
    if name in m:
        return str(m[name])
    if name.startswith("asset:") and name[6:] in m:
        return str(m[name[6:]])
    return None


def _backdrop_path(b: dict, assets: dict) -> str | None:
    return _assets_get(assets, "backdrops", b.get("src")) or _assets_get(assets, "backdrops", b.get("id"))


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


# ---------- subject geometry: per-beat scale, push-in, flash ----------
def _subject_beats(plan: dict, D: float) -> list[dict]:
    """Subject beats on the output timeline from subject.segments (cumulative in..out, rescaled to the duration):
    [{start, end, scale, trans, dur}]. scale = per-beat head-size normalisation, trans = transition_in type."""
    segs = (plan.get("subject") or {}).get("segments") or []
    durs = [max(0.0, float(sg.get("out", 0)) - float(sg.get("in", 0))) for sg in segs]
    tot = sum(durs)
    if not segs or tot <= 0:
        return [{"start": 0.0, "end": D, "scale": 1.0, "trans": None, "dur": 0.0}]
    k = 1.0 if abs(tot - D) < 0.05 else D / tot
    out, t = [], 0.0
    for i, (sg, d) in enumerate(zip(segs, durs)):
        tr = sg.get("transition_in") or {}
        out.append({"start": round(t, 3), "end": round(t + d * k, 3), "scale": float(sg.get("subject_scale") or 1.0),
                    "trans": tr.get("type") if i else None, "dur": float(tr.get("dur") or 0.0)})
        t += d * k
    out[-1]["end"] = D
    return out


def _events(plan: dict, D: float) -> list[dict]:
    """Beat-boundary effects: [{t, kind: push|flash, dur}] from segment transition_in and background transition_in."""
    ev: list[dict] = []
    for b in _subject_beats(plan, D):
        if b["trans"] in ("push", "flash") and 0 < b["start"] < D:
            ev.append({"t": b["start"], "kind": b["trans"], "dur": b["dur"]})
    for b in _bgs(plan):
        tr = (b.get("transition_in") or {}).get("type")
        if tr in ("push", "flash") and 0 < b.get("start", 0) < D and not any(abs(e["t"] - b["start"]) < 0.08 for e in ev):
            ev.append({"t": float(b["start"]), "kind": tr, "dur": float((b.get("transition_in") or {}).get("dur") or 0.0)})
    return sorted(ev, key=lambda e: e["t"])


def _push_params(e: dict) -> tuple[float, float]:
    return PUSH_AMP, max(0.1, min(0.6, e["dur"] or PUSH_DUR))


def _flash_half(e: dict) -> float:
    return max(0.05, min(0.3, (e["dur"] or 0.12) / 2))


def scale_at(plan: dict, lay: ct.Layout, t: float) -> float:
    """Total subject scale at output time t (transform.scale * layout * per-beat normalisation * push-in)."""
    D = float(plan["duration"])
    s = float(((plan.get("subject") or {}).get("transform") or {}).get("scale", 1.0)) * lay.sub_scale
    base = 1.0
    for b in _subject_beats(plan, D):
        if b["start"] <= t + 1e-9:
            base = b["scale"]
    s *= base
    for e in _events(plan, D):
        if e["kind"] == "push" and t >= e["t"]:
            a, d = _push_params(e)
            s *= 1 + a * (1 - min(1.0, max(0.0, (t - e["t"]) / d))) ** 3
    return s


def _scale_expr(plan: dict, lay: ct.Layout) -> str | None:
    """The same scale as an ffmpeg expression of t; None when it is constant (a plain static scale is then used)."""
    D = float(plan["duration"])
    k = float(((plan.get("subject") or {}).get("transform") or {}).get("scale", 1.0)) * lay.sub_scale
    beats = _subject_beats(plan, D)
    evs = [e for e in _events(plan, D) if e["kind"] == "push"]
    if len({b["scale"] for b in beats}) == 1 and not evs:
        return None
    expr = f"{beats[-1]['scale']:.4f}"
    for b in reversed(beats[:-1]):
        expr = f"if(lt(t,{b['end']:.3f}),{b['scale']:.4f},{expr})"
    expr = f"{k:.4f}*{expr}"
    for e in evs:
        a, d = _push_params(e)
        expr += f"*if(gte(t,{e['t']:.3f}),1+{a}*pow(1-clip((t-{e['t']:.3f})/{d:.3f},0,1),3),1)"
    return expr


def _geom(plan: dict, lay: ct.Layout, S: float) -> tuple[float, float, float, float]:
    """Where the (1080x1920 master-space) subject lands on the canvas at total scale S: x, y, w, h."""
    tr = (plan.get("subject") or {}).get("transform") or {}
    sw, sh = W * S, H * S
    x = lay.sub_cx + float(tr.get("x", 0)) - sw / 2
    dy = _hook_shift(plan, lay)
    if lay.anchor == "bottom":
        y = lay.h - sh + float(tr.get("y", 0)) + dy
    else:
        y = lay.head_y - _head_meta(plan)[0] * H * S + float(tr.get("y", 0)) + dy
    return x, y, sw, sh


def _head_meta(plan: dict) -> tuple[float, list[float], bool]:
    hd = (plan.get("meta") or {}).get("head") or {}
    top = hd.get("top_frac")
    bb = hd.get("bbox_frac")
    ok = isinstance(top, (int, float))
    top = float(top) if ok else DEFAULT_HEAD_TOP
    bb = [float(v) for v in bb] if isinstance(bb, (list, tuple)) and len(bb) == 4 else [0.05, top, 0.95, 1.0]
    return top, bb, ok


def _hook_item(plan: dict) -> dict | None:
    for t in plan.get("text") or []:
        if t.get("region", "top_center") == "top_center" and (t.get("role") == "hook" or t.get("style") == "pill_headline"):
            return t
    return None


def _hook_shift(plan: dict, lay: ct.Layout) -> float:
    """Pixels the cut-out is pushed DOWN so the headline can be set big above the head. 0 when the plan has no head box,
    no hook, the layout puts the hook beside the head (16:9), or the hook already fits at HOOK_TARGET. Static for the
    whole video (no jumping), capped (80 px in 9:16, 40 px in 1:1); the head stays inside the frame."""
    cap = HOOK_MAX_SHIFT.get(lay.name)
    top, _, ok = _head_meta(plan)
    hk = _hook_item(plan)
    if not cap or not ok or hk is None:
        return 0.0
    tr = (plan.get("subject") or {}).get("transform") or {}
    D = float(plan["duration"])
    S = float(tr.get("scale", 1.0)) * lay.sub_scale * max(b["scale"] for b in _subject_beats(plan, D))
    size = min(int(ct.resolve_style(hk, lay)["size"]), HOOK_TARGET)
    need = ct.hook_room(hk, lay, size) + ct.HEAD_GAP
    y0 = ct.text_box(hk, lay)[1]
    if lay.anchor == "bottom":
        head_top = lay.h - H * S + (top - HEAD_TOP_SLACK) * H * S
    else:
        head_top = lay.head_y - HEAD_TOP_SLACK * H * S
    head_top += float(tr.get("y", 0))
    short = need - (head_top - y0)
    if short <= 0:
        return 0.0
    dy = min(cap, float(-(-short // 2) * 2))
    # never push the head's lower half out of the frame
    return float(max(0.0, min(dy, lay.h - (head_top + HEAD_H_FRAC * H * S) - 40)))


def head_box(plan: dict, lay: ct.Layout, t: float) -> tuple[float, float, float, float] | None:
    """Head box in canvas px at time t, or None when the plan carries no meta.head (legacy fixed regions then)."""
    top, bb, ok = _head_meta(plan)
    if not ok:
        return None
    S = scale_at(plan, lay, t)
    x, y, _, _ = _geom(plan, lay, S)
    hcx = (bb[0] + bb[2]) / 2 * W
    hw = HEAD_W_FRAC * W
    return (x + (hcx - hw / 2) * S, y + (top - HEAD_TOP_SLACK) * H * S, x + (hcx + hw / 2) * S, y + (top + HEAD_H_FRAC) * H * S)


def best_preview_time(plan: dict, beat, end: float | None = None) -> float:
    """A time inside the beat where a headline / callout / CTA is on screen and settled (past its entrance), so the
    preview still shows the layout. beat = background index, a (start, end) pair, or a start second with `end`."""
    D = float(plan["duration"])
    if isinstance(beat, (tuple, list)):
        s, e = float(beat[0]), float(beat[1])
    elif end is not None:
        s, e = float(beat), float(end)
    else:
        rng = _beat_ranges(plan)
        s, e = rng[min(max(int(beat), 0), len(rng) - 1)] if rng else (0.0, D)
    pri = {"hook": 0, "title": 1, "callout": 2, "cta": 3}
    best = None
    for t in plan.get("text") or []:
        role = t.get("role") or ("hook" if t.get("style") == "pill_headline" else "callout" if t.get("style") == "callout_tag" else "cta")
        if role not in pri:
            continue
        a, b = max(float(t["start"]), s), min(float(t["end"]), e)
        if b - a < 0.25:
            continue
        lo, hi = min(a + 0.5, b - 0.1), max(b - 0.3, a + 0.1)    # after the pop-in, before the fade-out
        cand = (pri[role], -(b - a), round((lo + max(lo, hi)) / 2, 3))
        best = cand if best is None or cand < best else best
    t = best[2] if best else (s + e) / 2
    return round(min(max(t, s), max(s, e - 0.05)), 3)


# ---------- backgrounds ----------
def _gradient(colors: list[str], size: tuple[int, int] = (W, H)) -> Image.Image:
    cs = [ct.hexc(c) for c in (colors or ["#3b0a2a", "#c2410c"])]
    if len(cs) == 1:
        c = cs[0]
        cs = [tuple(int(v * 0.55) for v in c), tuple(min(255, int(v * 1.25)) for v in c)]
    n = len(cs) - 1
    cw, ch = size
    col = Image.new("RGB", (1, ch))
    px = []
    for y in range(ch):
        t = y / (ch - 1) * n
        i = min(int(t), n - 1)
        f = t - i
        px.append(tuple(int(cs[i][j] + (cs[i + 1][j] - cs[i][j]) * f) for j in range(3)))
    col.putdata(px)
    return col.resize((cw, ch))


def _mean_luma(im: Image.Image) -> float:
    return sum(i * c for i, c in enumerate(im.convert("L").resize((64, 114)).histogram())) / (64 * 114 * 255.0)


def _glow(im: Image.Image, cx: float, cy: float, rad: float, color, strength: float) -> Image.Image:
    """Soft radial light behind the subject: lifts the plate where it meets the dark saree so the cut-out separates."""
    w, h = im.size
    m = Image.radial_gradient("L").resize((int(rad * 2), int(rad * 2)), Image.BICUBIC)   # 0 centre -> 255 edge
    m = m.point(lambda v: int(max(0.0, 1 - v / 255.0) ** 1.6 * strength * 255))
    full = Image.new("L", (w, h), 0)
    full.paste(m, (int(cx - rad), int(cy - rad)))
    return Image.composite(Image.new("RGB", im.size, color), im, full)


def prep_bg(b: dict, assets: dict, lay: ct.Layout | None = None) -> Image.Image:
    """The background still at the layout's canvas BEFORE Ken Burns: cover-fit, static zoom, blur, dim (+ auto-dim of
    bright plates), brand tint, a soft glow behind the subject, vignette. Deterministic."""
    lay = lay or ct.get_layout("9x16")
    cw, ch = lay.w, lay.h
    kind = b["kind"]
    cols = b.get("colors") or []
    if kind == "gradient" or not _backdrop_path(b, assets):
        im = _gradient(cols, (cw, ch))
        lit = ct.blend((255, 214, 170), ct.hexc(cols[-1]) if cols else (194, 65, 12), 0.35)
        im = _glow(im, lay.sub_cx, ch * 0.46, ch * 0.62, lit, 0.30)
        return _vignette(im, 0.18)
    im = Image.open(_backdrop_path(b, assets)).convert("RGB")
    s = max(cw / im.width, ch / im.height) * float(b.get("zoom") or 1.0)
    im = im.resize((max(cw, round(im.width * s)), max(ch, round(im.height * s))), Image.LANCZOS)
    l, t = (im.width - cw) // 2, (im.height - ch) // 2
    im = im.crop((l, t, l + cw, t + ch))
    if b.get("blur"):
        im = im.filter(ImageFilter.GaussianBlur(float(b["blur"])))
    dim = float(b.get("dim") or 1.0)
    if dim < 1:
        im = Image.eval(im, lambda v: int(v * dim))
    luma = _mean_luma(im)
    if luma > MAX_BG_LUMA:   # plate too light/low contrast against a dark subject: pull it down
        k = MAX_BG_LUMA / luma
        im = Image.eval(im, lambda v: int(v * k))
    if cols:   # tint toward the brand colour so a generated plate sits in the palette
        im = Image.blend(im, Image.new("RGB", im.size, ct.hexc(cols[0])), TINT_STRENGTH)
    return _vignette(im, 0.35)


def _vignette(im: Image.Image, strength: float) -> Image.Image:
    if strength <= 0:
        return im
    m = Image.radial_gradient("L").resize(im.size, Image.BICUBIC)   # 0 centre -> 255 edge
    m = m.point(lambda v: int(max(0, v - 90) * strength * 255 / 165))
    return Image.composite(Image.new("RGB", im.size, (0, 0, 0)), im, m)


def _kb_frame(img2x: Image.Image, z: float, size: tuple[int, int] = (W, H)) -> Image.Image:
    """Centre crop of a 2x prepared still at zoom z, resized to the canvas (the preview twin of ffmpeg zoompan)."""
    w, h = img2x.size
    cw, ch = w / z, h / z
    box = ((w - cw) / 2, (h - ch) / 2, (w + cw) / 2, (h + ch) / 2)
    return img2x.resize(size, Image.LANCZOS, box=box)


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
        if self.kind != "caption" and left < FADE_OUT:   # captions cut out hard: never a half-grey caption
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
        out = workdir / f"pipframe_{_sha1(src)}.png"
        if not out.exists():
            _run(["ffmpeg", "-y", "-v", "error", "-ss", sec, "-i", vid, "-frames:v", "1", str(out)])
        return out
    p = _assets_get(assets, "pip_images", src)
    return Path(p) if p else None


def caption_style(cap: dict) -> dict:
    """The active caption look: full-opacity white, CAPTION_STROKE (4 px) outline in the plan's outline colour, the
    highlight word in the plan's highlight colour."""
    cst = {**ct.ROLE_STYLE["caption"]}
    cst["fill"] = "#ffffff"
    cst["hl"] = cap.get("highlight") or cst["hl"]
    sk = cap.get("stroke")
    col = sk[1] if isinstance(sk, (list, tuple)) and len(sk) > 1 else cst["stroke"][1]
    cst["stroke"] = [ct.CAPTION_STROKE, col]
    return cst


def build_layers(plan: dict, assets: dict, workdir: Path, lay: ct.Layout | None = None) -> list[Layer]:
    """Render every text/caption/scrim/PiP layer to PNG sequences (same code feeds the full render and the stills)."""
    lay = lay or ct.get_layout("9x16")
    cv = (lay.w, lay.h)
    D = float(plan["duration"])
    layers: list[Layer] = []
    cap = plan.get("captions") or {}
    chunks = [c for c in (cap.get("chunks") or []) if c["end"] > c["start"]]
    cbox = tuple(lay.cap_box)
    if chunks and float(cap.get("scrim", ct.SCRIM_ALPHA)) > 0:
        sc = ct.render_scrim(cbox, float(cap.get("scrim", ct.SCRIM_ALPHA)), lay)
        if sc is not None:   # one soft gradient for the whole caption span
            s, e = max(0.0, chunks[0]["start"] - 0.2), min(D, chunks[-1]["end"] + 0.2)
            paths, xywh = ct.save_sequence(ct.anim_frames(sc[0], sc[1], "fade", 6), workdir, "scrim", cv)
            layers.append(Layer("scrim", "scrim", s, e, paths, xywh))
    cst = caption_style(cap)
    for i, ch in enumerate(chunks):
        img, c, info = ct.render_caption(ch["text"], cst, cbox, ch.get("hl"), lay)
        paths, xywh = ct.save_sequence(ct.anim_frames(img, c, "pop", 8, fade_in=False), workdir, f"cap{i:02d}", cv)
        layers.append(Layer(f"cap{i}", "caption", ch["start"], min(D, ch["end"]), paths, xywh, info))
    for t in plan.get("text") or []:
        mid = (float(t["start"]) + min(D, float(t["end"]))) / 2
        img, c, info = ct.render_block(t, lay, head_box(plan, lay, mid))
        anim = (ct.resolve_style(t, lay).get("animation") or "pop")
        paths, xywh = ct.save_sequence(ct.anim_frames(img, c, anim), workdir, f"txt_{t['id']}", cv)
        layers.append(Layer(t["id"], "text", t["start"], min(D, t["end"]), paths, xywh, info))
    for o in plan.get("overlays") or []:
        if lay.name != "9x16":   # the PiP card has no room beside the cut-out in the other ratios: dropped there
            continue
        sp = _pip_source(o, assets, workdir)
        if sp is None:
            continue
        card = ct.render_pip(Image.open(sp), o.get("crop"), tuple(o["size"]), float(o.get("rot") or 0))
        x0, y0, x1, y1 = lay.regions[o.get("region", "right_mid")][:4]
        al = lay.regions[o.get("region", "right_mid")][4]
        cx = x0 + card.width / 2 - 48 if al == "left" else x1 - card.width / 2 + 48
        cy = (y0 + y1) / 2
        paths, xywh = ct.save_sequence(ct.anim_frames(card, (cx, cy), o.get("animation") or "slide_right"), workdir, f"pip_{o['id']}", cv)
        info = {"box": (cx - card.width / 2 + 48, cy - card.height / 2 + 48, cx + card.width / 2 - 48, cy + card.height / 2 - 48)}
        layers.append(Layer(o["id"], "overlay", o["start"], min(D, o["end"]), paths, xywh, info))
    return layers


# ---------- subject chain (shared by the render and the preview stills) ----------
def _rim_beats(plan: dict, preps: list[dict]) -> tuple[list[tuple[float, float]], int, str]:
    """Time ranges where the cut-out gets a light rim: backgrounds darker than RIM_LUMA (or 0.3 when the plan enables
    subject.rim). subject.rim.enabled == false switches it off. Returns (ranges, px, colour)."""
    rim = (plan.get("subject") or {}).get("rim") or {}
    if rim.get("enabled") is False:
        return [], 2, "#ffe9d2"
    thr = 0.3 if rim.get("enabled") else RIM_LUMA
    rng = _beat_ranges(plan)
    out = [(s, e) for (s, e), p in zip(rng, preps) if p["luma"] < thr]
    return out, max(2, int(rim.get("px") or 2)), rim.get("color") or "#ffe9d2"


def _subject_filters(cur: str, vi: str, mi: str, plan: dict, lay: ct.Layout, fps: int, dur: float, sexpr: str | None,
                     const_scale: float, rim: tuple[list, int, str], extra_overlay: str = "") -> tuple[list[str], str]:
    """Filter lines that overlay shadow, rim and cut-out onto `cur`; returns (filters, new label)."""
    sj = plan["subject"]
    feather = float((sj.get("mask") or {}).get("feather", 2.0))
    lo, hi = (sj.get("mask") or {}).get("edge", [0.35, 0.65])
    tr = sj.get("transform") or {}
    shadow = float(sj.get("shadow", 0.5))
    ty = float(tr.get("y", 0))
    htop = _head_meta(plan)[0]
    base = f"{W}:{H}"
    rim_ranges, rim_px, rim_col = rim
    n_al = 3 if rim_ranges else 2
    al_labels = "".join(f"[al{k}]" for k in range(n_al))
    f = [
        # feather at half-res (cheap), upscale bicubic, smoothstep the edge -> clean antialiased alpha
        f"[{mi}]setpts=PTS-STARTPTS,scale={W // 2}:{H // 2}:flags=bicubic,format=gray,gblur=sigma={feather / 2:.3f},scale={base}:flags=bicubic,"
        f"curves=all='0/0 {lo}/0 {hi}/1 1/1',format=gray,split={n_al}{al_labels}",
        f"[{vi}]setpts=PTS-STARTPTS,scale={base}:flags=bicubic,format=yuv420p[sv]",
        "[sv][al0]alphamerge[sub0]",
        f"[al1]scale={W // 4}:{H // 4},gblur=sigma=5,lut=y=val*{shadow:.3f},scale={base}:flags=bilinear[sa]",
        f"color=c=black:s={W}x{H}:r={fps}:d={dur:.3f},format=yuv420p[blk]",
        "[blk][sa]alphamerge[shd0]",
    ]
    if rim_ranges:
        col = rim_col.lstrip("#")
        f.append(f"[al2]gblur=sigma={rim_px * 2.0:.2f},lut=y='clip(val*1.5,0,255)*0.42',format=gray[rg]")
        f.append(f"color=c=0x{col}:s={W}x{H}:r={fps}:d={dur:.3f},format=yuv420p[rc]")
        f.append("[rc][rg]alphamerge[rim0]")
    names = ["shd", "rim", "sub"] if rim_ranges else ["shd", "sub"]
    for nm in names:
        src = f"[{nm}0]"
        if sexpr:
            f.append(f"{src}scale=w='trunc({W}*({sexpr})/2)*2':h='trunc({H}*({sexpr})/2)*2':eval=frame:flags=bicubic[{nm}]")
        else:
            sw, sh = int(round(W * const_scale / 2)) * 2, int(round(H * const_scale / 2)) * 2
            f.append(f"{src}scale={sw}:{sh}:flags=bicubic[{nm}]")
    cx = lay.sub_cx + float(tr.get("x", 0))
    xe = f"{cx:.2f}-overlay_w/2"
    ty += _hook_shift(plan, lay)      # cut-out pushed down when the hook needs headroom (same shift as _geom)
    ye = f"{lay.h}-overlay_h+{ty:.2f}" if lay.anchor == "bottom" else f"{lay.head_y:.2f}-{htop * H:.2f}*overlay_h/{H}+{ty:.2f}"
    f.append(f"{cur}[shd]overlay=x='{xe}':y='{ye}+14':format=auto[l0]")
    cur = "[l0]"
    if rim_ranges:
        en = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in rim_ranges)
        f.append(f"{cur}[rim]overlay=x='{xe}':y='{ye}':format=auto:enable='{en}'[l0r]")
        cur = "[l0r]"
    f.append(f"{cur}[sub]overlay=x='{xe}':y='{ye}':format=auto[l1]")
    return f, "[l1]"


def _subject_png(master: str, matte: str, t: float, plan: dict, out: Path, lay: ct.Layout | None = None,
                 rim: tuple[list, int, str] | None = None) -> None:
    """RGBA still of shadow+cutout at time t through the SAME filter chain as the render. Both inputs are re-timed
    from 0 (setpts=PTS-STARTPTS): a held-still 5 fps master/matte has its first frame at pts 0.1, which used to leave
    the overlay without a subject frame and the still black."""
    lay = lay or ct.get_layout("9x16")
    S = scale_at(plan, lay, t)
    r = rim or ([], 2, "#ffe9d2")
    r = ([(0.0, 1.0)] if r[0] else [], r[1], r[2])
    chain, last = _subject_filters(f"[base]", "0:v", "1:v", plan, lay, 1, 1, None, S, r)
    fc = ";".join([f"color=c=black@0.0:s={lay.w}x{lay.h}:r=1:d=1,format=rgba[base]"] + chain + [f"{last}format=rgba[out]"])
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", master, "-ss", f"{t:.3f}", "-i", matte,
          "-filter_complex", fc, "-map", "[out]", "-frames:v", "1", str(out)])


# ---------- lint ----------
def lint_render_inputs(plan: dict, assets: dict, size=None) -> list[str]:
    """Human-readable problems that would make the render fail or look bad. Empty list = good to go."""
    lay = ct.layout_for_size(size)
    probs: list[str] = []
    D = float(plan.get("duration", 0))
    for key in ("master", "matte"):
        p = assets.get(key)
        if not p or not Path(p).exists():
            probs.append(f"missing asset '{key}': {Path(str(p)).name if p else 'not provided'}")
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
            preps.append(prep_bg(b, assets, lay))
        except Exception as e:   # unreadable image
            probs.append(f"background '{b['id']}': cannot prepare image ({type(e).__name__})")
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
        x0, y0, x1, y1 = (max(0, int(v)) for v in box)
        return im.crop((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1))).resize((1, 1), Image.BOX).getpixel((0, 0))

    for t in plan.get("text") or []:
        tid = t.get("id", "?")
        if t.get("region", "top_center") not in lay.regions and not t.get("box"):
            probs.append(f"text '{tid}': unknown region '{t.get('region')}'")
            continue
        if t["end"] <= t["start"]:
            probs.append(f"text '{tid}': end {t['end']}s is not after start {t['start']}s")
        if t["end"] > D + 1e-6:
            probs.append(f"text '{tid}': ends at {t['end']}s, after the {D}s video")
        mid = (t["start"] + t["end"]) / 2
        hb = head_box(plan, lay, mid)
        _, _, info = ct.render_block(t, lay, hb)
        st = ct.resolve_style(t, lay)
        want = st["size"]
        if not info["fits"] or info["size"] < 0.7 * want:
            probs.append(f"text '{tid}': does not fit its region at the requested size (shrunk {want} -> {info['size']}px, {len(info['lines'])} lines)")
        over = ct.box_overflow(info["box"], lay.safe)
        if over:
            probs.append(f"text '{tid}' is outside the safe zone: " + "; ".join(over))
        if hb is not None and info["role"] in ("hook", "callout", "other"):
            bx = info["box"]
            if bx[0] < hb[2] and bx[2] > hb[0] and bx[1] < hb[3] and bx[3] > hb[1]:
                probs.append(f"text '{tid}' overlaps the speaker's head ({info['role']} must stay above or beside it)")
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
        cbox = tuple(lay.cap_box)
        cst = caption_style(cap)
        local = region_color(cbox, cap["chunks"][0]["start"])
        if local:
            eff = ct.blend((12, 4, 10), local, min(scrim, ct.SCRIM_CAP) if lay.scrim else 0.0)
            ratio = ct.contrast_ratio(cst["fill"], eff)
            if ratio < 3.0:
                probs.append(f"captions: low contrast {ratio:.1f}:1 even on the scrim: raise scrim or darken the background")
        for i, c in enumerate(cap["chunks"]):
            _, _, info = ct.render_caption(c["text"], cst, cbox, None, lay)
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
def _prep_assets(plan: dict, assets: dict, workdir: Path, lay: ct.Layout) -> tuple[list[dict], list[Path]]:
    """Per-background: dict(prepared still path, kb flag, luma). Stills are 2x for Ken Burns, 1x for gradients."""
    out, paths = [], []
    for i, b in enumerate(_bgs(plan)):
        im = prep_bg(b, assets, lay)
        kb = b["kind"] != "gradient" and bool(_backdrop_path(b, assets))
        p = workdir / f"bg{i}.png"
        (im.resize((lay.w * 2, lay.h * 2), Image.LANCZOS) if kb else im).save(p)
        out.append({"path": p, "kb": kb, "img": im, "luma": _mean_luma(im)})
        paths.append(p)
    return out, paths


def render_composition(plan: dict, assets: dict, out_path: str | Path, *, size: tuple[int, int] | None = (1080, 1920),
                       fps: int = 30, preview: bool = False, workdir: str | Path | None = None) -> dict:
    """Execute a composition plan. The layout comes from `size` (9:16, 1:1 or 16:9; the output has exactly `size`).
    preview=True -> one PNG still per beat (half canvas, 540x960 for 9:16) + a contact sheet and returns
    {"out": contact sheet, "frames_dir", "frames", "seconds", "layers"}; else renders the mp4 and returns
    {"out", "seconds", "layers", "frames_dir"}. out_path in preview mode: a directory (or a file path whose stem
    becomes the directory). Raises ValueError for an invalid plan / missing assets, ComposeFail for ffmpeg failures
    and timeouts (the caller then keeps the simple cut)."""
    t0 = time.time()
    perr = validate_plan(plan)
    if perr:
        raise ValueError("invalid composition plan: " + "; ".join(perr[:4]))
    lay = ct.layout_for_size(size)
    errs = [e for e in lint_render_inputs(plan, assets, size) if e.startswith("missing asset") or "no background" in e]
    if errs:
        raise ValueError("cannot render: " + "; ".join(errs))
    out_path = Path(out_path)
    own_tmp = None
    if workdir is None:
        own_tmp = tempfile.TemporaryDirectory(prefix="adf_compose_")
        wd = Path(own_tmp.name)
    else:
        wd = Path(workdir)
        wd.mkdir(parents=True, exist_ok=True)
    ok = False
    try:
        plan = copy.deepcopy(plan)
        plan["size"], plan["fps"] = [W, H], fps
        master, matte = str(assets["master"]), str(assets["matte"])
        md = probe_duration(master)
        D = float(plan["duration"])
        if md and md + 0.1 < D:
            D = round(md, 3)
            plan["duration"] = D
        elif not preview:
            fit_plan_to_master(plan, md)
            D = float(plan["duration"])
        layers = build_layers(plan, assets, wd, lay)
        if preview:
            r = _preview(plan, assets, master, matte, layers, out_path, wd, fps, t0, lay)
        else:
            r = _full(plan, assets, master, matte, layers, out_path, wd, tuple(size) if size else (lay.w, lay.h), fps, t0, D, lay)
            if own_tmp:
                r["frames_dir"] = None     # the scratch dir is gone after this call
        r["layout"] = lay.name
        ok = True
        return r
    finally:
        if own_tmp:
            own_tmp.cleanup()
        if not ok and not preview:
            out_path.unlink(missing_ok=True)   # never leave a half-written mp4 behind


def fit_plan_to_master(plan: dict, master_s: float) -> dict:
    """Make the plan timeline match the master. A master longer than the plan (e.g. the end hold added after the plan was
    written) stretches every plan-timed beat, text block, PiP and plan-timed caption by master/plan so nothing ends early;
    speech-timed (asr) captions already sit on the master timeline and are left alone. The CTA pill is then clamped to
    the last CTA_MAX_S seconds and runs to the end. Returns the plan (modified in place)."""
    D = float(plan["duration"])
    if master_s and master_s > D + RESCALE_TOL:
        k = master_s / D
        for b in plan.get("background") or plan.get("backgrounds") or []:
            b["start"] = round(float(b.get("start", 0)) * k, 3)
        for o in (plan.get("text") or []) + (plan.get("overlays") or []):
            o["start"], o["end"] = round(float(o["start"]) * k, 3), round(float(o["end"]) * k, 3)
        cap = plan.get("captions") or {}
        if cap.get("source") != "asr":
            for c in cap.get("chunks") or []:
                c["start"], c["end"] = round(float(c["start"]) * k, 3), round(float(c["end"]) * k, 3)
        plan["meta"] = dict(plan.get("meta") or {}, rescaled={"from": round(D, 3), "to": round(master_s, 3)})
        D = round(master_s, 3)
        plan["duration"] = D
    for t in plan.get("text") or []:
        if t.get("role") == "cta" or t.get("style") == "cta_pill":
            t["start"] = round(min(max(float(t["start"]), D - CTA_MAX_S), max(D - 1.0, 0.0)), 3)
            t["end"] = D
    return plan


def _full(plan, assets, master, matte, layers, out: Path, wd: Path, size, fps, t0, D, lay: ct.Layout) -> dict:
    th = _threads()
    out.parent.mkdir(parents=True, exist_ok=True)
    bgs = _bgs(plan)
    preps, _ = _prep_assets(plan, assets, wd, lay)
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
        tr = b.get("transition_in") or {}
        d = 0.0 if i == 0 else max(0.04, float(tr.get("dur", 0.6)))
        if tr.get("type") == "flash":
            d = 0.06
        offs.append(b["start"] - d / 2)
        durs.append(d)
    bi, fc = [], []
    for i, b in enumerate(bgs):
        end_t = (offs[i + 1] + durs[i + 1]) if i + 1 < len(bgs) else D
        length = max(1.0, end_t - offs[i] + 0.5)
        idx = add(["-loop", "1", "-framerate", str(fps), "-t", f"{length:.3f}", "-i", str(preps[i]["path"])])
        if preps[i]["kb"]:
            nfr = max(1, int(round(length * fps)))
            fc.append(f"[{idx}:v]zoompan=z='1+{KB_ZOOM}*on/{nfr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={lay.w}x{lay.h}:fps={fps},"
                      f"format=yuv420p,setsar=1[b{i}]")
        else:
            fc.append(f"[{idx}:v]scale={lay.w}:{lay.h},format=yuv420p,fps={fps},setsar=1[b{i}]")
        bi.append(f"[b{i}]")
    cur = bi[0]
    for i in range(1, len(bgs)):
        tr = (bgs[i].get("transition_in") or {}).get("type", "fade")
        fc.append(f"{cur}{bi[i]}xfade=transition={TRANSITIONS.get(tr, 'fade')}:duration={durs[i]:.3f}:offset={offs[i]:.3f}[bgx{i}]")
        cur = f"[bgx{i}]"
    fc.append(f"{cur}format=yuv420p[bgf]")
    sexpr = _scale_expr(plan, lay)
    chain, cur = _subject_filters("[bgf]", f"{vi}:v", f"{mi}:v", plan, lay, fps, D, sexpr, scale_at(plan, lay, 0.0), _rim_beats(plan, preps))
    fc.extend(chain)
    flashes = [e for e in _events(plan, D) if e["kind"] == "flash"]
    if flashes:
        expr = "+".join(f"{FLASH_PEAK}*max(0,1-abs(t-{e['t']:.3f})/{_flash_half(e):.3f})" for e in flashes)
        fc.append(f"{cur}eq=brightness='{expr}':eval=frame[lf]")
        cur = "[lf]"
    # animated layers
    for li, L in enumerate(layers):
        ii = add(["-framerate", str(fps), "-i", str(wd / f"{L.paths[0].stem[:-3]}_%02d.png")])
        x, y, cw, ch = L.xywh
        dur = L.end - L.start
        hold = max(0.0, dur - len(L.paths) / fps) + 0.1
        fout = "" if L.kind == "caption" else f"fade=t=out:st={max(0.0, dur - FADE_OUT):.3f}:d={FADE_OUT}:alpha=1,"
        fc.append(f"[{ii}:v]format=rgba,tpad=stop_mode=clone:stop_duration={hold:.3f},"
                  f"{fout}setpts=PTS-STARTPTS+{L.start:.3f}/TB[a{li}]")
        fc.append(f"{cur}[a{li}]overlay={x}:{y}:enable='between(t,{L.start:.3f},{L.end:.3f})':format=auto:eof_action=pass[o{li}]")
        cur = f"[o{li}]"
    scale = f"scale={size[0]}:{size[1]}:flags=lanczos," if tuple(size) != (lay.w, lay.h) else ""
    fc.append(f"{cur}{scale}format=yuv420p[vout]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-threads", str(th), "-filter_complex_threads", str(th), *ins,
           "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", f"{vi}:a?", "-t", f"{D:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(fps),
           "-c:a", "copy", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
           "-movflags", "+faststart", str(out)]
    t1 = time.time()
    _run(cmd, FULL_TIMEOUT)
    return {"out": str(out), "seconds": round(time.time() - t0, 2), "render_seconds": round(time.time() - t1, 2),
            "layers": [{"id": L.id, "kind": L.kind, "start": L.start, "end": L.end} for L in layers],
            "frames_dir": str(wd), "duration": D, "size": list(size), "fps": fps}


def _preview(plan, assets, master, matte, layers, out_path: Path, wd: Path, fps: int, t0: float, lay: ct.Layout) -> dict:
    fdir = out_path if not out_path.suffix else out_path.with_suffix("")
    fdir.mkdir(parents=True, exist_ok=True)
    bgs = _bgs(plan)
    ranges = _beat_ranges(plan)
    pw, ph = lay.w // 2, lay.h // 2
    frames = []
    preps = [{"luma": _mean_luma(prep_bg(b, assets, lay))} for b in bgs]
    rim = _rim_beats(plan, preps)
    for i, (b, (s, e)) in enumerate(zip(bgs, ranges)):
        mid = best_preview_time(plan, (s, e))
        # background at that time: Ken Burns zoom of the beat's own still
        life = max(0.5, e - s)
        z = 1 + KB_ZOOM * ((mid - s) / life) if (b["kind"] != "gradient" and _backdrop_path(b, assets)) else 1.0
        base = prep_bg(b, assets, lay)
        if z > 1:
            base = _kb_frame(base.resize((lay.w * 2, lay.h * 2), Image.LANCZOS), z, (lay.w, lay.h))
        im = base.convert("RGBA")
        subj = wd / f"subj_{i}.png"
        on = any(a <= mid < bb for a, bb in rim[0])
        _subject_png(master, matte, mid, plan, subj, lay, (rim[0] if on else [], rim[1], rim[2]))
        im.alpha_composite(Image.open(subj).convert("RGBA"))
        for L in layers:
            fr = L.frame_at(mid, fps)
            if fr is not None:
                im.alpha_composite(fr, (L.xywh[0], L.xywh[1]))
        p = fdir / f"beat{i + 1:02d}_{mid:05.2f}s.png"
        im.convert("RGB").resize((pw, ph), Image.LANCZOS).save(p)
        frames.append(p)
    sheet = fdir / "contact.png"
    cs = Image.new("RGB", (pw * len(frames) + 12 * (len(frames) - 1), ph), (24, 24, 24))
    for k, p in enumerate(frames):
        cs.paste(Image.open(p), (k * (pw + 12), 0))
    cs.save(sheet)
    return {"out": str(sheet), "seconds": round(time.time() - t0, 2), "frames_dir": str(fdir),
            "frames": [str(p) for p in frames], "layers": [{"id": L.id, "kind": L.kind, "start": L.start, "end": L.end} for L in layers]}

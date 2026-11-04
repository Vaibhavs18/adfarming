"""The COMPOSED look for the agent: composition plan, plates, composed preview stills, composed render.

The AI decides (Muse Spark writes the composition plan, compose_plan lints and repairs it); deterministic code
renders (compose.render_composition). This module is a mixin for `tools.Toolbox`; every tool still goes through
`Toolbox.call`, so the code-enforced gates, allow-lists and cost caps apply to it unchanged.

Tools (names in tools.SPECS):
  plan_composition     free/cheap  one Spark call -> plan.vN.json (a new version per call; human locks re-applied)
  backdrops            PAID        Muse Image plates for the plan's generated backgrounds, cached per run
  preview_composition  PAID cheap  SAM image cut-out of the REAL speaker at each background's midpoint + the
                                   deterministic preview renderer -> composed stills (falls back to the storyboard)
  render_composition   PAID        SAM video matte of the master + full render (9:16) + derived 1:1 and 16:9 + QC
                                   (falls back to the simple cuts, logged as a `fallback` event)

Artifacts: `plan` (step preview), `edit_plan` (step edit), `frames` (kind composed|storyboard), `cuts`, `qc`.
Files in the run dir: plan.json / plan.vN.json, plan_locks.json, look.json (run-level fallback to simple),
composition.json (the plan that was actually rendered), backdrops/, mattes/, previews/.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

LOOKS = ("composed", "simple")
COMPOSE_TOOLS = ("plan_composition", "backdrops", "preview_composition", "render_composition")
# paid composition tool -> step whose approval it needs (merged into studio.gates.TOOL_GATES by tools.py)
COMPOSE_GATES = {"backdrops": "ideas", "preview_composition": "ideas", "render_composition": "preview"}
LOOK_PRESETS = ("cutout_on_brand_colour", "cutout_on_textile_plate", "cutout_over_blurred_scene", "split_with_pip_card")

PLAN_USD = 0.01            # one Spark call (same flat estimate as tools.SPARK_CALL_USD)
PLATE_USD = 0.01           # Muse Image, per plate (storyboard.COST_PER_IMAGE_USD)
STILL_USD = 0.0025         # SAM 3.1 image segmentation, per still (listed $2.50 / 1k images)
SAM_FRAME_USD = 0.0002     # SAM 3.1 video, per sampled frame (listed $0.20 / 1k frames; 50-frame minimum)
SAM_FPS = 15
RENDER_USD = 0.02          # flat estimate for the ffmpeg render (compute time, not an API price)
PREVIEW_RENDER_USD = 0.0
QC_USD = 0.03
HOLD_S = 2.0               # cuts.HOLD_S: clean end hold on the master
TYPICAL_STILLS, TYPICAL_PLATES = 2, 1
MAX_PLAN_VERSIONS = 12
SAM_SUFFIX_GUARD = 1e-9

LOOK_LABEL = ("These stills use the real speaker, cut out of her footage by SAM, on the planned backgrounds. "
              "Backgrounds are brand colours or generated textile plates, never people.")
LOOK_EDITABLE = ["hook", "cta", "title", "callout", "preset"]
FIELD_ALIASES = {"headline": "hook", "look_preset": "preset", "callout_text": "callout"}
# steer words that are about the LOOK (plan) rather than the content (words spoken)
LOOK_WORDS = re.compile(r"\b(background|backdrop|plate|colou?r|palette|gradient|textile|headline|callout|call-out|tag|"
                        r"caption|captions|text|font|bigger|larger|smaller|calmer|calm|busy|brighter|darker|look|"
                        r"overlay|card|pip|transition|dissolve|layout|style|scrim|contrast)\b", re.I)


FIELD_MAX = 80             # hook / cta / title / callout, enforced at edit time
CTA_MAX_WORDS = 4
_INTERNAL = re.compile(r"\s[-\u2013\u2014]\s|\b(card|soft|end|fade|b-?roll|overlay|logo|cut to)\b", re.I)
_CTA_FILLER = re.compile(r"^(?:see|watch|view|check out|explore|discover|visit)\s+(?:this|the|our|these)?\s*", re.I)
_CTA_WEBSITE = re.compile(r"(?:see|watch|view|shop|explore|discover|check out)\s+(?:this|the|our|these)\s+(.+?)\s+"
                          r"(?:on|at|from|in)\s+our\s+(?:website|site|store|app|page)", re.I)
_ABS_PATH = re.compile(r"(?<![\w.])(?:[A-Za-z]:)?(?:/[^\s'\":,;()\[\]]+){2,}")


def clean_detail(text: str, limit: int = 300) -> str:
    """Event details must not leak absolute paths of the server: keep the file name only."""
    return _ABS_PATH.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1], str(text or ""))[:limit]


def check_field(key: str, value) -> str | None:
    """None if a human-edited text field is acceptable, else the reason it is ignored."""
    if not isinstance(value, str):
        return f"{key} (must be text)"
    if len(value.strip()) > FIELD_MAX:
        return f"{key} (too long: {len(value.strip())} characters, the maximum is {FIELD_MAX})"
    return None


def cta_is_internal(text: str) -> bool:
    return bool(_INTERNAL.search(text or ""))


def short_cta(cta: str) -> str:
    """The VISIBLE call to action: a short phrase (max 4 words) derived by code from the concept's cta field, which
    may carry an internal direction ('See this Maheshwari on our website - soft end card'). Never shows a direction."""
    raw = re.sub(r"\s+", " ", str(cta or "")).strip()
    seg = re.split(r"\s[-\u2013\u2014]\s|[:;(\[\n]", raw)[0].strip(" .,!")
    m = _CTA_WEBSITE.search(seg)
    if m:
        what = " ".join(m.group(1).split()[:2])
        out = f"Shop {what}"
    else:
        words = _CTA_FILLER.sub("", seg).split() if len(seg.split()) > CTA_MAX_WORDS else seg.split()
        out = " ".join(words[:CTA_MAX_WORDS])
    out = out.strip(" .,!")
    if not out or cta_is_internal(out) or len(out.split()) > CTA_MAX_WORDS:
        out = "Shop now"
    return out


def look_mode() -> str:
    """STUDIO_LOOK=composed|simple (default composed). simple = the old talking-head cut + storyboard."""
    v = (os.environ.get("STUDIO_LOOK") or "composed").strip().lower()
    return v if v in LOOKS else "composed"


def is_look_steer(text: str) -> bool:
    return bool(LOOK_WORDS.search(text or ""))


def _T():
    from . import tools
    return tools


def _cp():
    from .. import compose_plan
    return compose_plan


# ------------------------------------------------------------------ cost estimates (shown on Approve buttons)
def est_preview(n_stills: int = TYPICAL_STILLS, n_plates: int = TYPICAL_PLATES) -> tuple[float, list[dict]]:
    items = [{"what": "composition plan (Spark)", "usd": PLAN_USD},
             {"what": f"{n_stills} cut-out stills of the real speaker (SAM)", "usd": round(n_stills * STILL_USD, 4)}]
    if n_plates:
        items.append({"what": f"{n_plates} background plate(s) (Muse Image)", "usd": round(n_plates * PLATE_USD, 4)})
    return round(sum(i["usd"] for i in items), 4), items


def est_edit(duration_s: float, n_plates: int = 0) -> tuple[float, list[dict]]:
    frames = max(int(round(max(duration_s, 1.0) * SAM_FPS)), 50)
    items = [{"what": "speaker matte for the whole cut (SAM video)", "usd": round(frames * SAM_FRAME_USD, 4)},
             {"what": "composed render (ffmpeg, 9:16 plus 1:1 and 16:9)", "usd": RENDER_USD},
             {"what": "quality check (Wonder)", "usd": QC_USD}]
    if n_plates:
        items.append({"what": f"{n_plates} missing background plate(s) (Muse Image)", "usd": round(n_plates * PLATE_USD, 4)})
    return round(sum(i["usd"] for i in items), 4), items


# ------------------------------------------------------------------ pure helpers (unit-tested)
def tint_dim(img, colour: str | None, max_luma: float = 0.40, tint: float = 0.22):
    """Plates must not fight the speaker: blend a little of the palette colour in and dim anything bright."""
    from PIL import Image, ImageEnhance, ImageStat
    img = img.convert("RGB")
    if colour and re.fullmatch(r"#[0-9a-fA-F]{6}", colour):
        rgb = tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))
        img = Image.blend(img, Image.new("RGB", img.size, rgb), tint)
    luma = ImageStat.Stat(img.convert("L").resize((32, 32))).mean[0] / 255.0
    if luma > max_luma:
        img = ImageEnhance.Brightness(img).enhance(max_luma / luma)
    return img


def bg_ranges(plan: dict) -> list[tuple[float, float]]:
    bgs = sorted(plan.get("background") or [], key=lambda b: b.get("start", 0))
    d = float(plan.get("duration") or 0)
    return [(float(b.get("start", 0)), float(bgs[i + 1].get("start", 0)) if i + 1 < len(bgs) else d)
            for i, b in enumerate(bgs)]


def locate(concept: dict, t: float) -> tuple[int, float]:
    """Output time t -> (beat index, source time inside that beat's clip)."""
    wins = _cp().beat_windows(concept)
    beats = concept.get("beats") or []
    if not beats:
        raise ValueError("concept has no beats")
    idx = len(wins) - 1
    for i, (s, e) in enumerate(wins):
        if s <= t < e:
            idx = i
            break
    s, e = wins[idx]
    b = beats[idx]
    src = float(b["src_in"]) + min(max(t - s, 0.0), max(e - s - 0.05, 0.0))
    return idx, round(src, 3)


def beats_sig(concept: dict) -> str:
    raw = json.dumps([[b.get("clip"), round(float(b.get("src_in", 0)), 3), round(float(b.get("src_out", 0)), 3)]
                      for b in concept.get("beats") or []])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def retime_plan(plan: dict, concept: dict, captions: list[dict], assets_info: dict | None = None,
                hold_s: float = HOLD_S) -> dict:
    """Re-derive the plan from the (speech-snapped) concept, add the end hold so the CTA lands on a clean frame,
    and fill the speech-timed captions. Human-locked texts come from the concept (repair_plan forces them)."""
    p = _cp().repair_plan(plan, [], concept, assets_info=assets_info)
    with_cta_short(p, concept, concept.get("cta_short"))
    total = float(p["duration"])
    d = round(total + hold_s, 2)
    p["duration"] = d
    for t in p.get("text") or []:
        if t.get("role") == "cta":
            t["end"] = d
    chunks = []
    for c in captions or []:
        txt = str(c.get("text") or "").strip()
        s, e = float(c.get("start", 0)), min(float(c.get("end", 0)), d)
        if txt and e - s > 0.05:
            chunks.append({"text": txt[:60], "start": round(s, 2), "end": round(e, 2)})
    p["captions"]["chunks"] = chunks
    p["meta"] = dict(p.get("meta") or {}, retimed=True, hold_s=hold_s)
    return p


def degrade_plates(plan: dict, failed: list[str], concept: dict) -> dict:
    """A plate that could not be made becomes a gradient from its own palette; the preset is re-inferred."""
    if not failed:
        return plan
    p = copy.deepcopy(plan)
    for b in p.get("background") or []:
        if b.get("id") in failed:
            cols = [c for c in (b.get("colors") or []) if re.fullmatch(r"#[0-9a-fA-F]{6}", str(c))] or ["#3b0a2a", "#7a1d3c"]
            b.update(kind="gradient", src=None, prompt=None, blur=0, zoom=1.0, dim=1.0, colors=cols)
    return _cp().repair_plan(p, [], concept)


def force_preset(plan: dict, concept: dict, preset: str, assets_info: dict | None = None) -> tuple[dict, bool]:
    """Apply a human-chosen look preset by code. Returns (plan, applied). Presets that need client assets
    (blurred scene, picture card) are only applied when the plan already carries what they need."""
    cp = _cp()
    p = copy.deepcopy(plan)
    if p.get("preset") == preset:
        return p, True
    cols = next((b.get("colors") for b in p.get("background") or [] if b.get("colors")), None)
    if preset == "cutout_on_brand_colour":
        for b in p.get("background") or []:
            if b.get("kind") != "gradient":
                b.update(kind="gradient", src=None, prompt=None, blur=0, zoom=1.0, dim=1.0,
                         colors=[c for c in (b.get("colors") or []) if c] or cols or ["#3b0a2a", "#7a1d3c"])
        p["overlays"] = []
    elif preset == "cutout_on_textile_plate":
        d = cp.default_plan(concept, cols, look=preset)
        if any(b.get("kind") == "generated" for b in d["background"]):
            p["background"] = d["background"]
    p = cp.repair_plan(p, [], concept, assets_info=assets_info)
    return p, p.get("preset") == preset


def apply_callout(plan: dict, text: str) -> tuple[dict, bool]:
    p = copy.deepcopy(plan)
    blocks = [t for t in p.get("text") or [] if t.get("role") == "callout"]
    if text == "":
        p["text"] = [t for t in p.get("text") or [] if t.get("role") != "callout"]
        return p, True
    if not blocks:
        return p, False
    for t in blocks:
        t["text"] = text
    return p, True


def _median(vals: list[float]) -> float:
    v = sorted(vals)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def head_meta(results: list[dict]) -> dict | None:
    """plan["meta"]["head"] = {"top_frac", "bbox_frac"} from matte results (median over the stills/video)."""
    tops = [float(r["head_top_frac"]) for r in results if isinstance(r, dict) and isinstance(r.get("head_top_frac"), (int, float))]
    boxes = [r["subject_bbox_frac"] for r in results if isinstance(r, dict) and isinstance(r.get("subject_bbox_frac"), (list, tuple))
             and len(r["subject_bbox_frac"]) == 4 and all(isinstance(x, (int, float)) for x in r["subject_bbox_frac"])]
    if not tops and not boxes:
        return None
    head: dict = {}
    if tops:
        head["top_frac"] = round(_median(tops), 4)
    if boxes:
        head["bbox_frac"] = [round(_median([b[i] for b in boxes]), 4) for i in range(4)]
    return head


def set_head(plan: dict, results: list[dict]) -> dict:
    h = head_meta(results)
    if h:
        plan["meta"] = dict(plan.get("meta") or {}, head=h)
    return plan


def with_cta_short(plan: dict, concept: dict, human_cta: str | None = None) -> dict:
    """Every cta block gets `cta_short` equal to its visible text. The planner's repair already derives a short CTA;
    a human's own CTA text is kept verbatim; if the visible text is still an internal direction (or too long) code
    replaces it with a short phrase derived from the concept's cta field."""
    human = human_cta.strip() if isinstance(human_cta, str) and human_cta.strip() else None
    for t in plan.get("text") or []:
        if t.get("role") != "cta":
            continue
        cur = (t.get("text") or "").strip()
        if human:
            cur = human
        elif not cur or cta_is_internal(cur) or len(cur.split()) > CTA_MAX_WORDS:
            cur = short_cta(concept.get("cta") or cur)
        t["text"] = t["cta_short"] = cur
    return plan


def render_view(plan: dict) -> dict:
    """The plan as handed to the renderer: the cta block shows cta_short, never the raw concept cta field."""
    p = copy.deepcopy(plan)
    for t in p.get("text") or []:
        if t.get("role") == "cta":
            t["text"] = t.get("cta_short") or short_cta(t.get("text") or "")
    return p


def preview_time(plan: dict, i: int, s: float, e: float) -> float:
    """Where in background beat i (s..e) the preview still is taken: compose.best_preview_time when the renderer
    has it, else the middle of the headline's on-screen window inside the beat (so the headline is visible)."""
    from .. import compose
    t = None
    fn = getattr(compose, "best_preview_time", None)
    if callable(fn):
        for call in (lambda: fn(plan, s, e), lambda: fn(plan, i), lambda: fn(plan, (s, e))):
            try:
                v = call()
                if isinstance(v, (int, float)):
                    t = float(v)
                    break
            except Exception:
                continue
    if t is None:
        hooks = [x for x in plan.get("text") or [] if x.get("role") == "hook"]
        for h in hooks:
            a, b = max(float(h.get("start", s)), s), min(float(h.get("end", e)), e)
            if b - a > 0.1:
                t = (a + b) / 2
                break
    if t is None:
        t = (s + e) / 2
    return round(min(max(t, s), max(s, e - 0.05)), 3)


class ComposeFail(Exception):
    """A composed step failed in a way that triggers the fallback to the simple look."""


# ------------------------------------------------------------------ default backends (all injectable)
def d_plan_composition(run, concept, brief, memory_text, assets_info, **kw):
    from .. import compose_plan
    return compose_plan.plan_composition(run, concept, brief, memory_text, assets_info, **kw)


def d_make_plate(run, name: str, prompt: str, colors: list[str], out_path) -> str:
    """Muse Image plate, tinted toward the palette and dimmed so the speaker keeps separation."""
    from PIL import Image
    from ..llm import image_b64
    t0 = time.time()
    data = image_b64(prompt)
    img = tint_dim(Image.open(io.BytesIO(data)), (colors or [None])[0])
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.stem}.part{out.suffix}")
    img.save(tmp)
    os.replace(tmp, out)                      # atomic: a half-written plate is never read as a cache hit
    run.trace("muse-image", {"prompt": prompt, "plate": name}, {"bytes": len(data)}, started=t0, cost_usd=PLATE_USD,
              artifacts=[str(out)], decision=f"background plate '{name}' (tinted, dimmed)")
    return str(out)


def d_matte_still(image_path, out_png, prompt: str = "woman") -> dict:
    from .. import matte
    return matte.extract_matte_still(image_path, out_png, prompt=prompt)


def d_matte_video(run, video, out_path, prompt: str = "woman", cache_dir=None) -> dict:
    from .. import matte
    return matte.extract_matte(run, video, out_path, prompt=prompt, cache_dir=cache_dir)


def d_render(plan, assets, out_path, preview: bool = False, workdir=None, size=None) -> dict:
    from .. import compose
    kw = {"size": tuple(size)} if size else {}
    return compose.render_composition(plan, assets, out_path, preview=preview, workdir=workdir, **kw)


def d_lint(plan, assets) -> list[str]:
    from .. import compose
    return compose.lint_render_inputs(plan, assets)


HELPER_TIMEOUT_S = 120
RATIO_SIZES = {"1x1": (1080, 1080), "16x9": (1920, 1080)}


def _ff(args: list[str]) -> None:
    try:
        p = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True,
                           timeout=HELPER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ComposeFail(f"ffmpeg helper timed out after {HELPER_TIMEOUT_S}s")
    if p.returncode:
        raise RuntimeError(f"ffmpeg failed: {clean_detail(p.stderr.strip()[-400:], 400)}")


def d_grab_frame(clip: dict, t: float, out_png) -> str:
    """One frame of the REAL footage at source time t, cover-fit to 1080x1920 like the master cut."""
    from .. import cuts
    out = Path(out_png)
    tmp = out.with_name(f".{out.stem}.part{out.suffix}")
    _ff(["-ss", f"{t:.3f}", "-i", str(cuts.clip_path(clip)), "-frames:v", "1", "-vf",
         "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1", str(tmp)])
    os.replace(tmp, out)
    return str(out)


def d_still_video(frames: list[tuple[str, float]], out_path, fps: int = 5) -> str:
    """Hold each still for its duration and concatenate: the preview renderer seeks into this like a master."""
    ins, fc = [], []
    for i, (png, dur) in enumerate(frames):
        ins += ["-loop", "1", "-framerate", str(fps), "-t", f"{max(dur, 0.2):.3f}", "-i", str(png)]
        fc.append(f"[{i}:v]scale=1080:1920,format=yuv420p,setsar=1[s{i}]")
    fc.append("".join(f"[s{i}]" for i in range(len(frames))) + f"concat=n={len(frames)}:v=1:a=0[out]")
    _ff([*ins, "-filter_complex", ";".join(fc), "-map", "[out]", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-r", str(fps), str(out_path)])
    return str(out_path)


def d_derive_ratio(src, ratio: str, crop_y: int, out_path) -> str:
    """1:1 = a square crop of the composed 9:16 around the cut-out; 16:9 = the composed 9:16 centred at full height
    (compose.py is a fixed 1080x1920 design space), the sides filled from the SAME render, blurred."""
    if ratio == "1x1":
        vf = f"[0:v]crop=1080:1080:0:{int(crop_y)}[v]"
    elif ratio == "16x9":
        vf = ("[0:v]split[a][b];[a]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,gblur=sigma=40[bg];"
              "[b]scale=-2:1080[fg];[bg][fg]overlay=(W-w)/2:0[v]")
    else:
        raise ValueError(f"cannot derive {ratio}")
    _ff(["-i", str(src), "-filter_complex", vf, "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-crf", "21",
         "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out_path)])
    return str(out_path)


def d_load_assets(client_id: str | None = None) -> dict:
    """Person-free background stills and PiP images the client supplied. v1: none (plates are generated)."""
    return {"info": {"backdrops": [], "pips": []}, "backdrops": {}, "pips": {}}


DEFAULTS = {
    "plan_composition": d_plan_composition, "make_plate": d_make_plate, "matte_still": d_matte_still,
    "matte_video": d_matte_video, "render": d_render, "lint": d_lint, "grab_frame": d_grab_frame,
    "still_video": d_still_video, "derive_ratio": d_derive_ratio, "load_assets": d_load_assets,
}


# ------------------------------------------------------------------ the mixin
class ComposeMixin:
    """Mixed into tools.Toolbox. Uses: self.b, self.run, self.dir, self.run_id, self._ev/_read/_write/_stale,
    self.state/current_step/working_concept/_freeze/clips/spent/_cost_since/full_brief/_memory_block."""

    _tool_mark = 0

    # ---------------- mode
    def look(self) -> str:
        f = self._read("look.json")
        if isinstance(f, dict) and f.get("mode") == "simple":
            return "simple"
        return look_mode()

    def _set_simple(self, step: str, reason: str, detail: str = "") -> None:
        detail = clean_detail(detail)
        self._write("look.json", {"mode": "simple", "reason": reason, "detail": detail, "step": step})
        self._ev("fallback", {"step": step, "from": "composed", "to": "simple", "scope": "look", "reason": reason,
                              "detail": detail}, "system")

    def _need_composed(self) -> None:
        if self.look() != "composed":
            raise _T().ToolError("not_composed", "the composed look is off for this run (STUDIO_LOOK=simple or a "
                                 "logged fallback)", False, "use storyboard / build_cuts")

    def _cap_room(self, extra: float) -> bool:
        return self.spent() + self._cost_since(self._tool_mark) + extra <= _T().run_cap_usd() + SAM_SUFFIX_GUARD

    @staticmethod
    def _write_atomic_json(path: Path, obj) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False))
        os.replace(tmp, path)

    # ---------------- plans
    def plan_rec(self) -> dict | None:
        return self._read("plan.json")

    def plan_current(self, st: dict | None = None) -> dict | None:
        """The current plan record unless it was marked stale (Back)."""
        st = st or self.state()
        rec = self.plan_rec()
        if not rec:
            return None
        for name in ("plan", "edit_plan"):
            a = st["artifacts"].get(name)
            if a and a["ref"] == f"{rec['id']}.json":
                return None if a["status"] == "stale" else rec
        return None

    def _save_plan(self, plan: dict, st: dict, how: str, guidance: str = "") -> dict:
        step = self.current_step(st)
        n = len(list(self.dir.glob("plan.v*.json"))) + 1
        rec = {"id": f"plan.v{n}", "version": n, "how": how, "step": step, "guidance": guidance[:600],
               "concept_id": self.working_concept(st).get("id"), "plan": plan}
        self._write(f"plan.v{n}.json", rec)
        self._write("plan.json", rec)
        in_preview = step in ("brief", "ideas", "preview")
        gates = _T().gates
        gates.propose_artifact(self.run_id, "preview" if in_preview else "edit", "plan" if in_preview else "edit_plan",
                               ref=f"plan.v{n}.json")
        if in_preview:
            self._stale([("frames", "preview")])
        else:
            self._stale([("cuts", "edit"), ("qc", "check")])
        return rec

    def plan_locks(self) -> dict:
        return self._read("plan_locks.json") or {}

    def _assets(self) -> dict:
        a = self.b.for_client("load_assets", self.client_id) or {}
        return {"info": a.get("info") or {"backdrops": [], "pips": []}, "backdrops": a.get("backdrops") or {},
                "pips": a.get("pips") or {}}

    def _plan_memory_text(self) -> str:
        return "\n".join(f"- [{i['kind']}] {i['text']}" for i in self.approved_memory())

    def _finalize_plan(self, plan: dict, concept: dict, assets: dict) -> dict:
        """repair (re-derives locked hook/cta/title from the concept) + the human's plan locks, in code."""
        cp = _cp()
        info = assets["info"]
        p = cp.repair_plan(plan, [], concept, assets_info=info)
        locks = self.plan_locks()
        if locks.get("preset") in LOOK_PRESETS:
            p, _ = force_preset(p, concept, locks["preset"], info)
        if isinstance(locks.get("callout"), str):
            p, _ = apply_callout(p, locks["callout"])
        p = cp.repair_plan(p, [], concept, assets_info=info)
        human = (self._read("human_locks.json") or {}).get(concept.get("id"), {}).get("cta")
        return with_cta_short(p, concept, human)

    def _cta_pass(self, plan: dict, concept: dict) -> dict:
        human = (self._read("human_locks.json") or {}).get(concept.get("id"), {}).get("cta")
        return with_cta_short(plan, concept, human)

    def _plan_guidance(self, st: dict, extra: str) -> str:
        parts = [extra.strip()] if (extra or "").strip() else []
        parts += [f"Human steer (verbatim): {s['text']}" for s in st["steers"]
                  if s["step"] in ("preview", "edit", "check") and s["text"]]
        return "\n".join(parts)

    def _t_plan_composition(self, a: dict, call_id):
        T = _T()
        self._need_composed()
        st = self.state()
        n_have = len(list(self.dir.glob("plan.v*.json")))
        if n_have >= MAX_PLAN_VERSIONS:
            raise T.ToolError("limit", f"at most {MAX_PLAN_VERSIONS} plan versions per run")
        wc = self._freeze(copy.deepcopy(self.working_concept(st)))
        assets = self._assets()
        locks = self.plan_locks()
        guidance = self._plan_guidance(st, a.get("guidance") or "")
        plan = self.b.plan_composition(self.run, copy.deepcopy(wc), self.full_brief(st), self._plan_memory_text(),
                                       assets["info"], guidance=guidance, look=locks.get("preset"))
        plan = self._finalize_plan(plan, wc, assets)
        rec = self._save_plan(plan, st, how=(plan.get("meta") or {}).get("source") or "spark", guidance=guidance)
        cp = _cp()
        return T.ToolResult(True, {
            "plan_id": rec["id"], "version": rec["version"], "source": (plan.get("meta") or {}).get("source"),
            "preset": plan["preset"], "summary": cp.plan_summary(plan),
            "plates": [r["name"] for r in cp.backdrop_requests(plan)], "human_locks": locks,
            "guidance_sent": guidance[:300],
            "note": "frames are stale: call preview_composition (backdrops is an optional prefetch), then ask_human"
                    if self.current_step(st) == "preview" else
                    "the cut is stale: call render_composition (the master is reused), then ask_human"})

    # ---------------- plates
    def _plate_path(self, req: dict, bg: dict) -> Path:
        key = hashlib.sha1((req["prompt"] + "|" + ",".join(bg.get("colors") or [])).encode()).hexdigest()[:10]
        return self.dir / "backdrops" / f"{req['name']}_{key}.png"

    def _missing_plates(self, plan: dict) -> int:
        bgs = {b["id"]: b for b in plan.get("background") or []}
        return sum(1 for r in _cp().backdrop_requests(plan) if not self._plate_path(r, bgs[r["name"]]).exists())

    def _plates(self, plan: dict, only_cached: bool = False) -> tuple[dict, list[str]]:
        """({bg id: path}, [ids that could not be made]). Cached per run; a failed or unaffordable plate is not fatal."""
        out, failed = {}, []
        bgs = {b["id"]: b for b in plan.get("background") or []}
        for req in _cp().backdrop_requests(plan):
            bg = bgs[req["name"]]
            path = self._plate_path(req, bg)
            if path.exists():
                out[req["name"]] = str(path)
                continue
            if only_cached or not self._cap_room(PLATE_USD):
                failed.append(req["name"])
                continue
            try:
                out[req["name"]] = self.b.make_plate(self.run, req["name"], req["prompt"], bg.get("colors") or [], path)
            except Exception as e:
                self.run.trace("muse-image", {"plate": req["name"]}, {"error": f"{type(e).__name__}"[:60]}, status="error",
                               decision=f"plate '{req['name']}' failed: {str(e)[:100]}; a gradient is used instead")
                failed.append(req["name"])
        return out, failed

    def _t_backdrops(self, a: dict, call_id):
        self._need_composed()
        st = self.state()
        rec = self.plan_current(st)
        if rec is None:
            raise _T().ToolError("missing_artifact", "no current composition plan", True, "call plan_composition first")
        plates, failed = self._plates(rec["plan"])
        return _T().ToolResult(True, {"plan_id": rec["id"], "plates": sorted(plates), "failed": failed,
                                      "note": "failed plates fall back to a gradient" if failed else "cached per run"})

    # ---------------- composed preview stills
    def _clip_map(self) -> dict[str, dict]:
        return {c["file"]: c for c in self.clips()}

    def _t_preview_composition(self, a: dict, call_id):
        T = _T()
        self._need_composed()
        st = self.state()
        wc = self._freeze(copy.deepcopy(self.working_concept(st)))
        rec = self.plan_current(st)
        if rec is None:
            raise T.ToolError("missing_artifact", "no current composition plan", True, "call plan_composition first")
        plan = copy.deepcopy(rec["plan"])
        try:
            plates, failed = self._plates(plan)
            plan_r = self._cta_pass(degrade_plates(plan, failed, wc), wc)
            files, notes, stats = self._preview_stills(plan_r, wc, plates, rec)
        except ComposeFail as e:
            return self._preview_fallback(st, str(e))
        except T.ToolError:
            raise
        except Exception as e:
            return self._preview_fallback(st, f"{type(e).__name__}: {str(e)[:160]}")
        if failed:
            notes.append(f"plate(s) {', '.join(failed)} unavailable: a gradient is used in the stills")
        self._write("frames.json", {"concept_id": wc["id"], "files": files, "kind": "composed", "plan_id": rec["id"],
                                    "notes": notes, "label": LOOK_LABEL})
        T.gates.propose_artifact(self.run_id, "preview", "frames", ref="frames.json")
        return T.ToolResult(True, {"plan_id": rec["id"], "frames": files, "notes": notes, **stats})

    def _preview_stills(self, plan: dict, wc: dict, plates: dict, rec: dict) -> tuple[list[str], list[str], dict]:
        wd = self.dir / "previews"
        (wd / "frames").mkdir(parents=True, exist_ok=True)
        (self.dir / "mattes").mkdir(exist_ok=True)
        by_file = self._clip_map()
        prompt = ((plan.get("subject") or {}).get("mask") or {}).get("prompt") or "woman"
        ranges = bg_ranges(plan)
        if not ranges:
            raise ComposeFail("plan has no background")
        cache: dict[str, tuple[str, str]] = {}
        slots: list[tuple[str, str, float]] = []
        heads: list[dict] = []
        n_new = n_cached = 0
        for i, (s, e) in enumerate(ranges):
            mid = preview_time(plan, i, s, e)       # where the headline / callout is on screen
            bi, src_t = locate(wc, mid)
            clip_name = wc["beats"][bi]["clip"]
            clip = by_file.get(clip_name)
            if clip is None:
                raise ComposeFail(f"clip {clip_name!r} not found")
            key = hashlib.sha1(f"{clip_name}|{src_t:.2f}|{prompt}".encode()).hexdigest()[:12]
            if key not in cache:
                frame, matte = wd / "frames" / f"f_{key}.png", self.dir / "mattes" / f"still_{key}.png"
                side = self.dir / "mattes" / f"still_{key}.json"
                if matte.exists() and frame.exists() and side.exists():
                    n_cached += 1
                    try:
                        heads.append(json.loads(side.read_text()))
                    except Exception:
                        pass
                else:
                    if not self._cap_room(STILL_USD):
                        raise _T().ToolError("cost_cap", "run cap would be exceeded by the next matte still", False,
                                             "stop spending: ask_human with what you have")
                    self.b.grab_frame(clip, src_t, frame)
                    t0 = time.time()
                    part = matte.with_name(f".{matte.stem}.part{matte.suffix}")
                    res = self.b.matte_still(str(frame), str(part), prompt)
                    cost = float(res.get("cost_usd") or STILL_USD) if isinstance(res, dict) else STILL_USD
                    ok = isinstance(res, dict) and not res.get("error") and part.exists()
                    if ok:
                        os.replace(part, matte)       # atomic cache write
                    self.run.trace("sam-3.1-still", {"clip": clip_name, "t": src_t, "prompt": prompt},
                                   {"error": (res or {}).get("error")} if not ok else {"head_top_frac": res.get("head_top_frac")},
                                   started=t0, cost_usd=cost, status="ok" if ok else "error",
                                   artifacts=[str(matte)] if ok else [],
                                   decision=f"cut out the speaker from {clip_name} @ {src_t}s for the preview")
                    if not ok:
                        raise ComposeFail(f"matte still failed: {(res or {}).get('error')}")
                    n_new += 1
                    h = {k: res.get(k) for k in ("head_top_frac", "subject_bbox_frac") if res.get(k) is not None}
                    heads.append(h)
                    self._write_atomic_json(side, h)
                cache[key] = (str(frame), str(matte))
            slots.append((*cache[key], e - s))
        set_head(plan, heads)       # the renderer places headline / callouts around the head from plan["meta"]["head"]
        master = self.b.still_video([(f, d) for f, _, d in slots], wd / "prev_master.mp4")
        matte = self.b.still_video([(m, d) for _, m, d in slots], wd / "prev_matte.mp4")
        assets = {"master": master, "matte": matte, "backdrops": {**plates, **self._assets()["backdrops"]},
                  "pip_images": self._assets()["pips"], "clips": {k: str(_clip_path(v)) for k, v in by_file.items()}}
        t0 = time.time()
        try:
            out = self.b.render(render_view(plan), assets, wd / "stills", preview=True, workdir=wd / "work")
        finally:
            shutil.rmtree(wd / "work", ignore_errors=True)
        self.run.trace("ffmpeg-render", {"preview": True, "plan": rec["id"]}, {"frames": len(out.get("frames") or [])},
                       started=t0, cost_usd=PREVIEW_RENDER_USD, decision="deterministic preview stills (composed)")
        frames = out.get("frames") or []
        if not frames:
            raise ComposeFail("the preview renderer returned no frames")
        names = []
        for i, p in enumerate(frames, 1):
            nm = f"look_{wc['id']}_v{rec['version']}_{i}.png"
            _copy(p, self.dir / nm)
            names.append(nm)
        if out.get("out") and Path(out["out"]).exists():
            _copy(out["out"], self.dir / f"look_{wc['id']}_v{rec['version']}_contact.png")
        return names, [], {"matte_stills": n_new, "matte_stills_cached": n_cached, "plates": sorted(plates)}

    def _preview_fallback(self, st: dict, reason: str):
        T = _T()
        reason = clean_detail(reason)
        self._set_simple("preview", "composition_preview_failed", reason)
        if not self._cap_room(0.04):
            raise T.ToolError("cost_cap", "run cap would be exceeded by the storyboard fallback", False,
                              "ask_human with what you have")
        names = self._storyboard_files(st)
        return T.ToolResult(True, {"fallback": True, "reason": reason[:200], "look": "simple", "frames": names,
                                   "note": "composition failed; the old storyboard frames (AI stand-ins) were made"})

    # ---------------- composed render (edit step)
    def master_path(self) -> Path:
        return self.dir / "work" / "master_hold.mp4"

    def master_fresh(self, st: dict | None = None) -> bool:
        meta = self._read("meta.json")
        if not meta or not self.master_path().exists():
            return False
        try:
            return meta.get("beats_sig") == beats_sig(self.working_concept(st))
        except Exception:
            return False

    def _t_render_composition(self, a: dict, call_id):
        T = _T()
        self._need_composed()
        st = self.state()
        wc = self._freeze(copy.deepcopy(self.working_concept(st)))
        meta = self._read("meta.json")
        if not meta or not self.master_fresh(st):
            raise T.ToolError("missing_artifact", "no up-to-date master cut to compose", True, "call build_cuts first")
        assets = self._assets()
        rec = self.plan_current(st)
        if rec is None or rec.get("concept_id") != wc["id"]:   # no plan, or a stale one (Back): make a fresh version
            if not self._cap_room(PLAN_USD + est_edit(_cp().total_duration(wc) + HOLD_S)[0]):
                raise T.ToolError("cost_cap", "run cap would be exceeded by a new plan + the render", False,
                                  "stop spending: ask_human with what you have")
            guidance = self._plan_guidance(st, "")
            fresh = self._finalize_plan(self.b.plan_composition(
                self.run, copy.deepcopy(wc), self.full_brief(st), self._plan_memory_text(), assets["info"],
                guidance=guidance, look=self.plan_locks().get("preset")), wc, assets)
            rec = self._save_plan(fresh, st, how="render_time", guidance=guidance)
        base = rec["plan"]
        try:
            out = self._compose(st, wc, meta, base, rec, assets, a.get("subject_prompt"))
        except ComposeFail as e:
            return self._render_fallback(st, wc, meta, str(e))
        except T.ToolError:
            raise
        except Exception as e:
            return self._render_fallback(st, wc, meta, f"{type(e).__name__}: {str(e)[:160]}")
        return T.ToolResult(True, out)

    def _compose(self, st: dict, wc: dict, meta: dict, base: dict, rec: dict | None, assets: dict, subject_prompt):
        T = _T()
        master = self.master_path()
        plan = retime_plan(self._finalize_plan(base, wc, assets), wc, meta.get("captions") or [], assets["info"])
        plates, failed = self._plates(plan)
        plan = degrade_plates(plan, failed, wc) if failed else plan
        if failed:  # degrade_plates re-runs repair_plan, which resets duration/captions: redo the retime
            plan = retime_plan(plan, wc, meta.get("captions") or [], assets["info"])
        plan = self._cta_pass(plan, wc)
        prompt = subject_prompt or ((plan.get("subject") or {}).get("mask") or {}).get("prompt") or "woman"
        if not self._cap_room(est_edit(plan["duration"])[0] - QC_USD):
            raise T.ToolError("cost_cap", "run cap would be exceeded by the matte + render", False,
                              "stop spending: ask_human with what you have")
        mres = self.b.matte_video(self.run, master, self.dir / "work" / "matte.mp4", prompt,
                                  self.dir / "matte_cache")
        if not isinstance(mres, dict) or mres.get("error") or not mres.get("matte"):
            raise ComposeFail(f"matte failed: {clean_detail((mres or {}).get('error'), 200)}")
        set_head(plan, [mres])           # head box from the matte: headline above the head, callouts at the margin
        clips = {k: str(_clip_path(v)) for k, v in self._clip_map().items()}
        rassets = {"master": str(master), "matte": mres["matte"], "backdrops": {**plates, **assets["backdrops"]},
                   "pip_images": assets["pips"], "clips": clips}
        lint = self.b.lint(plan, rassets) or []
        blocking = [m for m in lint if m.startswith("missing asset") or "contains a person" in m or "no background" in m]
        if blocking:
            raise ComposeFail("render inputs rejected: " + "; ".join(blocking)[:200])
        cid = wc["id"]
        outs = {"9x16": f"{cid}_9x16.mp4", "1x1": f"{cid}_1x1.mp4", "16x9": f"{cid}_16x9.mp4"}
        old = dict(meta.get("cuts") or {})
        simple = dict(meta.get("simple_cuts") or old)
        # the simple cuts are the fallback: keep them under their own names before the composed 9:16 takes the name
        keep = {}
        for r, n in simple.items():
            if (self.dir / n).is_file():
                kn = f"{cid}_simple_{r}.mp4"
                if n != kn:        # n is a fresh build_cuts output (meta was rebuilt): always refresh the saved copy
                    _copy(self.dir / n, self.dir / kn)
                keep[r] = kn
        work = self.dir / "work" / "compose"
        view = render_view(plan)
        t0 = time.time()
        part9 = self.dir / f".{outs['9x16']}.part.mp4"
        try:
            rres = self.b.render(view, rassets, part9, preview=False, workdir=work)
            render_s = round(time.time() - t0, 2)
            self.run.trace("ffmpeg-render", {"plan": rec["id"] if rec else "default", "duration": plan["duration"]},
                           {"seconds": (rres or {}).get("seconds"), "render_seconds": (rres or {}).get("render_seconds")},
                           started=t0, cost_usd=RENDER_USD, artifacts=[outs["9x16"]],
                           decision=f"composed 9:16 render in {render_s}s")
            produced = Path((rres or {}).get("out") or part9)
            if not produced.is_file():
                raise ComposeFail("the renderer produced no 9:16 file")
            os.replace(produced, self.dir / outs["9x16"])
            ratios = {"9x16": "composed"}
            for r, size in RATIO_SIZES.items():
                ratios[r] = self._render_ratio(r, size, view, rassets, outs[r], keep.get(r), work)
        finally:
            part9.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)
        verdict, hist = self._qc_composed(st, wc, meta, self.dir / outs["9x16"])
        comp = {"plan_id": rec["id"] if rec else "default", "plan": plan, "matte": {k: mres.get(k) for k in
                ("frames", "sampled_frames", "cost_usd", "head_top_frac", "subject_bbox_frac", "cached", "seconds")},
                "render_seconds": render_s, "lint": lint, "plates": sorted(plates), "plates_failed": failed,
                "ratios": ratios}
        self._write("composition.json", comp)
        meta.update(cuts=outs, simple_cuts=keep or simple, simple_sig=meta.get("beats_sig"), ratios=ratios, look="composed", plan_id=comp["plan_id"], qc=verdict,
                    qc_history=hist, composed={k: v for k, v in comp.items() if k != "plan"})
        self._write("meta.json", meta)
        T.gates.propose_artifact(self.run_id, "edit", "cuts", ref="meta.json")
        T.gates.propose_artifact(self.run_id, "check", "qc", ref="meta.json")
        return {"look": "composed", "plan_id": comp["plan_id"], "cuts": outs, "render_seconds": render_s,
                "matte": comp["matte"], "lint": lint[:6], "plates_failed": failed, "ratios": ratios,
                "qc": {"pass": verdict.get("pass"), "score": verdict.get("score"),
                       "issues": (verdict.get("issues") or [])[:5], "summary": verdict.get("summary")}}

    def _render_ratio(self, r: str, size: tuple[int, int], view: dict, rassets: dict, out_name: str,
                      simple_name: str | None, work: Path) -> str:
        """Render one ratio with its own layout (the renderer picks safe zones from `size`). A ratio the renderer
        cannot make good, or that fails, keeps the SIMPLE cut for that ratio and is marked 'simple'."""
        t1 = time.time()
        dst = self.dir / out_name
        part = self.dir / f".{out_name}.part.mp4"
        status, why = "composed", ""
        try:
            res = self.b.render(view, rassets, part, preview=False, workdir=work / r, size=size)
            produced = Path((res or {}).get("out") or part)
            if isinstance(res, dict) and (res.get("unsupported") or res.get("ok") is False):
                status, why = "simple", str(res.get("reason") or "layout not supported")
            elif not produced.is_file():
                status, why = "simple", "renderer produced no file"
            else:
                os.replace(produced, dst)
        except Exception as e:
            status, why = "simple", f"{type(e).__name__}: {e}"
        finally:
            part.unlink(missing_ok=True)
        if status == "simple":
            if simple_name and (self.dir / simple_name).is_file():
                _copy(self.dir / simple_name, dst)
        self.run.trace("ffmpeg-render", {"ratio": r}, {"file": out_name, "status": status}, started=t1, cost_usd=0.0,
                       artifacts=[out_name],
                       decision=(f"composed {r} render" if status == "composed" else
                                 f"{r}: composed layout not available ({clean_detail(why, 120)}); the simple cut is kept"))
        return status

    def _qc_composed(self, st: dict, wc: dict, meta: dict, video: Path) -> tuple[dict, list[dict]]:
        hist = list(meta.get("qc_history") or [])
        if not self._cap_room(QC_USD):
            v = {"pass": None, "score": None, "issues": ["qc skipped: run cost cap"], "fixes": [],
                 "summary": "QC skipped (cost cap)"}
        else:
            v = self.b.review(self.run, self._qc_brief(st), wc, meta.get("captions") or [], video)
        hist.append({"attempt": len(hist) + 1, "score": v.get("score"), "pass": v.get("pass") is True,
                     "summary": v.get("summary"), "look": "composed"})
        return v, hist

    def _qc_brief(self, st: dict) -> str:
        brief = (self.b.for_client("load_brief", self.client_id) or "").strip() or self.full_brief(st)
        hard = self._memory_block(hard_only=True)
        return brief + ("\n\nCLIENT HARD RULES (QC must verify):\n" + hard if hard else "")

    def _render_fallback(self, st: dict, wc: dict, meta: dict, reason: str):
        """The composed render failed: keep the simple cuts, review those, say so loudly (event + result)."""
        T = _T()
        reason = clean_detail(reason)
        self._set_simple("edit", "composed_render_failed", reason)
        simple = dict(meta.get("simple_cuts") or meta.get("cuts") or {})
        meta["cuts"] = simple
        meta["look"] = "simple_fallback"
        reason = clean_detail(reason, 300)
        meta["fallback_reason"] = reason
        verdict = {"pass": None, "score": None, "issues": [], "fixes": [], "summary": "QC skipped"}
        if "9x16" in simple and self._cap_room(QC_USD):
            verdict = self.b.review(self.run, self._qc_brief(st), wc, meta.get("captions") or [], self.dir / simple["9x16"])
        hist = list(meta.get("qc_history") or [])
        hist.append({"attempt": len(hist) + 1, "score": verdict.get("score"), "pass": verdict.get("pass") is True,
                     "summary": verdict.get("summary"), "look": "simple_fallback"})
        meta["qc"], meta["qc_history"] = verdict, hist
        self._write("meta.json", meta)
        T.gates.propose_artifact(self.run_id, "edit", "cuts", ref="meta.json")
        T.gates.propose_artifact(self.run_id, "check", "qc", ref="meta.json")
        return T.ToolResult(True, {"fallback": True, "look": "simple", "reason": reason[:200], "cuts": simple,
                                   "qc": {"pass": verdict.get("pass"), "score": verdict.get("score"),
                                          "summary": verdict.get("summary")},
                                   "note": "composition failed; the simple talking-head cuts are delivered"})

    # ---------------- human edits to the plan (applied by code, locked)
    def apply_plan_edits(self, step: str, fields: dict, st: dict) -> tuple[list[str], list[str]]:
        """Callout text and look preset: store as locks and, in the preview step, re-derive the plan by code (new
        version, frames stale). hook/cta/title are concept locks handled by the caller. Returns (applied, ignored)."""
        applied, ignored = [], []
        locks = self.plan_locks()
        rec = self.plan_current(st) or self.plan_rec()
        plan = rec["plan"] if rec else None
        try:
            wc = self._freeze(copy.deepcopy(self.working_concept(st)))
        except Exception:
            wc = None
        if "callout" in fields:
            bad = check_field("callout", fields["callout"])
            if bad:
                ignored.append(bad)
            else:
                text = fields["callout"].strip()
                if plan is None:
                    ok = True                      # no plan yet: the lock is applied when the plan is made
                else:
                    ok = apply_callout(plan, text)[1]
                if ok:
                    locks["callout"] = text
                    applied.append("callout")
                else:
                    ignored.append("callout (the current plan has no callout block to put the text in)")
        if "preset" in fields:
            if fields["preset"] not in LOOK_PRESETS:
                ignored.append(f"preset (unknown: choose one of {list(LOOK_PRESETS)})")
            else:
                ok = True
                if plan is not None and wc is not None:
                    try:
                        ok = force_preset(plan, wc, fields["preset"], self._assets()["info"])[1]
                    except Exception:
                        ok = False
                if ok:
                    locks["preset"] = fields["preset"]
                    applied.append("preset")
                else:
                    ignored.append(f"preset ({fields['preset']} cannot be applied: the footage/plan lacks what it needs)")
        self._write("plan_locks.json", locks)
        return applied, ignored

    def replan_by_code(self, st: dict, how: str = "human_edit") -> str | None:
        """New plan version from the current one with the concept + plan locks re-applied (no model call)."""
        rec = self.plan_current(st) or self.plan_rec()
        if rec is None or self.look() != "composed":
            return None
        wc = self._freeze(copy.deepcopy(self.working_concept(st)))
        plan = self._finalize_plan(rec["plan"], wc, self._assets())
        return self._save_plan(plan, st, how=how)["id"]

    # ---------------- cards
    def look_card(self, st: dict, mode: str | None = None) -> dict | None:
        rec = self.plan_rec()
        if rec is None:
            return None
        return {"summary": _cp().plan_summary(rec["plan"]), "plan_id": rec["id"], "preset": rec["plan"].get("preset"),
                "mode": mode or "composed", "label": LOOK_LABEL, "editable": LOOK_EDITABLE,
                "source": (rec["plan"].get("meta") or {}).get("source")}

    def est_tool(self, name: str, st: dict | None = None) -> float | None:
        """Dynamic estimate of a composition tool for the cost cap check (None = use the static EST table)."""
        try:
            if name == "plan_composition":
                return PLAN_USD
            rec = self.plan_rec()
            miss = self._missing_plates(rec["plan"]) if rec else TYPICAL_PLATES
            if name == "backdrops":
                return PLATE_USD * max(1, miss)
            if name == "preview_composition":
                n = len(bg_ranges(rec["plan"])) if rec else TYPICAL_STILLS
                return est_preview(n, miss)[0] - PLAN_USD
            if name == "render_composition":
                wc = self.working_concept(st)
                return est_edit(_cp().total_duration(wc) + HOLD_S, miss)[0]
        except Exception:
            return None
        return None

    def next_cost(self, step: str, st: dict) -> tuple[float | None, list[dict] | None]:
        """(usd, itemised) cost of the step AFTER `step` in the composed look; (None, None) = use NEXT_COST."""
        if self.look() != "composed":
            return None, None
        try:
            if step == "ideas":
                cs = self.concepts_list()
                n = max([len(c.get("beats") or []) for c in cs] or [TYPICAL_STILLS])
                return est_preview(min(max(n, 1), 3))
            if step == "preview":
                wc = self.working_concept(st)
                rec = self.plan_rec()
                miss = self._missing_plates(rec["plan"]) if rec else 0
                return est_edit(_cp().total_duration(wc) + HOLD_S, miss)
        except Exception:
            return None, None
        return None, None


def _clip_path(clip: dict) -> Path:
    from .. import cuts
    return cuts.clip_path(clip)


def _copy(src, dst) -> None:
    import shutil
    if Path(src).resolve() != Path(dst).resolve():
        shutil.copy2(src, dst)

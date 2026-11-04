"""Composition planner: the AI decides, deterministic code renders.

Muse Spark writes a composition PLAN (schemas/composition_plan.schema.json) for one approved concept:
the speaker cut out over changing backgrounds, stylised text in named regions, an optional PiP card.
Code then lints the plan against the concept, the client hard rules and the assets, repairs what is
mechanical, retries the model with the remaining errors, and falls back to a safe deterministic
default_plan so a run never blocks. The plan is shown to the human; the renderer only executes it.

Public API:
    plan_composition(run, concept, brief, memory_text, assets_info, *, guidance="", look=None) -> plan
    lint_plan(plan, concept, hard_rules, *, assets_info=None) -> list[problem]
    repair_plan(plan, problems, concept=None, *, assets_info=None) -> plan
    default_plan(concept, brand_colours=None, *, look=None) -> plan
    backdrop_requests(plan) -> [{name, prompt}]
    plan_summary(plan) -> [str]
A problem is {"code", "level": "error"|"warn", "path", "msg", "fixable"}.
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

from jsonschema import Draft202012Validator

from . import llm

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "schemas/composition_plan.schema.json").read_text())
_VALIDATOR = Draft202012Validator(SCHEMA)

PRESETS = {
    "cutout_on_brand_colour": "Speaker cut out over a brand-colour gradient. The safest look; no extra assets.",
    "cutout_on_textile_plate": "Speaker cut out over a generated flat textile/pattern plate (dimmed, tinted to the palette), "
                               "optionally dissolving from a gradient. Needs Muse Image (paid, human-approved).",
    "cutout_over_blurred_scene": "Speaker cut out over a heavily blurred, slowly zooming person-free still from the assets.",
    "split_with_pip_card": "Speaker cut out over a gradient or plate, plus one PiP image card on left_mid/right_mid.",
}
TEXT_STYLES = {
    "pill_headline": "big headline, optional small label pill above it; region top_center; the hook",
    "callout_tag": "short rotated tag with a plate (2-4 words) in left_mid or right_mid",
    "cta_pill": "end-card call to action on a pill; region cta_top; last beat only",
}
# max characters per region (blocks wrap to several lines; beyond this the font would be unreadably small)
REGION_CAP = {"top_center": 44, "left_mid": 24, "right_mid": 24, "bottom_caption": 40, "cta_top": 70}
MIN_BLOCK_S = 0.8
MAX_PLATE_DIM = 0.7
MIN_SCRIM = 0.35

_PRICE = re.compile(r"(₹|\brs\.?\s*\d|\brupees?\b|\binr\b|[$€£]|\d\s*%|%\s*off|\bdiscounts?\b|\bsale\b|\bprice[sd]?\b|"
                    r"\bcheap(er|est)?\b|\bfree shipping\b|\bcoupon\b|\bcod\b|\bmrp\b|\boffer\b|\bdeal\b)", re.I)
_URGENCY = re.compile(r"(\bhurry\b|\blimited (stock|time|period)\b|\blast chance\b|\bonly today\b|\bnow or never\b|"
                      r"\bdon'?t miss\b|\bselling fast\b|\bwhile stocks? last\b|\bjaldi\b|\bfinal (hours|call)\b|\bending soon\b)", re.I)
_PEOPLE = re.compile(r"\b(person|people|woman|women|man|men|girl|boy|lady|model|face|faces|portrait|hands?|child|"
                     r"human|figure|crowd|smiling|body)\b", re.I)
_PLATE_TEXTY = re.compile(r"\b(text|logo|letters?|words?|caption|watermark|typography)\b", re.I)


# ---------------------------------------------------------------- small helpers
def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lum(h: str) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _hex_rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _darken(h: str, f: float) -> str:
    r, g, b = _hex_rgb(h)
    return "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))


def _is_hex(x) -> bool:
    return isinstance(x, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", x) is not None


def _num(x, default=0.0) -> float:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else default


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _p(code, path, msg, fixable=True, level="error") -> dict:
    return {"code": code, "level": level, "path": path, "msg": msg, "fixable": fixable}


def beat_windows(concept: dict) -> list[tuple[float, float]]:
    """Output-timeline (start, end) of each beat: beats are played back to back."""
    t, out = 0.0, []
    for b in concept.get("beats") or []:
        d = max(0.0, _num(b.get("src_out")) - _num(b.get("src_in")))
        out.append((round(t, 3), round(t + d, 3)))
        t += d
    return out


def total_duration(concept: dict) -> float:
    w = beat_windows(concept)
    return round(w[-1][1], 2) if w else 0.0


def _hook_text(concept: dict) -> str:
    beats = concept.get("beats") or []
    return ((beats[0].get("on_screen_text") if beats else "") or "").strip()


def _cta_text(concept: dict) -> str:
    return (concept.get("cta") or "").strip()


def _concept_texts(concept: dict) -> str:
    parts = [concept.get("title", ""), concept.get("hook", ""), concept.get("cta", "")]
    parts += [b.get("on_screen_text", "") for b in concept.get("beats") or []]
    return " ".join(p for p in parts if p)


def hard_rules_from_memory(memory_text: str) -> list[str]:
    """Hard rules from a memory block (lines like '- [hard_rule] text' or 'HARD RULE: text')."""
    rules = []
    for line in (memory_text or "").splitlines():
        m = re.match(r"\s*[-*]?\s*(?:\[hard_rule\]|hard[_ ]rule\s*[:|-])\s*(.+)", line, re.I)
        if m:
            rules.append(m.group(1).strip())
    return rules


def _asset_names(assets_info: dict | None, key: str, *, people_ok: bool) -> set[str]:
    out = set()
    for a in (assets_info or {}).get(key, []) or []:
        if isinstance(a, dict) and a.get("name") and (people_ok or not a.get("has_people")):
            out.add(a["name"])
    return out


def _bg_colors(bg: dict) -> list[str]:
    cols = [c for c in (bg.get("colors") or []) if _is_hex(c)] or ["#2a1020"]
    dim = _clamp(_num(bg.get("dim"), 1.0), 0.2, 1.0)
    return [_darken(c, dim) for c in cols]


# ---------------------------------------------------------------- lint
def _schema_problems(plan: dict) -> list[dict]:
    out = []
    for e in sorted(_VALIDATOR.iter_errors(plan), key=lambda e: list(map(str, e.absolute_path))):
        path = "/".join(str(p) for p in e.absolute_path) or "(plan)"
        out.append(_p("schema", path, e.message[:200], fixable=False))
    return out


def lint_plan(plan: dict, concept: dict, hard_rules: list[str] | None = None, *,
              assets_info: dict | None = None) -> list[dict]:
    """All problems with the plan. Errors block; warnings are advisory. Never raises."""
    hard_rules = hard_rules or []
    probs = _schema_problems(plan) if isinstance(plan, dict) else [_p("schema", "(plan)", "plan is not an object", False)]
    if not isinstance(plan, dict):
        return probs
    try:
        probs += _lint_semantics(plan, concept, hard_rules, assets_info)
    except Exception as e:  # malformed beyond what the schema pass names; report, do not crash
        probs.append(_p("malformed", "(plan)", f"could not lint: {type(e).__name__}: {e}", False))
    return probs


def _lint_semantics(plan, concept, hard_rules, assets_info) -> list[dict]:
    probs: list[dict] = []
    dur = total_duration(concept)
    wins = beat_windows(concept)
    beats = concept.get("beats") or []
    allowed_stills = _asset_names(assets_info, "backdrops", people_ok=False)
    known_pips = _asset_names(assets_info, "pips", people_ok=True)
    concept_clips = {b.get("clip") for b in beats}
    allowed_price = _concept_texts(concept)

    # duration + subject segments mirror the concept beats
    if dur and abs(_num(plan.get("duration")) - dur) > 0.05:
        probs.append(_p("duration", "duration", f"duration {plan.get('duration')} != sum of beats {dur}"))
    segs = (plan.get("subject") or {}).get("segments") or []
    want = [(i, b.get("clip"), round(_num(b.get("src_in")), 3), round(_num(b.get("src_out")), 3)) for i, b in enumerate(beats)]
    got = [(s.get("beat"), s.get("clip"), round(_num(s.get("in")), 3), round(_num(s.get("out")), 3)) for s in segs]
    if got != want:
        probs.append(_p("segments", "subject/segments", "subject segments must equal the concept beats (clip, in, out) in order"))

    # backgrounds
    bgs = plan.get("background") or []
    for i, bg in enumerate(bgs):
        path = f"background/{i}"
        kind = bg.get("kind")
        if kind in ("image", "blur_still"):
            src = bg.get("src")
            if src not in allowed_stills:
                why = "contains people or is unknown" if src else "has no src"
                probs.append(_p("backdrop", path, f"{kind} src {src!r} {why}; allowed person-free stills: {sorted(allowed_stills)}"))
        if kind == "generated":
            pr = bg.get("prompt") or ""
            if _PEOPLE.search(pr) or _PLATE_TEXTY.search(pr):
                probs.append(_p("plate_prompt", path, "plate prompt must describe a flat textile/pattern with no people and no text"))
        if kind in ("image", "generated") and _num(bg.get("dim"), 1) > MAX_PLATE_DIM + 1e-9:
            probs.append(_p("plate_dim", path, f"plates must be dimmed (dim <= {MAX_PLATE_DIM}) so text and the speaker separate"))
        if _num(bg.get("start")) > max(dur - 0.5, 0):
            probs.append(_p("timing", path, "background starts too late"))
    if bgs and _num(bgs[0].get("start")) != 0:
        probs.append(_p("timing", "background/0/start", "first background must start at 0"))
    starts = [_num(b.get("start")) for b in bgs]
    if starts != sorted(starts):
        probs.append(_p("timing", "background", "backgrounds must be in time order"))

    # preset consistency
    kinds = {b.get("kind") for b in bgs}
    need = {"cutout_on_brand_colour": lambda: kinds <= {"gradient"},
            "cutout_on_textile_plate": lambda: bool(kinds & {"generated", "image"}),
            "cutout_over_blurred_scene": lambda: "blur_still" in kinds,
            "split_with_pip_card": lambda: bool(plan.get("overlays"))}
    pre = plan.get("preset")
    if pre in need and not need[pre]():
        probs.append(_p("preset", "preset", f"preset {pre} does not match the backgrounds/overlays present"))

    # text blocks: timing, region fit, price/urgency, contrast
    texts = plan.get("text") or []
    worst_bg = min((c for b in bgs for c in _bg_colors(b)), key=_lum, default="#2a1020")
    bg_lums = [_lum(c) for b in bgs for c in _bg_colors(b)] or [_lum("#2a1020")]
    for i, t in enumerate(texts):
        path = f"text/{i}"
        s, e = _num(t.get("start")), _num(t.get("end"))
        lo, hi = 0.0, dur
        if isinstance(t.get("beat"), int) and 0 <= t["beat"] < len(wins):
            lo, hi = wins[t["beat"]]
        if s < lo - 1e-6 or e > hi + 1e-6 or e - s < MIN_BLOCK_S - 1e-6:
            probs.append(_p("timing", path, f"{t.get('id')}: {s}-{e}s must lie within {lo}-{hi}s and last >= {MIN_BLOCK_S}s"))
        cap = REGION_CAP.get(t.get("region"), 40)
        if len(t.get("text") or "") > cap:
            probs.append(_p("text_len", path, f"{t.get('id')}: {len(t['text'])} chars exceed {cap} for region {t.get('region')}"))
        for field in ("text", "pill_text"):
            val = t.get(field) or ""
            m = _PRICE.search(val)
            if m and m.group(0).lower() not in allowed_price.lower():
                probs.append(_p("price", path, f"{t.get('id')}.{field}: {m.group(0)!r} looks like a price/discount claim the brief did not give"))
            if _URGENCY.search(val) and not _URGENCY.search(allowed_price):
                probs.append(_p("urgency", path, f"{t.get('id')}.{field}: urgency wording is banned by the client rules"))
        if t.get("role") == "hook" and _hook_text(concept) and t.get("text") != _hook_text(concept):
            probs.append(_p("locked", path, "hook text must equal the concept's on-screen hook (human edits win)"))
        if t.get("role") == "cta" and _cta_text(concept) and t.get("text") != _cta_text(concept):
            probs.append(_p("locked", path, "CTA text must equal the concept's cta (human edits win)"))
        if t.get("role") == "title" and concept.get("title") and t.get("text") != concept["title"]:
            probs.append(_p("locked", path, "title text must equal the concept title (human edits win)"))
        fill, plate = t.get("fill"), t.get("plate")
        stroke = t.get("stroke")
        if _is_hex(fill):
            ref = plate if _is_hex(plate) else None
            if ref:
                ok = contrast(fill, ref) >= 4.5
            else:
                ok = all(contrast(fill, c) >= 4.5 for c in {worst_bg, *(c for b in bgs for c in _bg_colors(b))}) or \
                     (isinstance(stroke, list) and len(stroke) == 2 and _is_hex(stroke[1]) and stroke[0] >= 4
                      and contrast(fill, stroke[1]) >= 4.5)
            if not ok:
                probs.append(_p("contrast", path, f"{t.get('id')}: fill {fill} has contrast < 4.5 against its plate/background"))
    if _cta_text(concept) and not any(t.get("role") == "cta" for t in texts):
        probs.append(_p("locked", "text", "concept has a cta but the plan shows no cta block"))
    if _hook_text(concept) and not any(t.get("role") == "hook" for t in texts):
        probs.append(_p("locked", "text", "concept has an on-screen hook but the plan shows no hook block"))

    # same region overlapping in time; at most 2 text blocks (captions included) at once
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = texts[i], texts[j]
            if a.get("region") == b.get("region") and _num(a.get("start")) < _num(b.get("end")) and _num(b.get("start")) < _num(a.get("end")):
                probs.append(_p("overlap", f"text/{j}", f"{a.get('id')} and {b.get('id')} share region {a.get('region')} at the same time"))
    mx, at = _max_visible(plan)
    if mx > 2:
        probs.append(_p("too_much_text", "text", f"{mx} text blocks visible at once around {at:.1f}s (max 2, captions count)"))

    # captions
    cap = plan.get("captions") or {}
    if _num(cap.get("scrim")) < MIN_SCRIM - 1e-9:
        probs.append(_p("scrim", "captions/scrim", f"captions need a scrim >= {MIN_SCRIM} over saree/hands"))
    for i, c in enumerate(cap.get("chunks") or []):
        if _num(c.get("start")) < 0 or _num(c.get("end")) > dur + 1e-6 or _num(c.get("end")) <= _num(c.get("start")):
            probs.append(_p("timing", f"captions/chunks/{i}", "caption chunk outside 0..duration or empty"))
        m = _PRICE.search(c.get("text") or "")
        if m and m.group(0).lower() not in allowed_price.lower():
            probs.append(_p("price", f"captions/chunks/{i}", f"caption {m.group(0)!r} looks like a price/discount claim"))
    if cap.get("source") == "asr" and cap.get("chunks"):
        probs.append(_p("captions_source", "captions", "source asr must have no chunks (the pipeline fills them)"))

    # overlays
    for i, o in enumerate(plan.get("overlays") or []):
        src = o.get("src") or ""
        path = f"overlays/{i}"
        if src.startswith("asset:"):
            if src[6:] not in known_pips:
                probs.append(_p("overlay_src", path, f"unknown asset {src!r}; known: {sorted(known_pips)}"))
        elif src.startswith("frame:"):
            if src[6:].rsplit("@", 1)[0] not in concept_clips:
                probs.append(_p("overlay_src", path, f"frame source {src!r} is not a clip of this concept"))
        s, e = _num(o.get("start")), _num(o.get("end"))
        if s < 0 or e > dur + 1e-6 or e - s < 1.5:
            probs.append(_p("timing", path, f"overlay {s}-{e}s must lie within 0-{dur}s and last >= 1.5s"))
    return probs


def _max_visible(plan: dict) -> tuple[int, float]:
    spans = [(_num(t.get("start")), _num(t.get("end"))) for t in plan.get("text") or []]
    cap = plan.get("captions") or {}
    chunks = [(_num(c.get("start")), _num(c.get("end"))) for c in cap.get("chunks") or []]
    # source "asr" captions are speech-timed over the whole reel: count them as one block for the whole duration
    if cap.get("source") == "asr":
        spans.append((0.0, _num(plan.get("duration"), 1e9)))
    else:
        spans += chunks
    pts = sorted({s for s, _ in spans})
    best, at = 0, 0.0
    for p in pts:
        n = sum(1 for s, e in spans if s <= p + 1e-9 < e)
        if n > best:
            best, at = n, p
    return best, at


# ---------------------------------------------------------------- repair
def _short(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    words, out = text.split(), ""
    for w in words:
        nxt = (out + " " + w).strip()
        if len(nxt) > cap:
            break
        out = nxt
    out = (out or text[:cap]).rstrip(" ,;:-")
    return out


def _coerce(plan: dict) -> dict:
    """Clamp numeric ranges / drop unknown keys so mechanical errors do not cost a model retry."""
    top = set(SCHEMA["properties"])
    for k in [k for k in plan if k not in top]:
        del plan[k]
    plan["version"] = 1
    plan["size"] = [1080, 1920]
    if plan.get("fps") not in (24, 25, 30):
        plan["fps"] = 30
    plan.setdefault("audio", {"source": "subject", "music": None})
    plan.setdefault("overlays", [])
    plan.setdefault("text", [])
    for bg in plan.get("background") or []:
        if not isinstance(bg, dict):
            continue
        bg["blur"] = _clamp(_num(bg.get("blur")), 0, 40)
        bg["zoom"] = _clamp(_num(bg.get("zoom"), 1.0), 1.0, 1.3)
        bg["dim"] = _clamp(_num(bg.get("dim"), 1.0), 0.2, 1.0)
        bg["start"] = max(0.0, _num(bg.get("start")))
        bg.setdefault("colors", ["#2a1020"])
        bg["colors"] = [c for c in bg["colors"] if _is_hex(c)][:3] or ["#2a1020"]
        bg.setdefault("src", None)
        bg.setdefault("prompt", None)
        ti = bg.get("transition_in")
        if not isinstance(ti, dict) or ti.get("type") not in ("cut", "fade", "slide_left", "slide_right"):
            bg["transition_in"] = {"type": "fade", "dur": 0.6}
        else:
            ti["dur"] = _clamp(_num(ti.get("dur"), 0.6), 0, 1.5)
    for t in plan.get("text") or []:
        if isinstance(t, dict):
            t["size"] = int(_clamp(_num(t.get("size"), 90), 40, 160))
            t["rot"] = _clamp(_num(t.get("rot")), -12, 12)
            t.setdefault("pill_text", None)
            t.setdefault("plate", None)
            t.setdefault("stroke", None)
            if isinstance(t.get("pill_text"), str):
                t["pill_text"] = t["pill_text"][:18] or None
    cap = plan.get("captions")
    if isinstance(cap, dict):
        cap["scrim"] = _clamp(_num(cap.get("scrim"), 0.5), 0, 0.85)
        cap.setdefault("source", "asr")
        cap.setdefault("chunks", [])
        cap.setdefault("stroke", None)
    sh = (plan.get("subject") or {})
    if isinstance(sh, dict):
        sh["shadow"] = _clamp(_num(sh.get("shadow"), 0.5), 0, 1)
    return plan


def _infer_preset(plan: dict) -> str:
    kinds = {b.get("kind") for b in plan.get("background") or []}
    if plan.get("overlays"):
        return "split_with_pip_card"
    if "blur_still" in kinds:
        return "cutout_over_blurred_scene"
    if kinds & {"generated", "image"}:
        return "cutout_on_textile_plate"
    return "cutout_on_brand_colour"


def _fallback_bg(plan: dict, bg: dict) -> None:
    """Turn a disallowed still/plate into a gradient from its own palette."""
    cols = [c for c in (bg.get("colors") or []) if _is_hex(c)] or ["#3b0a2a", "#7a1d3c"]
    bg.update(kind="gradient", src=None, prompt=None, blur=0, zoom=1.0, dim=1.0, colors=cols)


def repair_plan(plan: dict, problems: list[dict] | None = None, concept: dict | None = None, *,
                assets_info: dict | None = None) -> dict:
    """Fix everything mechanical in a copy of the plan. `problems` (from lint_plan) is advisory: the repair is
    idempotent and always re-derives from the concept, so passing [] or None is fine."""
    plan = _coerce(copy.deepcopy(plan if isinstance(plan, dict) else {}))
    concept = concept or {}
    beats = concept.get("beats") or []
    wins = beat_windows(concept)
    dur = total_duration(concept) or _num(plan.get("duration"), 15.0)
    fixed: list[str] = []

    def note(msg):
        fixed.append(msg)

    # duration + subject segments come straight from the concept
    if abs(_num(plan.get("duration")) - dur) > 0.05:
        note(f"duration -> {dur}")
    plan["duration"] = dur
    sub = plan.get("subject") if isinstance(plan.get("subject"), dict) else {}
    want = [{"beat": i, "clip": b.get("clip"), "in": round(_num(b.get("src_in")), 3), "out": round(_num(b.get("src_out")), 3)}
            for i, b in enumerate(beats)]
    if want and sub.get("segments") != want:
        note("subject segments re-derived from concept beats")
        sub["segments"] = want
    sub.setdefault("mask", {"prompt": "woman", "feather": 2.0, "edge": [0.35, 0.65]})
    sub.setdefault("transform", {"scale": 1.0, "x": 0, "y": 0})
    sub.setdefault("shadow", 0.5)
    plan["subject"] = sub

    # backgrounds
    stills = _asset_names(assets_info, "backdrops", people_ok=False)
    bgs = [b for b in (plan.get("background") or []) if isinstance(b, dict)]
    for i, bg in enumerate(bgs):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", str(bg.get("id") or "")):
            bg["id"] = f"bg{i + 1}"
        if bg.get("kind") in ("image", "blur_still") and bg.get("src") not in stills:
            note(f"{bg['id']}: disallowed still {bg.get('src')!r} replaced")
            if stills:
                bg["src"] = sorted(stills)[0]
            else:
                _fallback_bg(plan, bg)
        if bg.get("kind") == "generated":
            pr = bg.get("prompt") or ""
            if len(pr.strip()) < 3 or _PEOPLE.search(pr) or _PLATE_TEXTY.search(pr):
                note(f"{bg['id']}: plate prompt replaced with a safe textile description")
                bg["prompt"] = "handloom woven textile weave pattern"
        if bg.get("kind") in ("image", "generated") and bg["dim"] > MAX_PLATE_DIM:
            note(f"{bg['id']}: dim {bg['dim']} -> 0.6")
            bg["dim"] = 0.6
        if bg.get("kind") == "gradient":
            bg["src"], bg["prompt"] = None, None
            if not bg.get("colors"):
                bg["colors"] = ["#3b0a2a", "#7a1d3c"]
            if len(bg["colors"]) == 1:
                bg["colors"] = [bg["colors"][0], _darken(bg["colors"][0], 0.55)]
        bg["start"] = round(_clamp(_num(bg.get("start")), 0, max(dur - 1.0, 0)), 2)
    if not bgs:
        bgs = [{"id": "bg1", "kind": "gradient", "colors": ["#3b0a2a", "#7a1d3c"], "src": None, "prompt": None, "blur": 0,
                "zoom": 1.05, "dim": 1.0, "start": 0, "transition_in": {"type": "cut", "dur": 0}}]
        note("no background -> default gradient")
    bgs.sort(key=lambda b: b["start"])
    if bgs[0]["start"] != 0:
        bgs[0]["start"] = 0
    bgs = bgs[:4]
    seen = set()
    for i, bg in enumerate(bgs):
        if bg["id"] in seen:
            bg["id"] = f"bg{i + 1}x"
        seen.add(bg["id"])
    plan["background"] = bgs

    # overlays
    known_pips = _asset_names(assets_info, "pips", people_ok=True)
    clips = {b.get("clip") for b in beats}
    ovs = []
    for o in plan.get("overlays") or []:
        if not isinstance(o, dict):
            continue
        src = o.get("src") or ""
        ok = (src.startswith("asset:") and src[6:] in known_pips) or \
             (src.startswith("frame:") and src[6:].rsplit("@", 1)[0] in clips)
        if not ok:
            note(f"overlay {o.get('id')} dropped: unusable src {src!r}")
            continue
        o["start"] = round(_clamp(_num(o.get("start")), 0, max(dur - 1.5, 0)), 2)
        o["end"] = round(_clamp(_num(o.get("end")), o["start"] + 1.5, dur), 2)
        if o["end"] - o["start"] < 1.5:
            note(f"overlay {o.get('id')} dropped: too short")
            continue
        o["rot"] = _clamp(_num(o.get("rot")), -12, 12)
        ovs.append(o)
    plan["overlays"] = ovs[:1]

    # text blocks
    def role_pri(t):
        return {"hook": 0, "cta": 0, "title": 1, "callout": 2}.get(t.get("role"), 3)

    texts = [t for t in plan.get("text") or [] if isinstance(t, dict)]
    hook, cta = _hook_text(concept), _cta_text(concept)
    for i, t in enumerate(texts):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", str(t.get("id") or "")):
            t["id"] = f"t{i + 1}"
        if t.get("role") == "hook" and hook and t.get("text") != hook:
            t["text"] = hook
            note(f"{t['id']}: hook restored from concept")
        if t.get("role") == "cta" and cta and t.get("text") != cta:
            t["text"] = cta
            note(f"{t['id']}: cta restored from concept")
        if t.get("role") == "title" and concept.get("title") and t.get("text") != concept["title"]:
            t["text"] = concept["title"]
        t["text"] = str(t.get("text") or "")
        if t.get("role") == "hook" and "beat" not in t and wins:
            t["beat"] = 0
        if t.get("role") == "cta" and "beat" not in t and wins:
            t["beat"] = len(wins) - 1
        if t.get("role") == "cta":
            t["region"], t["style"] = "cta_top", "cta_pill"
        lo, hi = 0.0, dur
        if isinstance(t.get("beat"), int) and 0 <= t["beat"] < len(wins):
            lo, hi = wins[t["beat"]]
        s = _clamp(_num(t.get("start")), lo, max(hi - MIN_BLOCK_S, lo))
        e = _clamp(_num(t.get("end")), s + MIN_BLOCK_S, hi)
        if (round(s, 2), round(e, 2)) != (round(_num(t.get("start")), 2), round(_num(t.get("end")), 2)):
            note(f"{t['id']}: timing clamped to {round(s, 2)}-{round(e, 2)}")
        t["start"], t["end"] = round(s, 2), round(e, 2)
        # length vs region
        locked = t.get("role") in ("hook", "cta", "title")
        cap = REGION_CAP.get(t.get("region"), 40)
        if len(t["text"]) > cap:
            if locked:
                big = "cta_top" if t.get("role") == "cta" else "top_center"
                if len(t["text"]) <= REGION_CAP[big] and t["region"] != big:
                    t["region"] = big
                    note(f"{t['id']}: moved to {big} to fit locked text")
                cap = REGION_CAP[t["region"]]
                if len(t["text"]) > cap:
                    t["size"] = int(max(40, t["size"] * cap / len(t["text"])))
                    note(f"{t['id']}: locked text kept, size reduced to {t['size']}")
            else:
                t["text"] = _short(t["text"], cap)
                note(f"{t['id']}: text shortened to fit {t['region']}")
        # claims: drop non-locked blocks with price/urgency wording
        allowed = _concept_texts(concept).lower()
        for field in ("text", "pill_text"):
            val = t.get(field) or ""
            m = _PRICE.search(val)
            bad = (m and m.group(0).lower() not in allowed) or (_URGENCY.search(val) and not _URGENCY.search(allowed))
            if bad:
                if field == "pill_text":
                    t["pill_text"] = None
                else:
                    t["_drop"] = True
                note(f"{t['id']}.{field}: price/urgency wording removed")
        # contrast
        bgcols = [c for b in plan["background"] for c in _bg_colors(b)]
        fill, plate, stroke = t.get("fill"), t.get("plate"), t.get("stroke")
        if not _is_hex(fill):
            t["fill"] = fill = "#ffffff"
        if plate is not None and not _is_hex(plate):
            t["plate"] = plate = None
        stroke_ok = isinstance(stroke, list) and len(stroke) == 2 and _is_hex(stroke[1]) and stroke[0] >= 4 \
            and contrast(fill, stroke[1]) >= 4.5
        ref = [plate] if plate else bgcols
        if not all(contrast(fill, c) >= 4.5 for c in ref) and not (stroke_ok and not plate):
            best = max(("#ffffff", "#111111"), key=lambda c: min(contrast(c, r) for r in ref))
            note(f"{t['id']}: fill {fill} -> {best} for contrast")
            t["fill"] = best
    texts = [t for t in texts if not t.pop("_drop", False) or t.get("role") in ("hook", "cta", "title")]
    for t in texts:
        t.pop("_drop", None)

    # same-region overlap: later block waits for the earlier one (or is dropped if no room)
    texts.sort(key=lambda t: (t["start"], role_pri(t)))
    for i, b in enumerate(texts):
        for a in texts[:i]:
            if a.get("region") == b.get("region") and b["start"] < a["end"] and a["start"] < b["end"] and b in texts:
                if role_pri(b) < role_pri(a) and a.get("role") not in ("hook", "cta"):
                    a["end"] = round(max(a["start"], b["start"]), 2)
                else:
                    b["start"] = round(a["end"], 2)
                note(f"{b['id']}/{a['id']}: region clash resolved by timing")
    texts = [t for t in texts if t["end"] - t["start"] >= MIN_BLOCK_S - 1e-6 or t.get("role") in ("hook", "cta", "title")]

    # at most 2 visible at once (captions count): trim/drop lowest-priority blocks
    plan["text"] = texts
    for _ in range(30):
        mx, at = _max_visible(plan)
        if mx <= 2:
            break
        cap = plan.get("captions") or {}
        active = [t for t in plan["text"] if t["start"] <= at + 1e-9 < t["end"]]
        cand = [t for t in active if t.get("role") not in ("hook", "cta")] or active
        victim = max(cand, key=lambda t: (role_pri(t), t["start"]))
        if victim["start"] >= at - 1e-9 or victim["end"] - at < 0:
            plan["text"].remove(victim)
            note(f"{victim['id']}: dropped (more than 2 text blocks at {at:.1f}s)")
        else:
            victim["end"] = round(at, 2)
            note(f"{victim['id']}: trimmed to {at:.1f}s (max 2 text blocks)")
        plan["text"] = [t for t in plan["text"] if t["end"] - t["start"] >= MIN_BLOCK_S - 1e-6 or t.get("role") in ("hook", "cta", "title")]

    # missing locked blocks
    have = {t.get("role") for t in plan["text"]}
    if hook and "hook" not in have and wins:
        plan["text"].append(_hook_block(concept, wins, plan))
        note("hook block added from concept")
    if cta and "cta" not in have and wins:
        plan["text"].append(_cta_block(concept, wins, plan))
        note("cta block added from concept")

    # captions
    cap = plan.get("captions") if isinstance(plan.get("captions"), dict) else {}
    cap.setdefault("style", "pop_captions")
    cap.setdefault("source", "asr")
    cap.setdefault("fill", "#ffffff")
    cap.setdefault("stroke", [6, "#2a0a1a"])
    cap["style"] = "pop_captions"
    if _num(cap.get("scrim")) < MIN_SCRIM:
        cap["scrim"] = 0.5
        note("captions scrim raised to 0.5")
    chunks = []
    for c in cap.get("chunks") or []:
        if not isinstance(c, dict):
            continue
        s = _clamp(_num(c.get("start")), 0, dur)
        e = _clamp(_num(c.get("end")), 0, dur)
        txt = str(c.get("text") or "")
        m = _PRICE.search(txt)
        if e - s > 0.05 and txt and not (m and m.group(0).lower() not in _concept_texts(concept).lower()):
            chunks.append({"text": txt[:60], "start": round(s, 2), "end": round(e, 2)})
    cap["chunks"] = [] if cap["source"] == "asr" else chunks
    plan["captions"] = cap
    plan["text"].sort(key=lambda t: t["start"])

    # preset must match content
    inferred = _infer_preset(plan)
    need_ok = {"cutout_on_brand_colour": inferred == "cutout_on_brand_colour",
               "cutout_on_textile_plate": inferred in ("cutout_on_textile_plate", "split_with_pip_card") and
               any(b["kind"] in ("generated", "image") for b in plan["background"]),
               "cutout_over_blurred_scene": any(b["kind"] == "blur_still" for b in plan["background"]),
               "split_with_pip_card": bool(plan["overlays"])}
    if not need_ok.get(plan.get("preset"), False):
        note(f"preset {plan.get('preset')} -> {inferred}")
        plan["preset"] = inferred
    plan["meta"] = dict(plan.get("meta") or {}, repaired=fixed)
    return plan


# ---------------------------------------------------------------- default plan
DEFAULT_COLOURS = ["#5a1230", "#a3321f"]


def _palette(brand_colours) -> list[str]:
    cols = [c for c in (brand_colours or []) if _is_hex(c)]
    return cols or list(DEFAULT_COLOURS)


def _hook_block(concept, wins, plan=None) -> dict:
    s, e = wins[0]
    return {"id": "hook", "role": "hook", "text": _hook_text(concept), "region": "top_center", "font": "latin",
            "size": 96, "style": "pill_headline", "fill": "#ffffff", "stroke": [6, "#2a0a1a"], "plate": None,
            "pill_text": None, "rot": 0, "animation": "pop", "start": round(s + 0.2, 2), "end": round(min(e, s + 3.2), 2), "beat": 0}


def _cta_block(concept, wins, plan=None) -> dict:
    s, e = wins[-1]
    return {"id": "cta", "role": "cta", "text": _cta_text(concept), "region": "cta_top", "font": "latin", "size": 64,
            "style": "cta_pill", "fill": "#111111", "stroke": None, "plate": "#fbbf24", "pill_text": None, "rot": 0,
            "animation": "slide_left", "start": round(max(s, e - 3.0), 2), "end": round(e, 2), "beat": len(wins) - 1}


def default_plan(concept: dict, brand_colours: list[str] | None = None, *, look: str | None = None) -> dict:
    """Safe deterministic plan (always schema-valid and lint-clean for any concept with >= 1 beat >= 3 s)."""
    cols = _palette(brand_colours)
    c1 = _darken(cols[0], 0.7)
    c2 = _darken(cols[1] if len(cols) > 1 else cols[0], 0.5)
    wins = beat_windows(concept)
    dur = total_duration(concept)
    bgs = [{"id": "bg1", "kind": "gradient", "colors": [c1, c2], "src": None, "prompt": None, "blur": 0, "zoom": 1.06,
            "dim": 1.0, "start": 0, "transition_in": {"type": "cut", "dur": 0}}]
    if look == "cutout_on_textile_plate" and dur > 6:
        bgs.append({"id": "plate1", "kind": "generated", "colors": [c1, c2], "src": None,
                    "prompt": "handloom woven textile weave pattern", "blur": 0, "zoom": 1.08, "dim": 0.55,
                    "start": round(dur / 2, 2), "transition_in": {"type": "fade", "dur": 0.6}})
    plan = {
        "version": 1, "preset": "cutout_on_textile_plate" if len(bgs) > 1 else "cutout_on_brand_colour",
        "size": [1080, 1920], "fps": 30, "duration": dur,
        "rationale": "Safe default look: speaker cut out over a brand-colour background, hook headline, closing CTA.",
        "subject": {"segments": [{"beat": i, "clip": b.get("clip"), "in": round(_num(b.get("src_in")), 3),
                                  "out": round(_num(b.get("src_out")), 3)} for i, b in enumerate(concept.get("beats") or [])],
                    "mask": {"prompt": "woman", "feather": 2.0, "edge": [0.35, 0.65]},
                    "transform": {"scale": 1.0, "x": 0, "y": 0}, "shadow": 0.5},
        "background": bgs, "text": [],
        "captions": {"style": "pop_captions", "source": "asr", "chunks": [], "fill": "#ffffff", "highlight": "#fbbf24",
                     "stroke": [6, "#2a0a1a"], "scrim": 0.5},
        "overlays": [], "audio": {"source": "subject", "music": None},
    }
    if wins and _hook_text(concept):
        plan["text"].append(_hook_block(concept, wins))
    if wins and _cta_text(concept):
        plan["text"].append(_cta_block(concept, wins))
    plan["meta"] = {"source": "default"}
    return plan


# ---------------------------------------------------------------- Muse Image requests + UI summary
def backdrop_requests(plan: dict) -> list[dict]:
    """Muse Image requests for the plan's generated plates: [{name, prompt}] (name = background id)."""
    out = []
    for bg in plan.get("background") or []:
        if bg.get("kind") != "generated":
            continue
        desc = (bg.get("prompt") or "handloom woven textile weave pattern").strip().rstrip(".")
        desc = _PEOPLE.sub("", desc)
        desc = _PLATE_TEXTY.sub("", desc)
        desc = re.sub(r"\s{2,}", " ", desc).strip(" ,;") or "handloom woven textile weave pattern"
        cols = ", ".join(bg.get("colors") or [])
        out.append({"name": bg["id"], "prompt": (
            f"{desc}. Flat decorative textile or pattern backdrop, seamless, edge to edge, soft even lighting, "
            f"low contrast and muted so a bright foreground subject stands out, vertical 9:16. "
            f"Tinted to this palette: {cols}. No people, no hands, no faces, no text, no letters, no logos, no borders.")})
    return out


def plan_summary(plan: dict) -> list[str]:
    """Short plain-language bullets for the UI card."""
    out = [f"Look: {plan.get('preset', '?').replace('_', ' ')}"]
    names = {"gradient": "brand-colour gradient", "image": "still", "generated": "generated textile plate",
             "blur_still": "blurred scene"}
    bgs = plan.get("background") or []
    if bgs:
        out.append("Background: " + " then ".join(names.get(b.get("kind"), str(b.get("kind"))) for b in bgs))
    label = {"hook": "Headline", "callout": "Callout", "cta": "CTA", "title": "Title", "other": "Text"}
    for t in plan.get("text") or []:
        out.append(f"{label.get(t.get('role'), 'Text')}: {t.get('text')} ({t.get('start')}-{t.get('end')}s)")
    cap = plan.get("captions") or {}
    if cap:
        out.append("Captions: pop-in, word highlight, dark scrim behind")
    for o in plan.get("overlays") or []:
        out.append(f"Picture card: {o.get('src')} on the {str(o.get('region', '')).replace('_mid', '')} ({o.get('start')}-{o.get('end')}s)")
    return out


# ---------------------------------------------------------------- the model call
SYSTEM = """You are the art director of an ad studio. You write a COMPOSITION PLAN as strict JSON for ONE short vertical reel:
the speaker is cut out of her footage and placed over changing backgrounds, with stylised text in named screen regions and an optional picture card.
You choose; a deterministic renderer executes. You never write pixel maths, only the enum names and numbers the schema allows.
Output ONLY one JSON object. No prose, no markdown fences."""

PROMPT = """CLIENT BRIEF
{brief}

CLIENT MEMORY (hard rules are absolute)
{memory}

CONCEPT (the edit is fixed; every time below is in SOURCE seconds; the OUTPUT timeline is the beats played back to back)
title: {title}
beats (output window in brackets):
{beats}
human-approved on-screen hook (use verbatim, role "hook"): {hook!r}
human-approved CTA (use verbatim, role "cta"): {cta!r}
output duration: {dur}s

AVAILABLE ASSETS
person-free backdrop stills (usable as kind image or blur_still, src = name): {backdrops}
picture-card images (overlay src "asset:<name>"): {pips}
You may also use overlay src "frame:<clip>@<seconds>" (a frame of the speaker footage) for a picture card.
generated plates (kind "generated", needs a short prompt describing a flat decorative textile or pattern; Muse Image makes it later, paid, human-approved).

PRESETS (pick one; preset must match what you use)
{presets}
TEXT STYLES
{styles}
REGIONS and max characters: {regions}

RULES
- background[0].start = 0; later backgrounds dissolve in (transition_in fade 0.4-0.8s) at their start. 1-3 backgrounds. Use plates only if the preset is cutout_on_textile_plate; dim plates to <= 0.6 and tint colors to the brand palette so the speaker separates from them.
- Never use a still that contains a person as a background. Never put a person-describing word or any text in a plate prompt.
- Text: the hook in top_center (pill_headline), the CTA in cta_top (cta_pill) during the last beat, at most one short callout_tag (2-4 words, left_mid/right_mid) from a beat's on_screen_text. At most 2 text blocks visible at once (spoken-word captions count as one: captions.source "asr", chunks [] , the pipeline fills them). Keep all times inside the beat windows; blocks last >= 1.0s.
- No prices, discounts, percentages, urgency or claims not in the concept/brief. pill_text is a neutral label like "STORY" or null.
- Contrast: text fill must have contrast >= 4.5 against its plate (or the dimmed background); captions.scrim >= 0.4.
- Add motion variety: different animations per block, background zoom 1.0-1.15.
{look}{guidance}
Return JSON matching this shape exactly (all keys shown are required):
{shape}"""

SHAPE = {
    "version": 1, "preset": "cutout_on_brand_colour", "size": [1080, 1920], "fps": 30, "duration": 14.1,
    "rationale": "one sentence why this look fits the brief",
    "subject": {"segments": [{"beat": 0, "clip": "A.mov", "in": 1.0, "out": 4.0}],
                "mask": {"prompt": "woman", "feather": 2.0, "edge": [0.35, 0.65]},
                "transform": {"scale": 1.0, "x": 0, "y": 0}, "shadow": 0.5},
    "background": [{"id": "bg1", "kind": "gradient", "colors": ["#3b0a2a", "#c2410c"], "src": None, "prompt": None,
                    "blur": 0, "zoom": 1.08, "dim": 1.0, "start": 0, "transition_in": {"type": "cut", "dur": 0}}],
    "text": [{"id": "hook", "role": "hook", "text": "...", "region": "top_center", "font": "latin", "size": 96,
              "style": "pill_headline", "fill": "#ffffff", "stroke": [6, "#2a0a1a"], "plate": None, "pill_text": "STORY",
              "rot": 0, "animation": "pop", "start": 0.2, "end": 3.2, "beat": 0}],
    "captions": {"style": "pop_captions", "source": "asr", "chunks": [], "fill": "#ffffff", "highlight": "#fbbf24",
                 "stroke": [6, "#2a0a1a"], "scrim": 0.5},
    "overlays": [], "audio": {"source": "subject", "music": None},
}


def _assets_block(assets_info: dict | None, key: str) -> str:
    items = (assets_info or {}).get(key) or []
    if not items:
        return "(none)"
    return "; ".join(f"{a['name']}" + (f" - {a.get('description', '')}" if a.get("description") else "") +
                     (" [CONTAINS PEOPLE, not allowed as background]" if a.get("has_people") else "") for a in items)


def _build_prompt(concept, brief, memory_text, assets_info, guidance, look, errors) -> str:
    wins = beat_windows(concept)
    beats = "\n".join(
        f"  {i} [{w[0]:.1f}-{w[1]:.1f}s] clip={b.get('clip')} src {b.get('src_in')}-{b.get('src_out')} "
        f"| purpose: {b.get('purpose')} | on_screen_text: {b.get('on_screen_text')!r}"
        for i, (b, w) in enumerate(zip(concept.get("beats") or [], wins)))
    look_s = ""
    if look:
        name = look if isinstance(look, str) else (look.get("preset") if isinstance(look, dict) else None)
        look_s = f"- The human asked for the look/preset: {name or json.dumps(look)}. Use it.\n"
    g = f"- HUMAN STEER (follow it): {guidance.strip()}\n" if guidance and guidance.strip() else ""
    shape = json.dumps(SHAPE, ensure_ascii=False)
    p = PROMPT.format(
        brief=brief.strip(), memory=memory_text.strip() or "(none)", title=concept.get("title", ""), beats=beats,
        hook=_hook_text(concept), cta=_cta_text(concept), dur=total_duration(concept),
        backdrops=_assets_block(assets_info, "backdrops"), pips=_assets_block(assets_info, "pips"),
        presets="\n".join(f"- {k}: {v}" for k, v in PRESETS.items()),
        styles="\n".join(f"- {k}: {v}" for k, v in TEXT_STYLES.items()),
        regions=json.dumps(REGION_CAP), look=look_s, guidance=g, shape=shape)
    if errors:
        p += "\n\nYOUR LAST PLAN FAILED THESE CHECKS. Fix every one and return the full corrected JSON:\n" + \
             "\n".join(f"- {e}" for e in errors)
    return p


def _look_preset(look) -> str | None:
    if isinstance(look, str) and look in PRESETS:
        return look
    if isinstance(look, dict) and look.get("preset") in PRESETS:
        return look["preset"]
    return None


def plan_composition(run, concept: dict, brief: str, memory_text: str, assets_info: dict | None, *,
                     guidance: str = "", look=None, retries: int = 2, brand_colours: list[str] | None = None,
                     spark=None) -> dict:
    """Muse Spark writes the plan; code lints + repairs; up to `retries` re-asks with the remaining errors; falls
    back to default_plan so a run never blocks. The returned plan is always schema-valid and lint-clean, with
    plan['meta'] = {source: 'spark'|'default', attempts, repaired, spark_errors, tokens}."""
    spark = spark or llm.spark_json
    rules = hard_rules_from_memory(memory_text)
    want = _look_preset(look)
    errors: list[str] = []
    history: list[str] = []
    tokens = 0
    for attempt in range(1 + retries):
        t0 = time.time()
        prompt = _build_prompt(concept, brief, memory_text, assets_info, guidance, look, errors)
        try:
            raw, usage = spark(SYSTEM, prompt, effort="low", max_out=8000)
            tokens += int((usage or {}).get("tokens") or 0)
        except Exception as e:  # network/empty/parse: counts as a failed attempt
            errors = [f"model call failed: {type(e).__name__}: {str(e)[:160]}"]
            history.append(errors[0])
            _trace(run, "muse-spark", {"attempt": attempt + 1}, {"error": errors[0]}, t0, "error",
                   f"composition plan attempt {attempt + 1} failed: {errors[0]}")
            continue
        raw = raw.get("plan", raw) if isinstance(raw, dict) and isinstance(raw.get("plan"), dict) else raw
        if not isinstance(raw, dict) or not {"preset", "background"} <= set(raw):
            errors = ["output is not a composition plan: needs at least preset and background (return the full plan JSON)"]
            history.append(errors[0])
            _trace(run, "muse-spark", {"attempt": attempt + 1}, {"error": errors[0]}, t0, "error", errors[0])
            continue
        problems_raw = lint_plan(_coerce(copy.deepcopy(raw)) if isinstance(raw, dict) else raw, concept, rules,
                                 assets_info=assets_info) if isinstance(raw, dict) else []
        plan = repair_plan(raw, problems_raw, concept, assets_info=assets_info)
        left = [p for p in lint_plan(plan, concept, rules, assets_info=assets_info) if p["level"] == "error"]
        if want and plan.get("preset") != want and not left:
            left = [_p("look", "preset", f"the human asked for preset {want}; use it and include what it needs", False)]
        pre = [p["msg"] for p in problems_raw if p["level"] == "error"]
        history += pre
        if not left:
            plan["meta"] = dict(plan.get("meta") or {}, source="spark", attempts=attempt + 1, spark_errors=history[-12:],
                                tokens=tokens)
            _trace(run, "muse-spark", {"attempt": attempt + 1, "guidance": guidance}, plan, t0, "ok",
                   f"composition plan {plan['preset']}; attempts={attempt + 1}; repaired={len(plan['meta'].get('repaired', []))}")
            return plan
        errors = [f"{p['path']}: {p['msg']}" for p in left][:12]
        _trace(run, "muse-spark", {"attempt": attempt + 1}, {"errors": errors}, t0, "error",
               f"composition plan attempt {attempt + 1} had {len(left)} unfixable problems, retrying")
    plan = default_plan(concept, brand_colours, look=want)
    plan = repair_plan(plan, [], concept, assets_info=assets_info)
    plan["meta"] = dict(plan.get("meta") or {}, source="default", attempts=1 + retries, spark_errors=history[-12:], tokens=tokens)
    _trace(run, "composition-default", {"reason": errors[:3]}, plan, time.time(), "ok",
           "fell back to the safe default composition plan")
    return plan


def _trace(run, tool, inp, out, t0, status, decision) -> None:
    if run is not None:
        try:
            run.trace(tool, inp, out, started=t0, status=status, decision=decision)
        except Exception:
            pass

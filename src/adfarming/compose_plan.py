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
import statistics
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
MAX_TEXT_CHARS = 80          # hook / cta / title / callout, enforced at edit time too (schema maxLength agrees)
CTA_MAX_WORDS = 4
CTA_MAX_CHARS = 28
CAPTION_WORDS = (3, 6)       # words per caption chunk
CAPTION_SECS = (0.8, 2.0)    # seconds per caption chunk
HEADLINE_TOP_PX = 190        # where a top_center headline starts (below the platform UI zone)
HEADLINE_GAP_PX = 28         # air between the last headline line and the top of the head
HEADLINE_MIN_SIZE = 56
DEFAULT_HEAD_TOP = 0.29      # used only when the agent supplied a head box without top_frac
SUBJECT_LUMA = 0.04          # a dark saree: relative luminance assumed when the matte did not measure it
MIN_BG_CONTRAST = 1.6        # background vs subject luminance contrast ratio
RIM_LUMA = 0.2               # below this background luminance the subject gets a light rim / outer glow
CALLOUT_W = 0.30             # callout box width as a fraction of frame width (side margins)
HEAD_HALF_W = 0.13           # half head width as a fraction of frame width
# words that mark an internal direction (note to the editor), never copy for the screen
_CTA_INTERNAL = re.compile(r"(\s[-\u2013\u2014]\s|\b(card|soft|end|hold|held|direction|readable|outro|overlay|b-?roll|clip|beat)\b)", re.I)
_CTA_STOP = {"if", "you", "your", "you're", "youre", "i", "we", "see", "this", "that", "the", "on", "our", "website", "new",
             "to", "a", "an", "of", "and", "or", "in", "at", "it", "is", "are", "for", "with", "check", "visit", "shop"}

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
    return clip_text(((beats[0].get("on_screen_text") if beats else "") or "").strip())


def cta_issue(text: str) -> str | None:
    """Why `text` may not be shown as the on-screen CTA, or None when it is a clean short CTA."""
    t = (text or "").strip()
    if not t:
        return "empty"
    m = _CTA_INTERNAL.search(t)
    if m:
        return f"looks like an internal direction ({m.group(0).strip()!r})"
    if len(t.split()) > CTA_MAX_WORDS:
        return f"more than {CTA_MAX_WORDS} words"
    if len(t) > CTA_MAX_CHARS:
        return f"longer than {CTA_MAX_CHARS} characters"
    return None


def derive_cta_short(concept: dict) -> str:
    """A short on-screen CTA (<= 4 words) from the concept: 'Shop <Product>' when the long cta names one."""
    cta = concept.get("cta") or ""
    m = re.search(r"\b(?:see|shop|explore|check out|find|try)\s+(?:the|this|our|these|that)?\s*([A-Za-z\u0900-\u097F]+)\s+(?:on|at|online|in|now)\b", cta, re.I)
    pick = m.group(1) if m and m.group(1).lower() not in _CTA_STOP else ""
    if not pick:
        caps = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", cta) if w[:1].isupper() and w.lower() not in _CTA_STOP]
        pick = caps[0] if caps else ""
    pick = pick[:1].upper() + pick[1:]
    out = f"Shop {pick}" if pick else "See it online"
    return out if not cta_issue(out) else "See it online"


def locked_cta(concept: dict) -> str | None:
    """The CTA the human fixed: an explicit concept['cta_short'], else a concept cta that is itself a clean short CTA."""
    explicit = (concept.get("cta_short") or "").strip()
    if explicit:
        return explicit[:MAX_TEXT_CHARS]
    plain = (concept.get("cta") or "").strip()
    if plain and not cta_issue(plain):
        return plain
    return None


def _cta_text(concept: dict) -> str:
    """The VISIBLE cta text (short). Empty when the concept has no call to action at all."""
    if not ((concept.get("cta") or "").strip() or (concept.get("cta_short") or "").strip()):
        return ""
    return locked_cta(concept) or derive_cta_short(concept)


def clip_text(text: str, cap: int = MAX_TEXT_CHARS) -> str:
    """Hard cap for any on-screen text; cuts on a word boundary. Used at edit time by the agent too."""
    t = (text or "").strip()
    return t if len(t) <= cap else _short(t, cap)


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
        if not (isinstance(a, dict) and a.get("name")):
            continue
        if people_ok or (a.get("has_people") is False and not _PEOPLE.search(str(a.get("description") or ""))):
            out.add(a["name"])
    return out


def _bg_colors(bg: dict) -> list[str]:
    cols = [c for c in (bg.get("colors") or []) if _is_hex(c)] or ["#2a1020"]
    dim = _clamp(_num(bg.get("dim"), 1.0), 0.2, 1.0)
    return [_darken(c, dim) for c in cols]



def _bg_luma(bg: dict) -> float:
    cols = _bg_colors(bg)
    return sum(_lum(c) for c in cols) / len(cols)


def _lum_contrast(l1: float, l2: float) -> float:
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _mix_white(h: str, f: float) -> str:
    r, g, b = _hex_rgb(h)
    return "#%02x%02x%02x" % (int(r + (255 - r) * f), int(g + (255 - g) * f), int(b + (255 - b) * f))


def _head(plan: dict) -> dict | None:
    h = (plan.get("meta") or {}).get("head")
    return h if isinstance(h, dict) else None


def _head_top(plan: dict) -> float | None:
    h = _head(plan)
    if not h:
        return None
    t = h.get("top_frac")
    return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 < t < 1 else DEFAULT_HEAD_TOP


def _subject_luma(plan: dict) -> float:
    v = ((plan.get("meta") or {}).get("subject") or {}).get("luma")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else SUBJECT_LUMA


def headline_height_px(text: str, size: int, stroke: int = 8) -> int:
    """Estimated height of a wrapped top_center headline (bold face ~0.56 em per char over a 960 px column)."""
    chars = max(1, len(text or ""))
    lines = max(1, -(-int(chars * 0.56 * size) // 960))
    return int(lines * size * 1.15 + 2 * stroke)


def headline_room_px(plan: dict) -> int | None:
    """Pixels between the headline top and the head top, or None when no head box is known."""
    top = _head_top(plan)
    if top is None:
        return None
    return int(top * 1920) - HEADLINE_TOP_PX - HEADLINE_GAP_PX


def _fit_size(text: str, size: int, room: int, stroke: int = 8) -> int:
    while size > HEADLINE_MIN_SIZE and headline_height_px(text, size, stroke) > room:
        size -= 4
    return max(size, HEADLINE_MIN_SIZE)


def callout_side(plan: dict) -> tuple[str, dict]:
    """Which side margin ('left_mid'|'right_mid') is farther from the head, with the horizontal overlap of each side
    callout box against the head box ({region: overlap_frac}). Without a head box: right_mid, no overlaps."""
    h = _head(plan)
    bb = h.get("bbox_frac") if h else None
    if not (isinstance(bb, (list, tuple)) and len(bb) == 4 and all(isinstance(v, (int, float)) for v in bb)):
        return "right_mid", {}
    cx = (bb[0] + bb[2]) / 2
    hx1, hx2 = cx - HEAD_HALF_W, cx + HEAD_HALF_W
    boxes = {"left_mid": (0.04, 0.04 + CALLOUT_W), "right_mid": (0.96 - CALLOUT_W, 0.96)}
    ov = {r: round(max(0.0, min(b[1], hx2) - max(b[0], hx1)), 3) for r, b in boxes.items()}
    # tie: the side with more free margin
    best = min(ov, key=lambda r: (ov[r], -(bb[0] if r == "left_mid" else 1 - bb[2])))
    return best, ov


def rechunk_captions(chunks: list[dict], words=CAPTION_WORDS, secs=CAPTION_SECS) -> list[dict] | None:
    """Re-cut caption chunks to 3-6 words and 0.8-2.0 s. Word timing is spread over each source chunk by letter count.
    Returns None when the rule cannot be met (the caller then falls back to ASR-timed captions)."""
    lo_w, hi_w = words
    lo_s, hi_s = secs
    toks: list[tuple[str, float, float]] = []
    for c in chunks:
        ws = str(c.get("text") or "").split()
        s, e = _num(c.get("start")), _num(c.get("end"))
        tot = sum(len(w) for w in ws) or 1
        t = s
        for w in ws:
            d = (e - s) * len(w) / tot
            toks.append((w, t, t + d))
            t += d
    if not toks:
        return []
    groups: list[list[tuple[str, float, float]]] = []
    cur: list[tuple[str, float, float]] = []
    for tk in toks:
        if cur and (len(cur) >= hi_w or tk[2] - cur[0][1] > hi_s or (len(cur) >= lo_w and cur[-1][0][-1:] in ".?!")):
            groups.append(cur)
            cur = []
        cur.append(tk)
    if cur:
        groups.append(cur)

    def span(g):
        return g[-1][2] - g[0][1]

    def fits(g):
        return len(g) <= hi_w and span(g) <= hi_s + 1e-6
    i = 0
    while i < len(groups):  # merge too-short groups into a neighbour
        g = groups[i]
        if len(g) < lo_w or span(g) < lo_s - 1e-6:
            if i > 0 and fits(groups[i - 1] + g):
                groups[i - 1] = groups[i - 1] + g
                del groups[i]
                continue
            if i + 1 < len(groups) and fits(g + groups[i + 1]):
                groups[i + 1] = g + groups[i + 1]
                del groups[i]
                continue
            # neither neighbour can absorb it whole: rebalance the two so both are long enough
            for j in (i - 1, i + 1):
                if 0 <= j < len(groups):
                    pair = (groups[j] + g) if j < i else (g + groups[j])
                    cut = len(pair) // 2
                    a1, a2 = pair[:cut], pair[cut:]
                    if all(len(x) >= lo_w and span(x) >= lo_s - 1e-6 and fits(x) for x in (a1, a2)):
                        groups[min(i, j)], groups[max(i, j)] = a1, a2
                        break
        i += 1
    out = []
    for k, g in enumerate(groups):
        s, e = g[0][1], g[-1][2]
        nxt = groups[k + 1][0][1] if k + 1 < len(groups) else 1e9
        if e - s < lo_s - 1e-6:
            e = min(s + lo_s, nxt)  # hold on screen a little longer if the gap allows it
        out.append({"text": " ".join(w for w, _, _ in g), "start": round(s, 2), "end": round(e, 2)})
    bad = [c for c in out if not (lo_w <= len(c["text"].split()) <= hi_w) or not (lo_s - 0.011 <= c["end"] - c["start"] <= hi_s + 0.011)]
    if bad and len(toks) >= lo_w:
        return None
    return out


def _chunk_problems(chunks, words=CAPTION_WORDS, secs=CAPTION_SECS) -> list[tuple[int, str]]:
    out = []
    for i, c in enumerate(chunks or []):
        n = len(str(c.get("text") or "").split())
        d = _num(c.get("end")) - _num(c.get("start"))
        if not words[0] <= n <= words[1]:
            out.append((i, f"{n} words (must be {words[0]}-{words[1]})"))
        elif not secs[0] - 0.011 <= d <= secs[1] + 0.011:
            out.append((i, f"{d:.1f}s on screen (must be {secs[0]}-{secs[1]}s)"))
    return out


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

    # backgrounds: at least 2 beats on the brand-colour preset, contrast vs the subject, rim light on dark backgrounds
    if plan.get("preset") == "cutout_on_brand_colour" and dur >= 6 and len(bgs) < 2:
        probs.append(_p("bg_beats", "background", "brand-colour preset needs at least 2 background beats (a lighter second gradient "
                        "from about the middle) so the reel is not one flat colour"))
    if bgs:
        sl = _subject_luma(plan)
        ratios = [_lum_contrast(_bg_luma(b), sl) for b in bgs if isinstance(b, dict)]
        rim = ((plan.get("subject") or {}).get("rim") or {})
        if ratios and max(ratios) < MIN_BG_CONTRAST:
            probs.append(_p("bg_contrast", "background", f"no background beat separates from the subject (luminance contrast "
                            f"{max(ratios):.2f} < {MIN_BG_CONTRAST}); use a lighter beat"))
        if any(_bg_luma(b) < RIM_LUMA for b in bgs if isinstance(b, dict)) and not (rim.get("enabled") and _num(rim.get("px")) >= 2):
            probs.append(_p("rim_light", "subject/rim", f"a background is darker than luma {RIM_LUMA}: enable subject.rim (>= 2 px light "
                            "rim / outer glow)"))

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
        if t.get("role") == "cta":
            lk = locked_cta(concept)
            why = cta_issue(t.get("text") or "")
            if lk and t.get("text") != lk:
                probs.append(_p("locked", path, f"CTA text must equal the human-approved CTA {lk!r}"))
            elif not lk and why:
                probs.append(_p("cta_short", path, f"visible CTA {t.get('text')!r} is not a short CTA: {why}. "
                                f"Use at most {CTA_MAX_WORDS} words, e.g. {derive_cta_short(concept)!r}"))
            if t.get("cta_short") not in (None, t.get("text")):
                probs.append(_p("cta_short", path, "cta_short must equal the cta block text"))
        if t.get("role") == "title" and concept.get("title") and t.get("text") != clip_text(concept["title"]):
            probs.append(_p("locked", path, "title text must equal the concept title (human edits win)"))
        if len(t.get("text") or "") > MAX_TEXT_CHARS:
            probs.append(_p("text_len", path, f"{t.get('id')}: more than {MAX_TEXT_CHARS} characters"))
        if t.get("keyword") and t["keyword"].lower() not in (t.get("text") or "").lower():
            probs.append(_p("keyword", path, f"{t.get('id')}: highlight keyword {t['keyword']!r} is not in the text"))
        if t.get("style") == "pill_headline" and t.get("region") == "top_center":
            room = headline_room_px(plan)
            if room is not None:
                sz = int(_num(t.get("size"), 90))
                need = headline_height_px(t.get("text") or "", sz, int((t.get("stroke") or [8])[0]))
                if need > room:
                    probs.append(_p("headline_room", path, f"{t.get('id')}: headline needs {need}px but only {room}px are free above "
                                    f"the head (head top {int(_head_top(plan) * 1920)}px); shrink or shorten it"))
        if t.get("role") == "callout" and t.get("region") in ("left_mid", "right_mid"):
            best, ov = callout_side(plan)
            if ov and ov[t["region"]] > 0.02:
                other = "right_mid" if t["region"] == "left_mid" else "left_mid"
                probs.append(_p("callout_head", path, f"{t.get('id')}: callout in {t['region']} overlaps the head "
                                f"({ov[t['region']]:.2f} of frame width); use {best}",
                                fixable=ov[other] <= 0.02, level="error" if ov[other] <= 0.02 else "warn"))
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
    if cap.get("source") == "plan":
        for i, why in _chunk_problems(cap.get("chunks") or []):
            probs.append(_p("chunk_size", f"captions/chunks/{i}", f"caption chunk {i}: {why}"))
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
        if isinstance(ti, dict) and ti.get("type") == "dissolve":
            ti["type"] = "fade"
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
            if t.get("animation") not in ANIMATIONS:
                t["animation"] = "pop"
            if t.get("font") not in ("devanagari", "latin"):
                t["font"] = "latin"
            if isinstance(t.get("text"), str) and len(t["text"]) > MAX_TEXT_CHARS:
                t["text"] = clip_text(t["text"])
            if t.get("weight") not in (None, "regular", "bold", "black"):
                t["weight"] = "bold"
            if t.get("point") not in (None, "shoulder", "margin", "none"):
                t["point"] = "shoulder"
            if not isinstance(t.get("keyword"), (str, type(None))):
                t["keyword"] = None
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


ANIMATIONS = ("pop", "slide_left", "slide_right", "fade")
BEAT_TRANSITIONS = ("cut", "dissolve", "push", "flash")


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
    old = [x for x in (sub.get("segments") or []) if isinstance(x, dict)]
    core = lambda xs: [{k: x.get(k) for k in ("beat", "clip", "in", "out")} for x in xs]  # noqa: E731
    if want and core(old) != want:
        note("subject segments re-derived from concept beats")
        old = []
    if want:
        head = _head(plan) or {}
        per_clip = head.get("per_clip") if isinstance(head.get("per_clip"), dict) else {}
        sizes = [float(v) for v in per_clip.values() if isinstance(v, (int, float)) and v > 0]
        ref = statistics.median(sizes) if sizes else None
        segs_out = []
        for i, w in enumerate(want):
            seg = dict(w)
            prev = old[i] if i < len(old) else {}
            sc = _num(prev.get("subject_scale"), 1.0)
            hs = per_clip.get(w["clip"])
            if ref and isinstance(hs, (int, float)) and hs > 0:
                sc = ref / float(hs)  # equal head size across clips: no jump at the beat boundary
            seg["subject_scale"] = round(_clamp(sc, 0.85, 1.15) if ref else _clamp(sc, 0.7, 1.3), 3)
            if i > 0:
                ti = prev.get("transition_in") if isinstance(prev.get("transition_in"), dict) else {}
                ty = ti.get("type") if ti.get("type") in BEAT_TRANSITIONS else ("flash" if i == len(want) - 1 else "push")
                dflt = 0.15 if ty == "flash" else 0.25
                seg["transition_in"] = {"type": ty, "dur": round(_clamp(_num(ti.get("dur"), dflt), 0.1, 0.6), 2)}
            segs_out.append(seg)
        sub["segments"] = segs_out
    rim = sub.get("rim") if isinstance(sub.get("rim"), dict) else {}
    sub["rim"] = {"enabled": bool(rim.get("enabled", False)), "px": int(_clamp(_num(rim.get("px"), 2), 1, 6)),
                  "color": rim.get("color") if _is_hex(rim.get("color")) else "#fff3e0"}
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
    if dur >= 6 and all(b["kind"] == "gradient" for b in bgs) and len(bgs) < 2:
        c = bgs[0]["colors"]
        mid = min((w[0] for w in wins[1:]), key=lambda x: abs(x - dur / 2), default=round(dur / 2, 2))
        bgs.append({"id": "bg2", "kind": "gradient", "colors": [_mix_white(c[-1], 0.4), _mix_white(c[0], 0.2)], "src": None,
                    "prompt": None, "blur": 0, "zoom": 1.08, "dim": 1.0, "start": round(_clamp(mid, 1.0, dur - 1.0), 2),
                    "transition_in": {"type": "fade", "dur": 0.5}})
        note("second background beat added (a lighter gradient) so the reel is not one flat colour")
    sl = _subject_luma(plan)
    grads = [b for b in bgs if b["kind"] == "gradient"]
    if grads and max(_lum_contrast(_bg_luma(b), sl) for b in bgs) < MIN_BG_CONTRAST:
        bg, k = grads[-1], 0
        while _lum_contrast(_bg_luma(bg), sl) < MIN_BG_CONTRAST + 0.4 and k < 10:
            bg["colors"] = [_mix_white(c, 0.15) for c in bg["colors"]]
            k += 1
        note(f"{bg['id']}: lightened for contrast against the subject")
    if any(_bg_luma(b) < RIM_LUMA for b in bgs) and not (sub["rim"]["enabled"] and sub["rim"]["px"] >= 2):
        sub["rim"].update(enabled=True, px=max(2, sub["rim"]["px"]))
        note("subject rim light enabled (dark background)")

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
        if t.get("role") == "cta":
            lk = locked_cta(concept)
            if lk and t.get("text") != lk:
                t["text"] = lk
                note(f"{t['id']}: cta restored from the human-approved CTA")
            elif not lk and cta_issue(str(t.get("text") or "")):
                t["text"] = cta or derive_cta_short(concept)
                note(f"{t['id']}: cta replaced by a short CTA {t['text']!r}")
        if t.get("role") == "title" and concept.get("title") and t.get("text") != clip_text(concept["title"]):
            t["text"] = clip_text(concept["title"])
        t["text"] = str(t.get("text") or "")
        if t.get("role") == "cta":
            t["cta_short"] = t["text"]
        else:
            t.pop("cta_short", None)
        if t.get("role") == "hook":
            if not isinstance(t.get("stroke"), list) or _num(t["stroke"][0]) < 8:
                t["stroke"] = [8, (t["stroke"][1] if isinstance(t.get("stroke"), list) and _is_hex(t["stroke"][1]) else "#2a0a1a")]
            t["weight"] = "black"
            if isinstance(t.get("keyword"), str):
                t["keyword"] = t["keyword"].strip(".,!?;:'\" ")
            if not t.get("keyword") or t["keyword"].lower() not in t["text"].lower():
                ws = [w.strip(".,!?;:'\"") for w in t["text"].split()]
                ws = [w for w in ws if len(w) >= 4] or ws
                t["keyword"] = max(reversed(ws), key=len) if ws else None
        elif t.get("keyword") and t["keyword"].lower() not in t["text"].lower():
            t["keyword"] = None
        if t.get("role") == "callout":
            t["weight"] = t.get("weight") or "bold"
            t["point"] = t.get("point") if t.get("point") in ("shoulder", "margin", "none") else "shoulder"
            if t.get("region") in ("left_mid", "right_mid"):
                best, ov = callout_side(plan)
                if ov and ov[t["region"]] > 0.02 and ov[best] < ov[t["region"]]:
                    note(f"{t['id']}: callout moved to {best}, away from the head")
                    t["region"] = best
                    t["rot"] = -abs(t["rot"]) if best == "left_mid" else abs(t["rot"])
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
        # headline stays above the head
        if t.get("style") == "pill_headline" and t.get("region") == "top_center":
            room = headline_room_px(plan)
            stk = int((t.get("stroke") or [8])[0]) if isinstance(t.get("stroke"), list) else 8
            if room is not None and headline_height_px(t["text"], t["size"], stk) > room:
                new = _fit_size(t["text"], t["size"], room, stk)
                note(f"{t['id']}: headline size {t['size']} -> {new} to stay above the head")
                t["size"] = new
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
        if not all(contrast(fill, c) >= 4.5 for c in ref) and not (stroke_ok and not plate) \
                and not plate and contrast(fill, "#2a0a1a") >= 4.5:
            t["stroke"] = [8, "#2a0a1a"]
            note(f"{t['id']}: dark 8 px stroke added for contrast")
        elif not all(contrast(fill, c) >= 4.5 for c in ref) and not (stroke_ok and not plate):
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
    if cap["source"] == "plan" and _chunk_problems(chunks):
        re_cut = rechunk_captions(chunks)
        if re_cut is None:
            note("caption chunks cannot meet 3-6 words / 0.8-2.0 s: using speech-timed captions instead")
            cap["source"], chunks = "asr", []
        else:
            note("caption chunks re-cut to 3-6 words / 0.8-2.0 s")
            chunks = re_cut
    cap["chunking"] = {"words": list(CAPTION_WORDS), "dur": list(CAPTION_SECS)}
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
            "size": 96, "style": "pill_headline", "fill": "#ffffff", "stroke": [8, "#2a0a1a"], "plate": None,
            "pill_text": None, "rot": 0, "animation": "pop", "weight": "black", "start": round(s + 0.2, 2), "end": round(min(e, s + 3.2), 2), "beat": 0}


def _cta_block(concept, wins, plan=None) -> dict:
    s, e = wins[-1]
    return {"id": "cta", "role": "cta", "text": _cta_text(concept), "cta_short": _cta_text(concept), "region": "cta_top",
            "font": "latin", "size": 64,
            "style": "cta_pill", "fill": "#111111", "stroke": None, "plate": "#fbbf24", "pill_text": None, "rot": 0,
            "animation": "slide_left", "start": round(max(s, e - 3.0), 2), "end": round(e, 2), "beat": len(wins) - 1}


def default_plan(concept: dict, brand_colours: list[str] | None = None, *, look: str | None = None,
                head: dict | None = None) -> dict:
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
    if isinstance(head, dict):
        plan["meta"]["head"] = head
    plan = repair_plan(plan, [], concept)  # second brand beat, rim light, beat transitions, headline fit, callout side
    plan["meta"] = {k: v for k, v in plan["meta"].items() if k != "repaired"}
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
{cta_line}
output duration: {dur}s
{head_line}
{palette_line}

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
ENUM VALUES (use these literal strings and nothing else; any other value is rejected):
{enums}

RULES
- background[0].start = 0; later backgrounds dissolve in (transition_in fade 0.4-0.8s) at their start. 1-3 backgrounds. Use plates only if the preset is cutout_on_textile_plate; dim plates to <= 0.6 and tint colors to the brand palette so the speaker separates from them.
- Never use a still that contains a person as a background. Never put a person-describing word or any text in a plate prompt.
- Text: the hook in top_center (pill_headline), the CTA in cta_top (cta_pill) during the last beat, at most one short callout_tag (2-4 words, left_mid/right_mid) from a beat's on_screen_text. At most 2 text blocks visible at once (spoken-word captions count as one: captions.source "asr", chunks [] , the pipeline fills them). Keep all times inside the beat windows; blocks last >= 1.0s.
- No prices, discounts, percentages, urgency or claims not in the concept/brief. pill_text is a neutral label like "STORY" or null.
- Contrast: text fill must have contrast >= 4.5 against its plate (or the dimmed background); captions.scrim >= 0.4.
- Add motion variety: different animations per block, background zoom 1.0-1.15.
- The visible CTA is a SHORT separate line (at most 4 words, e.g. "Shop Maheshwari"), never an internal direction: no " - ", "card", "soft", "end". Set the cta block's cta_short to the same text. Every text is at most 80 characters.
- Backgrounds: on cutout_on_brand_colour use at least 2 background beats (the second a clearly lighter mid-tone gradient, luminance 0.15-0.45, from about the middle, at a beat boundary; never near-white), never a still with people, and keep the background's luminance different from the saree so she separates. If a background is dark (luma < 0.2) set subject.rim.enabled true (px >= 2).
- Hook: stroke width 8, weight "black", keyword = ONE word of the hook to highlight in yellow. The headline must fit above the speaker's head. A callout sits in the side margin (left_mid/right_mid) away from the head with point "shoulder", never over the face.
- Between beats set segments[i].transition_in (i >= 1) to push (0.25s) or flash (0.15s) or dissolve; subject_scale 1.0 unless the head sizes differ.
- Caption chunks (only if captions.source is "plan") are 3-6 words and 0.8-2.0 s each; normally keep source "asr".
{look}{guidance}
Return JSON matching this shape exactly (all keys shown are required):
{shape}"""

SHAPE = {
    "version": 1, "preset": "cutout_on_brand_colour", "size": [1080, 1920], "fps": 30, "duration": 14.1,
    "rationale": "one sentence why this look fits the brief",
    "subject": {"segments": [{"beat": 0, "clip": "A.mov", "in": 1.0, "out": 4.0, "subject_scale": 1.0},
                             {"beat": 1, "clip": "B.mov", "in": 2.0, "out": 5.0, "subject_scale": 1.0,
                              "transition_in": {"type": "push", "dur": 0.25}}],
                "mask": {"prompt": "woman", "feather": 2.0, "edge": [0.35, 0.65]},
                "transform": {"scale": 1.0, "x": 0, "y": 0}, "shadow": 0.5,
                "rim": {"enabled": True, "px": 2, "color": "#fff3e0"}},
    "background": [{"id": "bg1", "kind": "gradient", "colors": ["#3b0a2a", "#c2410c"], "src": None, "prompt": None,
                    "blur": 0, "zoom": 1.08, "dim": 1.0, "start": 0, "transition_in": {"type": "cut", "dur": 0}},
                   {"id": "bg2", "kind": "gradient", "colors": ["#a3586f", "#d98a5f"], "src": None, "prompt": None,
                    "blur": 0, "zoom": 1.08, "dim": 1.0, "start": 4.0, "transition_in": {"type": "fade", "dur": 0.5}}],
    "text": [{"id": "hook", "role": "hook", "text": "...", "region": "top_center", "font": "latin", "size": 96,
              "style": "pill_headline", "fill": "#ffffff", "stroke": [8, "#2a0a1a"], "plate": None, "pill_text": "STORY",
              "rot": 0, "animation": "pop", "start": 0.2, "end": 3.2, "beat": 0, "weight": "black", "keyword": "one word of the text"},
             {"id": "cta", "role": "cta", "text": "Shop Maheshwari", "cta_short": "Shop Maheshwari", "region": "cta_top",
              "font": "latin", "size": 64, "style": "cta_pill", "fill": "#111111", "stroke": None, "plate": "#fbbf24",
              "pill_text": None, "rot": 0, "animation": "slide_left", "start": 11.0, "end": 14.0, "beat": 3}],
    "captions": {"style": "pop_captions", "source": "asr", "chunks": [], "fill": "#ffffff", "highlight": "#fbbf24",
                 "stroke": [6, "#2a0a1a"], "scrim": 0.5},
    "overlays": [], "audio": {"source": "subject", "music": None},
}


def _enum_table() -> str:
    """Every enum of the schema spelled out literally, so the model never invents a value (and never costs a retry)."""
    P = SCHEMA["properties"]
    ti = P["text"]["items"]["properties"]
    seg = P["subject"]["properties"]["segments"]["items"]["properties"]
    rows = {
        "preset": P["preset"]["enum"],
        "background[].kind": P["background"]["items"]["properties"]["kind"]["enum"],
        "background[].transition_in.type": SCHEMA["$defs"]["transition"]["properties"]["type"]["enum"],
        "subject.segments[].transition_in.type": seg["transition_in"]["properties"]["type"]["enum"],
        "text[].role": ti["role"]["enum"],
        "text[].region": ti["region"]["enum"],
        "text[].font": ti["font"]["enum"],
        "text[].style": ti["style"]["enum"],
        "text[].animation": ti["animation"]["enum"],
        "text[].weight": ti["weight"]["enum"],
        "text[].point": ti["point"]["enum"],
        "captions.source": P["captions"]["properties"]["source"]["enum"],
        "overlays[].region": P["overlays"]["items"]["properties"]["region"]["enum"],
        "overlays[].animation": P["overlays"]["items"]["properties"]["animation"]["enum"],
        "fps": P["fps"]["enum"],
    }
    return "\n".join(f"- {k}: " + " | ".join(json.dumps(v) for v in vals) for k, vals in rows.items())


def _assets_block(assets_info: dict | None, key: str) -> str:
    items = (assets_info or {}).get(key) or []
    if not items:
        return "(none)"
    return "; ".join(f"{a['name']}" + (f" - {a.get('description', '')}" if a.get("description") else "") +
                     (" [CONTAINS PEOPLE, not allowed as background]" if a.get("has_people") else "") for a in items)


def _palette_line(brand_colours) -> str:
    cols = [c for c in (brand_colours or []) if _is_hex(c)]
    if cols:
        return (f"BRAND PALETTE: {', '.join(cols)}. Build gradients from these; the colours in the JSON shape below are "
                "examples only, do not copy them.")
    return ("BRAND PALETTE: not given; pick 2-3 rich colours that suit the garment and the brief. The colours in the JSON "
            "shape below are examples only, do not copy them.")


def _build_prompt(concept, brief, memory_text, assets_info, guidance, look, errors, head=None, brand_colours=None) -> str:
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
        hook=_hook_text(concept), cta_line=_cta_line(concept), head_line=_head_line(head), palette_line=_palette_line(brand_colours), dur=total_duration(concept),
        enums=_enum_table(),
        backdrops=_assets_block(assets_info, "backdrops"), pips=_assets_block(assets_info, "pips"),
        presets="\n".join(f"- {k}: {v}" for k, v in PRESETS.items()),
        styles="\n".join(f"- {k}: {v}" for k, v in TEXT_STYLES.items()),
        regions=json.dumps(REGION_CAP), look=look_s, guidance=g, shape=shape)
    if errors:
        p += "\n\nYOUR LAST PLAN FAILED THESE CHECKS. Fix every one and return the full corrected JSON:\n" + \
             "\n".join(f"- {e}" for e in errors)
    return p


def _cta_line(concept: dict) -> str:
    lk = locked_cta(concept)
    if lk:
        return f'human-approved visible CTA (use verbatim as the cta block text and cta_short, role "cta"): {lk!r}'
    if not _cta_text(concept):
        return "no CTA"
    raw = (concept.get("cta") or "").strip()
    note = f" (an internal direction, NEVER show it: {raw!r})" if raw else ""
    return (f'CTA: write a SHORT visible CTA of at most 4 words as the cta block text and cta_short, '
            f'e.g. {derive_cta_short(concept)!r}{note}')


def _head_line(head: dict | None) -> str:
    if not isinstance(head, dict):
        return ""
    top = head.get("top_frac") if isinstance(head.get("top_frac"), (int, float)) else DEFAULT_HEAD_TOP
    room = int(top * 1920) - HEADLINE_TOP_PX - HEADLINE_GAP_PX
    bb = head.get("bbox_frac")
    side = f"; the speaker box is {[round(float(v), 2) for v in bb]} of the frame" if isinstance(bb, (list, tuple)) and len(bb) == 4 else ""
    return (f"SPEAKER GEOMETRY: her head top is at {int(top * 1920)} px of 1920, so the headline has about {room} px of height "
            f"(shrink size or shorten words to fit; 2 lines at size 96 need ~230 px){side}.")


def _look_preset(look) -> str | None:
    if isinstance(look, str) and look in PRESETS:
        return look
    if isinstance(look, dict) and look.get("preset") in PRESETS:
        return look["preset"]
    return None


def plan_composition(run, concept: dict, brief: str, memory_text: str, assets_info: dict | None, *,
                     guidance: str = "", look=None, retries: int = 2, brand_colours: list[str] | None = None,
                     spark=None, head: dict | None = None) -> dict:
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
        prompt = _build_prompt(concept, brief, memory_text, assets_info, guidance, look, errors, head, brand_colours)
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
        if isinstance(head, dict):
            raw["meta"] = dict(raw.get("meta") if isinstance(raw.get("meta"), dict) else {}, head=head)
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
    plan = default_plan(concept, brand_colours, look=want, head=head)
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

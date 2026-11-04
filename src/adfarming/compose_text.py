"""Text / card artwork for the composition renderer: pure Pillow, no ffmpeg, no network.

Design space is ALWAYS 1080x1920 (the renderer scales the final video if another size is asked for).

Instagram Reels safe zones (1080x1920 design space), documented decision:
  * top 12 percent (y < 230) is covered by the Reels header (back arrow, "Reels", camera) -> no text/cards.
  * bottom 18 percent (y > 1574) is covered by the caption/username/audio row and the progress bar -> no text.
  * 54 px side margins (rounded-corner phones, thumb-crop in grid view).
  The like/comment/share column floats on the right edge from y~1180; regions never extend into x > 1026.
Regions below are the ONLY places the planner can put text; the LLM never emits pixel maths.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .cuts import Face  # mixed-script face: Devanagari run -> Noto Sans Devanagari, Latin run -> Noto Sans

W, H = 1080, 1920
SAFE = {"top": 230, "bottom": 1574, "left": 54, "right": 1026}   # x0,y0,x1,y1 = left, top, right, bottom
SAFE_BOX = (SAFE["left"], SAFE["top"], SAFE["right"], SAFE["bottom"])
# region name -> (x0, y0, x1, y1, align)
REGIONS: dict[str, tuple[int, int, int, int, str]] = {
    "top_center": (54, 240, 1026, 600, "center"),
    "cta_top": (54, 250, 1026, 430, "center"),
    "left_mid": (54, 640, 470, 1100, "left"),
    "right_mid": (610, 640, 1026, 1100, "right"),
    "bottom_caption": (54, 1300, 1026, 1570, "center"),
}
# default role look by region (planner may override through text.style)
ROLE_BY_REGION = {"top_center": "hook", "cta_top": "cta", "left_mid": "callout", "right_mid": "callout",
                  "bottom_caption": "caption"}
ROLE_STYLE: dict[str, dict] = {
    # hook typography: heavy (faux-black) weight, 8 px outline, one highlighted keyword, tight 2-line stack
    "hook": {"size": 96, "fill": "#ffffff", "stroke": [8, "#1a0a14"], "bg": None, "hl": "#fde047", "heavy": 3, "lh": 1.16,
             "shadow": True, "animation": "pop", "max_lines": 2},
    "cta": {"size": 58, "fill": "#ffffff", "stroke": [0, "#000000"], "bg": {"fill": "#e11d48", "radius": 80, "pad": 30},
            "shadow": True, "animation": "pop", "max_lines": 1},
    "callout": {"size": 64, "fill": "#2a0a1a", "stroke": [0, "#000000"], "bg": {"fill": "#fbbf24", "radius": 30, "pad": 26},
                "rot": -6, "tail": "auto", "shadow": True, "animation": "slide_left", "max_lines": 3},
    "caption": {"size": 84, "fill": "#ffffff", "stroke": [9, "#1a0a14"], "hl": "#fde047", "animation": "pop", "max_lines": 2},
}
SCRIM_ALPHA = 0.62   # requested scrim strength (the renderer caps what is drawn at SCRIM_CAP: stroke + shadow carry the rest)
SCRIM_CAP = 0.35
SCRIM_MIN_H = 400    # the gradient scrim is at least this tall so it never reads as a rectangle
WEIGHT_HEAVY = {"regular": 0, "bold": 2, "black": 3}
HEAD_GAP = 24        # clear space kept between a headline and the top of the head


@dataclass(frozen=True)
class Layout:
    """Everything that differs per output ratio. Text regions, safe zone, caption box and where the cut-out sits.
    anchor 'bottom': subject sits on the bottom edge (9:16). anchor 'head': subject placed so the head top lands at head_y."""
    name: str
    w: int
    h: int
    safe: tuple
    regions: dict = field(hash=False)
    cap_box: tuple = (54, 1300, 1026, 1570)
    size_k: float = 1.0
    scrim: bool = True
    anchor: str = "bottom"
    sub_cx: float = 540.0
    head_y: float = 0.0
    sub_scale: float = 1.0
    ok: bool = True


LAYOUTS: dict[str, Layout] = {}


def _mk_layouts() -> None:
    LAYOUTS["9x16"] = Layout("9x16", 1080, 1920, SAFE_BOX, REGIONS, REGIONS["bottom_caption"][:4])
    LAYOUTS["1x1"] = Layout(
        "1x1", 1080, 1080, (54, 60, 1026, 990),
        {"top_center": (54, 60, 1026, 380, "center"), "cta_top": (54, 60, 1026, 200, "center"),
         "left_mid": (54, 430, 330, 780, "left"), "right_mid": (750, 430, 1026, 780, "right"),
         "bottom_caption": (54, 800, 1026, 985, "center")},
        (54, 800, 1026, 985), size_k=0.84, anchor="head", sub_cx=540.0, head_y=384.0, sub_scale=0.86)
    LAYOUTS["16x9"] = Layout(
        "16x9", 1920, 1080, (90, 60, 1830, 1020),
        {"top_center": (90, 110, 830, 500, "left"), "cta_top": (90, 110, 830, 260, "left"),
         "left_mid": (90, 520, 830, 790, "left"), "right_mid": (90, 520, 830, 790, "left"),
         "bottom_caption": (90, 810, 830, 1000, "left")},
        (90, 810, 830, 1000), size_k=1.0, scrim=False, anchor="head", sub_cx=1306.0, head_y=150.0, sub_scale=0.82)


def layout_for_size(size) -> Layout:
    """Pick the layout from the requested output size (any scale of 9:16, 1:1 or 16:9)."""
    if not LAYOUTS:
        _mk_layouts()
    if size is None:
        return LAYOUTS["9x16"]
    a = size[0] / float(size[1])
    return LAYOUTS["9x16"] if a < 0.75 else (LAYOUTS["1x1"] if a < 1.34 else LAYOUTS["16x9"])


def get_layout(name: str) -> Layout:
    if not LAYOUTS:
        _mk_layouts()
    return LAYOUTS[name]


def hexc(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def relative_luminance(rgb) -> float:
    """WCAG relative luminance, 0..1."""
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(float(v)) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(c1, c2) -> float:
    """WCAG contrast ratio (1..21) between two colours (hex string or RGB tuple)."""
    a = relative_luminance(hexc(c1) if isinstance(c1, str) else c1)
    b = relative_luminance(hexc(c2) if isinstance(c2, str) else c2)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def blend(c_top, c_bottom, alpha: float) -> tuple[int, int, int]:
    """Composite colour c_top at opacity alpha over c_bottom."""
    t = hexc(c_top) if isinstance(c_top, str) else c_top
    b = hexc(c_bottom) if isinstance(c_bottom, str) else c_bottom
    return tuple(int(round(t[i] * alpha + b[i] * (1 - alpha))) for i in range(3))  # type: ignore[return-value]


def box_in_safe(box, safe=SAFE_BOX) -> bool:
    x0, y0, x1, y1 = box
    return x0 >= safe[0] and y0 >= safe[1] and x1 <= safe[2] and y1 <= safe[3]


def box_overflow(box, safe=SAFE_BOX) -> list[str]:
    """Which edges of box poke outside the safe rect (e.g. ['top 40px'])."""
    x0, y0, x1, y1 = box
    out = []
    if y0 < safe[1]:
        out.append(f"top {safe[1] - y0:.0f}px into the Reels header zone")
    if y1 > safe[3]:
        out.append(f"bottom {y1 - safe[3]:.0f}px into the Reels caption/UI zone")
    if x0 < safe[0]:
        out.append(f"left {safe[0] - x0:.0f}px")
    if x1 > safe[2]:
        out.append(f"right {x1 - safe[2]:.0f}px")
    return out


# ---------- wrapping ----------
_DUMMY = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
_faces: dict[int, Face] = {}


def face(size: int) -> Face:
    if size not in _faces:
        _faces[size] = Face(size)
    return _faces[size]


def wrap_text(text: str, size: int, max_w: float, stroke: int = 0) -> list[str]:
    """Greedy word wrap with the mixed-script face; honours explicit newlines; hard-breaks a word that is
    wider than max_w by code point (never inside a Devanagari cluster: breaks only before a base letter)."""
    f = face(size)
    avail = max_w - 2 * stroke

    def length(s: str) -> float:
        return f.length(_DUMMY, s)

    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for w in para.split():
            trial = f"{cur} {w}".strip()
            if length(trial) <= avail or not cur:
                cur = trial
                if length(cur) > avail and " " not in cur:  # single overlong word: break it
                    pieces = _hard_break(cur, f, avail)
                    lines.extend(pieces[:-1])
                    cur = pieces[-1]
            else:
                lines.append(cur)
                cur = w
                if length(cur) > avail:
                    pieces = _hard_break(cur, f, avail)
                    lines.extend(pieces[:-1])
                    cur = pieces[-1]
        lines.append(cur)
    return [l for l in lines if l] or [""]


def _is_mark(ch: str) -> bool:
    """Devanagari combining marks / virama / ZWJ: a line may not start with one."""
    o = ord(ch)
    return o in (0x200C, 0x200D) or 0x0900 <= o <= 0x0903 or 0x093A <= o <= 0x094F or 0x0951 <= o <= 0x0957 or 0x0962 <= o <= 0x0963


def _hard_break(word: str, f: Face, avail: float) -> list[str]:
    out, cur = [], ""
    for i, ch in enumerate(word):
        nxt = cur + ch
        if cur and f.length(_DUMMY, nxt) > avail and not _is_mark(ch) and not cur.endswith("्"):
            out.append(cur)
            cur = ch
        else:
            cur = nxt
    out.append(cur)
    return out


def fit_text(text: str, size: int, max_w: float, max_h: float, max_lines: int, stroke: int = 0,
             min_scale: float = 0.55, line_h: float = 1.34) -> tuple[list[str], int]:
    """Shrink the font in 2 px steps until the wrapped text fits (width, height, line count) or the floor is hit."""
    s = size
    floor = max(16, int(size * min_scale))
    while True:
        lines = wrap_text(text, s, max_w, stroke)
        if (len(lines) <= max_lines and len(lines) * s * line_h <= max_h) or s <= floor:
            return balance_lines(lines, s), s
        s -= 2


def balance_lines(lines: list[str], size: int) -> list[str]:
    """Two-line text: split where the longer line is shortest (no one-word widow under a long first line)."""
    if len(lines) != 2:
        return lines
    words = (lines[0] + " " + lines[1]).split()
    if len(words) < 3 or "\n" in "".join(lines):
        return lines
    f = face(size)
    best = min(range(1, len(words)), key=lambda k: max(f.length(_DUMMY, " ".join(words[:k])), f.length(_DUMMY, " ".join(words[k:]))))
    cand = [" ".join(words[:best]), " ".join(words[best:])]
    wmax = max(f.length(_DUMMY, l) for l in lines)
    return cand if max(f.length(_DUMMY, l) for l in cand) <= wmax + 1 else lines


# ---------- styles ----------
STYLE_ROLE = {"pill_headline": "hook", "callout_tag": "callout", "cta_pill": "cta"}   # composition-plan style enum -> look


def resolve_style(item: dict, lay: "Layout | None" = None) -> dict:
    """Merge the preset look with the item's own fields. Accepts the composition-plan schema (style enum string,
    plate hex|null, pill_text, stroke [w,hex]|null, fill, rot, animation, weight, keyword, point) and, for tests/tools,
    a style dict. `lay` rescales the font size for a ratio."""
    region = item.get("region", "top_center")
    sty = item.get("style")
    role = STYLE_ROLE.get(sty) if isinstance(sty, str) else None
    role = role or ROLE_BY_REGION.get(region, "hook")
    base = {k: (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v)) for k, v in ROLE_STYLE[role].items()}
    if isinstance(sty, dict):
        for k, v in sty.items():
            base[k] = ({**(base.get("bg") or {}), **v} if (k == "bg" and v) else v)
    for k in ("size", "fill", "animation", "rot"):
        if item.get(k) is not None:
            base[k] = item[k]
    if "plate" in item:   # schema: a colour = pill/tag background, null = text straight on the background
        base["bg"] = None if item["plate"] is None else {**(base.get("bg") or {"radius": 40, "pad": 28}), "fill": item["plate"], "alpha": 255}
    if "stroke" in item:
        base["stroke"] = item["stroke"] or [0, "#000000"]
    if item.get("pill_text"):
        base["pill"] = {"text": item["pill_text"], "fill": "#fbbf24", "color": "#2a0a1a", "size": 32}
    if item.get("weight") in WEIGHT_HEAVY:
        base["heavy"] = WEIGHT_HEAVY[item["weight"]]
    sw = base.get("stroke") or [0, "#000000"]
    base["stroke"] = [int(sw[0]), sw[1]]
    if role == "hook" and not base.get("bg"):   # headline straight on the background: 8 px outline minimum
        base["stroke"][0] = max(base["stroke"][0], 8)
    if lay is not None and lay.size_k != 1.0:
        base["size"] = int(round(float(base["size"]) * lay.size_k / 2) * 2)
    base["role"] = role
    base["region"] = region
    base["keyword"] = item.get("keyword")
    base["point"] = item.get("point") or "shoulder"
    return base


def text_box(item: dict, lay: "Layout | None" = None) -> tuple[int, int, int, int]:
    """Placement box in design px: an explicit item['box'] else the named region of the layout."""
    if item.get("box"):
        return tuple(int(v) for v in item["box"])  # type: ignore[return-value]
    regs = (lay.regions if lay is not None else REGIONS)
    r = regs.get(item.get("region", "top_center"), regs["top_center"])
    return r[:4]


def visible_text(item: dict) -> str:
    """What is drawn: a cta block shows its SHORT cta_short, never the raw concept direction."""
    if (item.get("role") == "cta" or item.get("style") == "cta_pill") and item.get("cta_short"):
        return str(item["cta_short"])
    return str(item["text"])


# ---------- block artwork ----------
_STOP = {"the", "and", "with", "from", "this", "that", "your", "for", "phir", "bhi", "thi", "hai", "aur", "ke", "ki", "ka", "ko"}


def _norm(w: str) -> str:
    return "".join(ch for ch in w.lower() if ch.isalnum())


def hl_index(lines: list[str], keyword: str | None, auto: bool) -> int | None:
    """Index (over all words, line by line) of the highlighted word: the plan's keyword, else (auto) the longest word."""
    flat = [w for l in lines for w in l.split()]
    if keyword:
        kn = _norm(keyword)
        for i, w in enumerate(flat):
            if kn and _norm(w) == kn:
                return i
        for i, w in enumerate(flat):
            if kn and kn in _norm(w):
                return i
    if auto and len(flat) >= 3:
        cand = [(len(_norm(w)), i) for i, w in enumerate(flat) if _norm(w) not in _STOP and len(_norm(w)) >= 4]
        if cand:
            return max(cand)[1]
    return None


def _draw_words(d: ImageDraw.ImageDraw, f: Face, lines: list[str], cx: float, y: float, lh: int, *, fill, hl_i=None,
                hl_fill=None, sw=0, stroke_fill=None, heavy=0, align="center", left=0.0, right=0.0, outline=False) -> None:
    """Draw word by word (so one keyword can take the highlight colour). outline=True draws only the dark outline
    (width sw + heavy) for every word; the next call draws the fill. heavy>0 fakes a Black weight by stroking the
    glyphs in their own colour (the bundled fonts stop at Medium)."""
    sp = f.length(d, " ")
    k = 0
    for i, line in enumerate(lines):
        lw = f.length(d, line)
        x = cx - lw / 2 if align == "center" else (left if align == "left" else right - lw)
        for w in line.split():
            col = hl_fill if (hl_i is not None and k == hl_i and hl_fill) else fill
            if outline:
                if sw + heavy:
                    f.draw(d, (x, y + i * lh), w, fill=stroke_fill, stroke_width=sw + heavy, stroke_fill=stroke_fill)
            elif heavy:
                f.draw(d, (x, y + i * lh), w, fill=col, stroke_width=heavy, stroke_fill=col)
            else:
                f.draw(d, (x, y + i * lh), w, fill=col)
            x += f.length(d, w) + sp
            k += 1


def _callout_box(lay: "Layout", head: tuple, st: dict, base_box: tuple) -> tuple[tuple, str, str]:
    """Callouts never sit on the face: beside the head in the free side margin when it is wide enough, else on the
    shoulder under the chin, hugging the margin on the side with more room. Returns (box, side, valign)."""
    hx0, hy0, hx1, hy1 = head
    side = "left" if (hx0 + hx1) / 2 >= lay.w / 2 else "right"
    sx0, sy0, sx1, sy1 = lay.safe
    limit = min(sy1, lay.cap_box[1] - 16)     # never down into the caption zone
    free = (hx0 - 24 - sx0) if side == "left" else (sx1 - (hx1 + 24))
    if free >= 200:   # beside the head, at jaw height, in the free side margin
        top = hy0 + 0.40 * (hy1 - hy0)
        wid = min(free, 430)
    else:             # on the shoulder, below the chin
        top = hy1 + 36
        wid = 430
    top = min(top, limit - 170)
    bot = min(top + 400, limit)
    box = (sx0, top, sx0 + wid, bot) if side == "left" else (sx1 - wid, top, sx1, bot)
    bx = tuple(int(round(v)) for v in box)
    return bx, side, "top"


def render_block(item: dict, lay: "Layout | None" = None, head: tuple | None = None
                 ) -> tuple[Image.Image, tuple[float, float], dict]:
    """Static RGBA art for a text block + (cx, cy) placement centre + info {size, lines, fits, w, h, box}.
    The image includes its own padding for the drop shadow and tail. `head` = the head box in canvas px (x0,y0,x1,y1):
    headlines stay above its top, callouts stay off it and their tail points at the shoulder."""
    lay = lay or get_layout("9x16")
    st = resolve_style(item, lay)
    text = visible_text(item)
    x0, y0, x1, y1 = text_box(item, lay)
    region = st["region"]
    ralign = lay.regions.get(region, ("", "", "", "", "center"))[4] if region in lay.regions else "center"
    role = st["role"]
    valign, side = "mid", None
    if head is not None and not item.get("box"):
        if region == "top_center" and role in ("hook", "other") and head[0] < x1 and head[2] > x0:
            y1 = max(y0 + 150, min(y1, int(head[1]) - HEAD_GAP))
        elif role == "callout" and region in ("left_mid", "right_mid") and lay.anchor in ("bottom", "head") and lay.name != "16x9":
            (x0, y0, x1, y1), side, valign = _callout_box(lay, head, st, (x0, y0, x1, y1))
            ralign = side
    if st.get("pill") and region == "top_center" and (y1 - y0) < 262:   # squeezed under the head: the label pill goes first
        st["pill"] = None
    bg = st.get("bg")
    pad = int(bg["pad"]) if bg else 0
    sw = st["stroke"][0]
    heavy = int(st.get("heavy", 0) or 0)
    pill = st.get("pill")
    ph = (int(pill.get("size", 32)) + 24) if pill else 0
    chip_gap = 10 if pill else 0
    tail = st.get("tail", "none")
    if st.get("point") == "none" and tail == "auto":
        tail = "none"
    if tail == "auto":
        if bg and region in ("left_mid", "right_mid"):
            if side:   # tail points IN toward the shoulder/saree (or out to the margin when asked), never up at the face
                inward = "down_right" if side == "left" else "down_left"
                outward = "down_left" if side == "left" else "down_right"
                tail = outward if st.get("point") == "margin" else inward
            else:
                tail = "down_right" if (region == "left_mid" or lay.name == "16x9") else "down_left"
        else:
            tail = "none"
    tail_h = 46 if tail not in ("none", None) else 0
    margin = 30
    max_w = (x1 - x0) - 2 * pad - (24 if bg else 0) - 16
    max_h = (y1 - y0) - 2 * pad - ph - chip_gap - tail_h - (0 if bg else int(int(st["size"]) * 0.12))
    lhk = float(st.get("lh", 1.34))
    lines, size = fit_text(text, int(st["size"]), max_w, max(max_h, 40), int(st.get("max_lines", 3)), stroke=sw + heavy, line_h=lhk)
    f = face(size)
    lh = int(size * lhk)
    tw = int(max(f.length(_DUMMY, l) for l in lines)) + 2 * (sw + heavy)
    cw, bh = tw + 2 * pad + (24 if bg else 0), lh * len(lines) + 2 * pad + (int(size * 0.12) if not bg else 0)
    fits = (tw + 2 * pad) <= (x1 - x0) and (bh + ph + chip_gap + tail_h) <= (y1 - y0) + 1
    pw = 0
    if pill:
        pf = face(int(pill.get("size", 32)))
        pw = int(pf.length(_DUMMY, pill["text"])) + 70
        cw = max(cw, pw)
    img = Image.new("RGBA", (cw + 2 * margin, bh + ph + chip_gap + tail_h + 2 * margin), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ox, oy = margin, margin
    if pill:
        px0 = ox + (cw - pw) // 2
        d.rounded_rectangle([px0, oy, px0 + pw, oy + ph], radius=ph // 2, fill=pill.get("fill", "#fbbf24"))
        face(int(pill.get("size", 32))).draw(d, (px0 + 35, oy + 11), pill["text"], fill=pill.get("color", "#2a0a1a"))
        oy += ph + chip_gap
    hli = hl_index(lines, st.get("keyword"), auto=(role == "hook" and not bg))
    hl_fill = st.get("hl") if (hli is not None) else None
    if st.get("shadow"):
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        if bg:
            sd.rounded_rectangle([ox, oy + 12, ox + cw, oy + bh + 12], radius=int(bg["radius"]), fill=(0, 0, 0, 120))
        else:   # soft drop shadow that follows the glyph outline
            _draw_words(sd, f, lines, ox + cw / 2 + 3, oy + pad + 8, lh, fill=(0, 0, 0, 190), sw=sw, stroke_fill=(0, 0, 0, 190),
                        heavy=heavy, outline=True)
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(9)))
        d = ImageDraw.Draw(img)
    if bg:
        col = hexc(bg["fill"]) + (int(bg.get("alpha", 255)),)
        d.rounded_rectangle([ox, oy, ox + cw, oy + bh], radius=int(bg["radius"]), fill=col)
        if tail not in ("none", None):
            if tail == "down_right":
                pts = [(ox + cw * 0.62, oy + bh - 2), (ox + cw * 0.86, oy + bh - 2), (ox + cw * 0.96, oy + bh + tail_h)]
            elif tail == "down_left":
                pts = [(ox + cw * 0.38, oy + bh - 2), (ox + cw * 0.14, oy + bh - 2), (ox + cw * 0.04, oy + bh + tail_h)]
            elif tail == "down":
                pts = [(ox + cw * 0.42, oy + bh - 2), (ox + cw * 0.58, oy + bh - 2), (ox + cw * 0.5, oy + bh + tail_h)]
            else:
                pts = []
            if pts:
                d.polygon(pts, fill=col)
    ty = oy + pad + (int(size * 0.06) if not bg else 0)
    if not bg:
        _draw_words(d, f, lines, ox + cw / 2, ty, lh, fill=st["fill"], sw=sw, stroke_fill=st["stroke"][1], heavy=heavy, outline=True)
    _draw_words(d, f, lines, ox + cw / 2, ty, lh, fill=st["fill"], hl_i=hli, hl_fill=hl_fill, heavy=0 if bg and not heavy else heavy)
    rot = float(st.get("rot", 0) or 0)
    if rot:
        img = img.rotate(rot, expand=True, resample=Image.BICUBIC)
    # placement
    if ralign == "left":
        cx = x0 + img.width / 2 - margin
    elif ralign == "right":
        cx = x1 - img.width / 2 + margin
    else:
        cx = (x0 + x1) / 2
    cy = (y0 + img.height / 2 - margin) if valign == "top" else (y0 + y1) / 2
    info = {"size": size, "lines": lines, "fits": fits, "w": img.width, "h": img.height, "hl": hli,
            "box": (cx - img.width / 2 + margin, cy - img.height / 2 + margin, cx + img.width / 2 - margin, cy + img.height / 2 - margin), "role": st["role"],
            "tail": tail, "ink_w": tw + 2 * pad, "ink_h": bh + ph + chip_gap + tail_h, "side": side}
    return img, (cx, cy), info


def render_caption(text: str, st: dict, box: tuple[int, int, int, int], hl_index: int | None = None,
                   lay: "Layout | None" = None) -> tuple[Image.Image, tuple[float, float], dict]:
    """One caption chunk: white words with dark stroke, one highlighted word (default the last), a soft text shadow.
    The block is anchored by its bottom edge: one- and two-line chunks share the SAME last baseline (no jumping)."""
    lay = lay or get_layout("9x16")
    sw = st["stroke"][0]
    x0, y0, x1, y1 = box
    size0 = int(round(int(st["size"]) * lay.size_k / 2) * 2) if lay.size_k != 1.0 else int(st["size"])
    max_w = (x1 - x0) - 40
    lines, size = fit_text(text, size0, max_w, y1 - y0 - 20, int(st.get("max_lines", 2)), stroke=sw)
    f = face(size)
    lh = int(size * 1.4)
    words_total = sum(len(l.split()) for l in lines)
    hl = (words_total - 1) if hl_index is None else min(max(hl_index, 0), words_total - 1)
    tw = int(max(f.length(_DUMMY, l) for l in lines)) + 2 * sw + 40
    pad = 26
    img = Image.new("RGBA", (tw + 2 * pad, lh * len(lines) + 30 + 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    sp = f.length(d, " ")
    k = 0
    for i, line in enumerate(lines):
        x = pad + (tw - f.length(d, line)) / 2
        for w in line.split():
            col = st.get("hl", "#fde047") if k == hl else st["fill"]
            f.draw(d, (x, pad + 10 + i * lh), w, fill=col, stroke_width=sw, stroke_fill=st["stroke"][1])
            x += f.length(d, w) + sp
            k += 1
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sh.putalpha(img.getchannel("A").point(lambda v: int(v * 0.6)))
    sh = ImageChops.offset(sh.filter(ImageFilter.GaussianBlur(9)), 0, 6)
    img = Image.alpha_composite(sh, img)
    ralign = lay.regions["bottom_caption"][4] if box == tuple(lay.cap_box) else "center"
    cx = (x0 + img.width / 2 - pad) if ralign == "left" else (x0 + x1) / 2
    cy = y1 + pad - img.height / 2 + 6     # bottom-anchored: the last line's baseline never moves
    return img, (cx, cy), {"size": size, "lines": lines, "w": img.width, "h": img.height,
                           "fits": (len(lines) * size * 1.34 <= (y1 - y0)) and tw <= (x1 - x0) + 40}


def render_scrim(box: tuple[int, int, int, int], strength: float = SCRIM_ALPHA, lay: "Layout | None" = None
                 ) -> tuple[Image.Image, tuple[float, float]] | None:
    """Soft dark gradient behind captions: full width, smoothstep ramp over >= SCRIM_MIN_H px, peak alpha capped at
    SCRIM_CAP (a thick outline and a text shadow carry legibility), so it never reads as a hard-edged rectangle.
    None when the layout draws no scrim (16:9 puts captions in a side column)."""
    lay = lay or get_layout("9x16")
    strength = min(float(strength), SCRIM_CAP)
    if strength <= 0 or not lay.scrim:
        return None
    x0, y0, x1, y1 = box
    bot = min(lay.h, y1 + 60)
    top = max(0, bot - max(SCRIM_MIN_H + 60, 480))
    h = bot - top
    up_len, dn_len = h - 60, 60
    vals = []
    for y in range(h):
        up, dn = min(1.0, y / float(up_len)), min(1.0, (h - y) / float(dn_len))
        vals.append(int(round(255 * strength * min(up * up * (3 - 2 * up), dn * dn * (3 - 2 * dn)))))
    col = Image.new("L", (1, h))
    col.putdata(vals)
    img = Image.new("RGBA", (lay.w, h), (12, 4, 10, 0))
    img.putalpha(col.resize((lay.w, h), Image.NEAREST))
    return img, (lay.w / 2, top + h / 2)


def render_pip(src_img: Image.Image, crop: list | None, size: tuple[int, int], rot: float,
               border: int = 14) -> Image.Image:
    """PiP card: cropped image, rounded, white border, soft shadow, rotated. Returned image carries the shadow margin."""
    im = src_img.convert("RGB")
    if crop:
        im = im.crop(tuple(int(v) for v in crop))
    # cover-fit to the requested size
    s = max(size[0] / im.width, size[1] / im.height)
    im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
    l, t = (im.width - size[0]) // 2, (im.height - size[1]) // 2
    im = im.crop((l, t, l + size[0], t + size[1]))
    m = Image.new("L", im.size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=26, fill=255)
    card = Image.new("RGBA", (im.width + 2 * border, im.height + 2 * border), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle([0, 0, card.width - 1, card.height - 1], radius=26 + border, fill="#ffffff")
    card.paste(im, (border, border), m)
    pad = 48
    sh = Image.new("RGBA", (card.width + 2 * pad, card.height + 2 * pad), (0, 0, 0, 0))
    sh.paste((0, 0, 0, 120), (pad, pad + 12), card.getchannel("A"))
    sh = sh.filter(ImageFilter.GaussianBlur(14))
    sh.alpha_composite(card, (pad, pad))
    if rot:
        sh = sh.rotate(rot, expand=True, resample=Image.BICUBIC)
    return sh


# ---------- animation ----------
def ease_out_back(t: float, s: float = 1.9) -> float:
    t -= 1
    return 1 + (s + 1) * t ** 3 + s * t ** 2


def ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


ANIMATIONS = ("pop", "slide_left", "slide_right", "fade", "none")


def anim_frames(img: Image.Image, center: tuple[float, float], kind: str, n: int = 9) -> list[tuple[Image.Image, tuple[int, int]]]:
    """Entrance as n RGBA frames with top-left positions. The LAST frame is the resting pose (held by the renderer)."""
    if kind == "none":
        n = 1
    out = []
    for k in range(n):
        p = (k + 1) / n
        a = min(1.0, p * 1.6) if kind != "none" else 1.0
        sc, dx = 1.0, 0
        if kind == "pop":
            sc = 0.55 + 0.45 * ease_out_back(p)
        elif kind == "slide_left":
            dx = -int(260 * (1 - ease_out(p)))
        elif kind == "slide_right":
            dx = int(260 * (1 - ease_out(p)))
        im = img.resize((max(1, int(img.width * sc)), max(1, int(img.height * sc))), Image.LANCZOS) if abs(sc - 1) > 1e-6 else img.copy()
        if a < 1:
            im.putalpha(im.getchannel("A").point(lambda v, a=a: int(v * a)))
        out.append((im, (int(round(center[0] - im.width / 2 + dx)), int(round(center[1] - im.height / 2)))))
    return out


def union_canvas(frames: list[tuple[Image.Image, tuple[int, int]]], canvas: tuple[int, int] = (W, H)) -> tuple[int, int, int, int]:
    """Even-sized bbox (x, y, w, h) covering every frame, clipped to the canvas."""
    x0 = max(0, min(p[0] for _, p in frames))
    y0 = max(0, min(p[1] for _, p in frames))
    x1 = min(canvas[0], max(p[0] + im.width for im, p in frames))
    y1 = min(canvas[1], max(p[1] + im.height for im, p in frames))
    w, h = x1 - x0, y1 - y0
    w += w % 2
    h += h % 2
    return x0, y0, w, h


def save_sequence(frames, workdir: Path, name: str, canvas: tuple[int, int] = (W, H)) -> tuple[list[Path], tuple[int, int, int, int]]:
    """Write frames on a shared (union) canvas as PNGs. Returns (paths, (x,y,w,h))."""
    x0, y0, cw, ch = union_canvas(frames, canvas)
    paths = []
    for k, (im, (x, y)) in enumerate(frames):
        cv = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        cv.alpha_composite(im, (x - x0, y - y0)) if (x - x0 >= 0 and y - y0 >= 0) else cv.paste(im, (x - x0, y - y0), im)
        p = workdir / f"{name}_{k:02d}.png"
        cv.save(p)
        paths.append(p)
    return paths, (x0, y0, cw, ch)

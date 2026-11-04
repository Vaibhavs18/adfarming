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
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .cuts import Face  # mixed-script face: Devanagari run -> Noto Sans Devanagari, Latin run -> Noto Sans

W, H = 1080, 1920
SAFE = {"top": 230, "bottom": 1574, "left": 54, "right": 1026}   # x0,y0,x1,y1 = left, top, right, bottom
SAFE_BOX = (SAFE["left"], SAFE["top"], SAFE["right"], SAFE["bottom"])
# region name -> (x0, y0, x1, y1, align)
REGIONS: dict[str, tuple[int, int, int, int, str]] = {
    "top_center": (54, 250, 1026, 600, "center"),
    "cta_top": (54, 250, 1026, 430, "center"),
    "left_mid": (54, 640, 470, 1100, "left"),
    "right_mid": (610, 640, 1026, 1100, "right"),
    "bottom_caption": (54, 1300, 1026, 1570, "center"),
}
# default role look by region (planner may override through text.style)
ROLE_BY_REGION = {"top_center": "hook", "cta_top": "cta", "left_mid": "callout", "right_mid": "callout",
                  "bottom_caption": "caption"}
ROLE_STYLE: dict[str, dict] = {
    "hook": {"size": 108, "fill": "#ffffff", "stroke": [0, "#000000"], "bg": {"fill": "#1c0714", "alpha": 215, "radius": 56, "pad": 34},
             "shadow": True, "animation": "pop", "max_lines": 3},
    "cta": {"size": 58, "fill": "#ffffff", "stroke": [0, "#000000"], "bg": {"fill": "#e11d48", "radius": 80, "pad": 30},
            "shadow": True, "animation": "pop", "max_lines": 1},
    "callout": {"size": 64, "fill": "#2a0a1a", "stroke": [0, "#000000"], "bg": {"fill": "#fbbf24", "radius": 30, "pad": 26},
                "rot": -6, "tail": "auto", "shadow": True, "animation": "slide_left", "max_lines": 3},
    "caption": {"size": 84, "fill": "#ffffff", "stroke": [9, "#1a0a14"], "hl": "#fde047", "animation": "pop", "max_lines": 2},
}
SCRIM_ALPHA = 0.62   # peak darkness of the caption scrim band


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
            return lines, s
        s -= 2


# ---------- styles ----------
STYLE_ROLE = {"pill_headline": "hook", "callout_tag": "callout", "cta_pill": "cta"}   # composition-plan style enum -> look


def resolve_style(item: dict) -> dict:
    """Merge the preset look with the item's own fields. Accepts the composition-plan schema (style enum string,
    plate hex|null, pill_text, stroke [w,hex]|null, fill, rot, animation) and, for tests/tools, a style dict."""
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
        base["pill"] = {"text": item["pill_text"], "fill": "#fbbf24", "color": "#2a0a1a", "size": 38}
    sw = base.get("stroke") or [0, "#000000"]
    base["stroke"] = [int(sw[0]), sw[1]]
    base["role"] = role
    base["region"] = region
    return base


def text_box(item: dict) -> tuple[int, int, int, int]:
    """Placement box in design px: an explicit item['box'] else the named region."""
    if item.get("box"):
        return tuple(int(v) for v in item["box"])  # type: ignore[return-value]
    r = REGIONS.get(item.get("region", "top_center"), REGIONS["top_center"])
    return r[:4]


# ---------- block artwork ----------
def _draw_lines(d: ImageDraw.ImageDraw, f: Face, lines: list[str], cx: float, y: float, lh: int, *,
                fill, stroke_w=0, stroke_fill=None, align="center", left=0.0, right=0.0) -> None:
    for i, line in enumerate(lines):
        lw = f.length(d, line)
        x = cx - lw / 2 if align == "center" else (left if align == "left" else right - lw)
        kw = {"fill": fill}
        if stroke_w:
            kw.update(stroke_width=stroke_w, stroke_fill=stroke_fill)
        f.draw(d, (x, y + i * lh), line, **kw)


def render_block(item: dict) -> tuple[Image.Image, tuple[float, float], dict]:
    """Static RGBA art for a text block + (cx, cy) placement centre + info {size, lines, fits, w, h, box}.
    The image includes its own padding for the drop shadow and tail."""
    st = resolve_style(item)
    x0, y0, x1, y1 = text_box(item)
    region = st["region"]
    ralign = REGIONS.get(region, ("", "", "", "", "center"))[4] if region in REGIONS else "center"
    bg = st.get("bg")
    pad = int(bg["pad"]) if bg else 0
    sw = st["stroke"][0]
    pill = st.get("pill")
    ph = (int(pill.get("size", 38)) + 38) if pill else 0
    chip_gap = 20 if pill else 0
    tail = st.get("tail", "none")
    if tail == "auto":
        tail = ("down_right" if region == "left_mid" else "down_left") if (bg and region in ("left_mid", "right_mid")) else "none"
    tail_h = 46 if tail not in ("none", None) else 0
    margin = 30
    max_w = (x1 - x0) - 2 * (pad + margin)
    max_h = (y1 - y0) - 2 * pad - ph - chip_gap - tail_h
    lines, size = fit_text(item["text"], int(st["size"]), max_w, max(max_h, 40), int(st.get("max_lines", 3)), stroke=sw)
    f = face(size)
    lh = int(size * 1.34)
    tw = int(max(f.length(_DUMMY, l) for l in lines)) + 2 * sw
    cw, bh = tw + 2 * pad + (24 if bg else 0), lh * len(lines) + 2 * pad
    fits = (tw + 2 * pad) <= (x1 - x0) and (bh + ph + chip_gap + tail_h) <= (y1 - y0)
    pw = 0
    if pill:
        pf = face(int(pill.get("size", 38)))
        pw = int(pf.length(_DUMMY, pill["text"])) + 70
        cw = max(cw, pw)
    img = Image.new("RGBA", (cw + 2 * margin, bh + ph + chip_gap + tail_h + 2 * margin), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ox, oy = margin, margin
    if pill:
        px0 = ox + (cw - pw) // 2
        d.rounded_rectangle([px0, oy, px0 + pw, oy + ph], radius=ph // 2, fill=pill.get("fill", "#fbbf24"))
        face(int(pill.get("size", 38))).draw(d, (px0 + 35, oy + 19), pill["text"], fill=pill.get("color", "#2a0a1a"))
        oy += ph + chip_gap
    if st.get("shadow"):
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        if bg:
            sd.rounded_rectangle([ox, oy + 12, ox + cw, oy + bh + 12], radius=int(bg["radius"]), fill=(0, 0, 0, 120))
        else:
            _draw_lines(sd, f, lines, ox + cw / 2 + 5, oy + pad + 9, lh, fill=(0, 0, 0, 170), align="center")
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(10)))
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
    _draw_lines(d, f, lines, ox + cw / 2, oy + pad, lh, fill=st["fill"], stroke_w=sw, stroke_fill=st["stroke"][1])
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
    cy = (y0 + y1) / 2
    info = {"size": size, "lines": lines, "fits": fits, "w": img.width, "h": img.height,
            "box": (cx - img.width / 2 + margin, cy - img.height / 2 + margin, cx + img.width / 2 - margin, cy + img.height / 2 - margin), "role": st["role"],
            "tail": tail, "ink_w": tw + 2 * pad, "ink_h": bh + ph + chip_gap + tail_h}
    return img, (cx, cy), info


def render_caption(text: str, st: dict, box: tuple[int, int, int, int], hl_index: int | None = None
                   ) -> tuple[Image.Image, tuple[float, float], dict]:
    """One caption chunk: white words with dark stroke, one highlighted word (default the last)."""
    sw = st["stroke"][0]
    x0, y0, x1, y1 = box
    max_w = (x1 - x0) - 40
    lines, size = fit_text(text, int(st["size"]), max_w, y1 - y0 - 20, int(st.get("max_lines", 2)), stroke=sw)
    f = face(size)
    lh = int(size * 1.4)
    words_total = sum(len(l.split()) for l in lines)
    hl = (words_total - 1) if hl_index is None else min(max(hl_index, 0), words_total - 1)
    tw = int(max(f.length(_DUMMY, l) for l in lines)) + 2 * sw + 40
    img = Image.new("RGBA", (tw, lh * len(lines) + 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    sp = f.length(d, " ")
    k = 0
    for i, line in enumerate(lines):
        x = (tw - f.length(d, line)) / 2
        for w in line.split():
            col = st.get("hl", "#fde047") if k == hl else st["fill"]
            f.draw(d, (x, 10 + i * lh), w, fill=col, stroke_width=sw, stroke_fill=st["stroke"][1])
            x += f.length(d, w) + sp
            k += 1
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return img, (cx, cy), {"size": size, "lines": lines, "w": img.width, "h": img.height,
                           "fits": (len(lines) * size * 1.34 <= (y1 - y0)) and tw <= (x1 - x0) + 40}


def render_scrim(box: tuple[int, int, int, int], strength: float = SCRIM_ALPHA) -> tuple[Image.Image, tuple[float, float]]:
    """Dark gradient band behind captions: full width, soft top/bottom edges, so white text stays readable
    over busy saree/hands. Extends 260 px above and 40 px below the caption box."""
    x0, y0, x1, y1 = box
    top, bot = y0 - 260, min(H, y1 + 40)
    h = bot - top
    vals = []
    for y in range(h):  # smoothstep ramp up over the first 260 px, down over the last 40 px
        up, dn = min(1.0, y / 260.0), min(1.0, (h - y) / 40.0)
        vals.append(int(255 * strength * min(up * up * (3 - 2 * up), dn * dn * (3 - 2 * dn))))
    col = Image.new("L", (1, h))
    col.putdata(vals)
    band = col.resize((W, h), Image.NEAREST)
    img = Image.new("RGBA", (W, h), (12, 4, 10, 0))
    img.putalpha(band)
    return img, (W / 2, top + h / 2)


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


def union_canvas(frames: list[tuple[Image.Image, tuple[int, int]]]) -> tuple[int, int, int, int]:
    """Even-sized bbox (x, y, w, h) covering every frame, clipped to the canvas."""
    x0 = max(0, min(p[0] for _, p in frames))
    y0 = max(0, min(p[1] for _, p in frames))
    x1 = min(W, max(p[0] + im.width for im, p in frames))
    y1 = min(H, max(p[1] + im.height for im, p in frames))
    w, h = x1 - x0, y1 - y0
    w += w % 2
    h += h % 2
    return x0, y0, w, h


def save_sequence(frames, workdir: Path, name: str) -> tuple[list[Path], tuple[int, int, int, int]]:
    """Write frames on a shared (union) canvas as PNGs. Returns (paths, (x,y,w,h))."""
    x0, y0, cw, ch = union_canvas(frames)
    paths = []
    for k, (im, (x, y)) in enumerate(frames):
        cv = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        cv.alpha_composite(im, (x - x0, y - y0)) if (x - x0 >= 0 and y - y0 >= 0) else cv.paste(im, (x - x0, y - y0), im)
        p = workdir / f"{name}_{k:02d}.png"
        cv.save(p)
        paths.append(p)
    return paths, (x0, y0, cw, ch)

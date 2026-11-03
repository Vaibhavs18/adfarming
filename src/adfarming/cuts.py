"""Assembler agent: concept beats -> master cut -> 9:16 / 1:1 / 16:9 with burned-in captions."""
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .footage import PRIV
from .trace import Run

FONT = "/System/Library/Fonts/Kohinoor.ttc"  # shapes Devanagari + Latin
SIZES = {"9x16": (1080, 1920), "1x1": (1080, 1080), "16x9": (1920, 1080)}
CAP_Y = {"9x16": 0.70, "1x1": 0.72, "16x9": 0.76}  # caption top as fraction of height
CAP_W = {"9x16": 0.88, "1x1": 0.88, "16x9": 0.60}  # max caption width as fraction of width
CAP_PT = {"9x16": 62, "1x1": 58, "16x9": 60}


def ff(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def caption_chunks(concept: dict, clips: dict[str, dict], words_per_chunk: int = 5) -> list[dict]:
    """Timed caption chunks on the OUTPUT timeline, from Voice Transcribe turns inside each beat."""
    chunks, offset = [], 0.0
    for b in concept["beats"]:
        for t in clips[b["clip"]]["turns"]:
            lo, hi = max(t["start"], b["src_in"]), min(t["end"], b["src_out"])
            if hi - lo < 0.2:
                continue
            words = t["text"].split()
            span = t["end"] - t["start"]
            a, z = round(len(words) * (lo - t["start"]) / span), round(len(words) * (hi - t["start"]) / span)
            words = words[a:z] or words
            n = max(1, round(len(words) / words_per_chunk))
            per = (hi - lo) / n
            size = -(-len(words) // n)
            for k in range(n):
                part = " ".join(words[k * size:(k + 1) * size])
                if part:
                    chunks.append({"text": part, "start": round(offset + (lo - b["src_in"]) + k * per, 2),
                                   "end": round(offset + (lo - b["src_in"]) + (k + 1) * per, 2)})
        offset += b["src_out"] - b["src_in"]
    return chunks


def render_caption(text: str, ratio: str, path: Path) -> int:
    W, H = SIZES[ratio]
    font = ImageFont.truetype(FONT, CAP_PT[ratio])
    maxw = int(W * CAP_W[ratio])
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines, cur = [], ""
    for w in text.split():
        trial = f"{cur} {w}".strip()
        if d.textlength(trial, font=font) <= maxw or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    lh = int(CAP_PT[ratio] * 1.55)
    im = Image.new("RGBA", (W, lh * len(lines) + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        x = (W - d.textlength(line, font=font)) / 2
        d.text((x, 6 + i * lh), line, font=font, fill="white", stroke_width=5, stroke_fill="black")
    im.save(path)
    return im.size[1]


def master(run: Run, concept: dict, work: Path) -> Path:
    segs = []
    for i, b in enumerate(concept["beats"], 1):
        t0, seg = time.time(), work / f"seg{i}.mp4"
        ff(["-ss", str(b["src_in"]), "-to", str(b["src_out"]), "-i", str(PRIV / "footage" / b["clip"]),
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1",
            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-c:a", "aac", "-ar", "48000", "-ac", "2",
            str(seg)])
        segs.append(seg)
        run.trace("ffmpeg", {"clip": b["clip"], "in": b["src_in"], "out": b["src_out"]}, {"seg": seg.name},
                  started=t0, artifacts=[str(seg)], decision=f"cut beat {i}: {b['purpose']}")
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{s.name}'\n" for s in segs))
    out = work / "master_9x16.mp4"
    t0 = time.time()
    ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    run.trace("ffmpeg", {"segments": len(segs)}, {"master": out.name}, started=t0, artifacts=[str(out)],
              decision="concatenated beats into master 9:16 cut")
    return out


def variant(run: Run, src: Path, ratio: str, chunks: list[dict], work: Path, out_path: Path,
            crop_y: int = 160) -> None:
    W, H = SIZES[ratio]
    t0 = time.time()
    if ratio == "9x16":
        base = "[0:v]null[v0]"
    elif ratio == "1x1":
        base = f"[0:v]crop=1080:1080:0:{crop_y}[v0]"
    else:
        base = ("[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,gblur=sigma=40[bg];"
                "[0:v]scale=-2:1080[fg];[bg][fg]overlay=(W-w)/2:0[v0]")
    inputs, chain, last = ["-i", str(src)], [base], "v0"
    for i, c in enumerate(chunks, 1):
        png = work / f"cap_{ratio}_{i}.png"
        render_caption(c["text"], ratio, png)
        inputs += ["-i", str(png)]
        nxt = f"v{i}"
        chain.append(f"[{last}][{i}:v]overlay=0:{int(H * CAP_Y[ratio])}:"
                     f"enable='between(t,{c['start']},{c['end']})'[{nxt}]")
        last = nxt
    ff([*inputs, "-filter_complex", ";".join(chain), "-map", f"[{last}]", "-map", "0:a",
        "-c:v", "libx264", "-crf", "21", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-movflags", "+faststart", str(out_path)])
    run.trace("ffmpeg", {"ratio": ratio, "captions": len(chunks)}, {"file": out_path.name}, started=t0,
              artifacts=[str(out_path)],
              decision={"9x16": "native vertical for Reels/Stories",
                        "1x1": f"center crop, y-offset {crop_y}px to keep face in frame",
                        "16x9": "blurred-fill side bars, subject kept uncropped"}[ratio] + "; captions burned in")


def build(run: Run, concept: dict, clips: list[dict], crop_y: int = 160) -> dict:
    work = run.dir / "work"
    work.mkdir(exist_ok=True)
    by_file = {c["file"]: c for c in clips}
    chunks = caption_chunks(concept, by_file)
    m = master(run, concept, work)
    outs = {}
    for ratio in SIZES:
        p = run.dir / f"{concept['id']}_{ratio}.mp4"
        variant(run, m, ratio, chunks, work, p, crop_y)
        outs[ratio] = p.name
    run.contract("assembler", "human", {"concept": concept["id"], "captions": chunks}, {"cuts": outs})
    return {"cuts": outs, "captions": chunks}

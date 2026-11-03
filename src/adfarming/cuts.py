"""Assembler agent: concept beats -> speech-snapped master -> 9:16 / 1:1 / 16:9 with captions and hook text."""
import re
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .footage import PRIV
from .llm import spark_json
from .trace import Run

FONT = "/System/Library/Fonts/Kohinoor.ttc"  # shapes Devanagari + Latin
SIZES = {"9x16": (1080, 1920), "1x1": (1080, 1080), "16x9": (1920, 1080)}
CAP_Y = {"9x16": 0.70, "1x1": 0.72, "16x9": 0.76}   # caption top as fraction of height
TITLE_Y = {"9x16": 0.09, "1x1": 0.06, "16x9": 0.07}
CAP_W = {"9x16": 0.88, "1x1": 0.88, "16x9": 0.60}   # max text width as fraction of width
CAP_PT = {"9x16": 62, "1x1": 58, "16x9": 60}
TITLE_PT = {"9x16": 58, "1x1": 52, "16x9": 54}
SNAP_WINDOW = 1.2
HOLD_S = 2.0  # clean end hold: last frame + CTA only, no speech


def ff(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


# ---------- speech structure ----------
_speech_cache: dict[str, list[tuple[float, float]]] = {}


def speech_intervals(clip: dict) -> list[tuple[float, float]]:
    """Speech (non-silent) intervals of a clip, from ffmpeg silencedetect."""
    f = clip["file"]
    if f in _speech_cache:
        return _speech_cache[f]
    p = subprocess.run(["ffmpeg", "-v", "info", "-i", str(PRIV / "footage" / f), "-vn",
                        "-af", "silencedetect=noise=-32dB:d=0.12", "-f", "null", "-"],
                       capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", p.stderr)]
    sil = list(zip(starts, ends + [clip["duration_s"]] * (len(starts) - len(ends))))
    out, cur = [], 0.0
    for s, e in sil:
        if s - cur > 0.05:
            out.append((cur, s))
        cur = e
    if clip["duration_s"] - cur > 0.05:
        out.append((cur, clip["duration_s"]))
    _speech_cache[f] = out
    return out


DANGLING = {"and", "but", "so", "or", "the", "a", "to", "एंड", "बट", "सो", "और", "या", "का", "के", "की"}


def word_times(clip: dict, turn: dict) -> list[tuple[str, float, float]]:
    """(word, start, end) per word, spread over the turn's actual speech time (silences excluded)."""
    segs = [(max(a, turn["start"]), min(b, turn["end"])) for a, b in speech_intervals(clip)
            if min(b, turn["end"]) - max(a, turn["start"]) > 0.03]
    words, total = turn["text"].split(), sum(b - a for a, b in segs)
    if not words or total <= 0:
        return []

    def at(sec: float) -> float:
        for a, b in segs:
            if sec <= b - a:
                return a + sec
            sec -= b - a
        return segs[-1][1]

    n = len(words)
    return [(w, at(total * i / n), at(total * (i + 1) / n)) for i, w in enumerate(words)]


def trim_dangling(run: Run, concept: dict, clips: dict[str, dict]) -> None:
    """If the final beat ends on a connective/stutter ('...comfortable and'), cut before it."""
    b = concept["beats"][-1]
    clip, t0 = clips[b["clip"]], time.time()
    inside = [(w, s, e) for t in clip["turns"] for (w, s, e) in word_times(clip, t) if s >= b["src_in"] and e <= b["src_out"] + 0.2]
    cut = None
    while len(inside) > 3 and inside[-1][0].strip(".,!?").lower() in DANGLING:
        cut = inside.pop()
    if cut:
        old = b["src_out"]
        b["src_out"] = round(inside[-1][2] + 0.12, 2)
        run.trace("trim-dangling", {"was": old}, {"now": b["src_out"]}, started=t0,
                  decision=f"final beat ended on dangling word '{cut[0]}'; cut at {b['src_out']:.2f}s so the line lands")


def snap_beats(run: Run, concept: dict, clips: dict[str, dict]) -> None:
    """Move each cut point to the nearest speech onset/offset so no word is clipped."""
    for i, b in enumerate(concept["beats"], 1):
        t0, sp, dur = time.time(), speech_intervals(clips[b["clip"]]), clips[b["clip"]]["duration_s"]
        old = (b["src_in"], b["src_out"])
        onsets = [s for s, _ in sp if abs(s - b["src_in"]) <= SNAP_WINDOW]
        offsets = [e for _, e in sp if abs(e - b["src_out"]) <= SNAP_WINDOW]
        if onsets:
            b["src_in"] = max(0.0, min(onsets, key=lambda x: abs(x - b["src_in"])) - 0.05)
        if offsets:
            b["src_out"] = min(dur, min(offsets, key=lambda x: abs(x - b["src_out"])) + 0.12)
        for prev in concept["beats"][:i - 1]:
            if prev["clip"] == b["clip"] and b["src_in"] < prev["src_out"] and b["src_out"] > prev["src_in"]:
                b["src_in"] = max(b["src_in"], prev["src_out"])
        if b["src_out"] - b["src_in"] < 1.5:
            b["src_in"], b["src_out"] = old
        run.trace("snap-to-speech", {"beat": i, "was": list(old)}, {"now": [round(b["src_in"], 2), round(b["src_out"], 2)]},
                  started=t0, decision=f"beat {i}: cut {old[0]:.2f}-{old[1]:.2f} -> "
                  f"{b['src_in']:.2f}-{b['src_out']:.2f} so no word is clipped")


def caption_chunks(concept: dict, clips: dict[str, dict], words_per_chunk: int = 4) -> list[dict]:
    """Caption chunks on the OUTPUT timeline; word times come from real speech, not silences."""
    chunks, offset = [], 0.0
    for b in concept["beats"]:
        clip = clips[b["clip"]]
        for t in clip["turns"]:
            wt = [(w, s, e) for (w, s, e) in word_times(clip, t) if b["src_in"] - 0.02 <= (s + e) / 2 <= b["src_out"] + 0.02]
            for k in range(0, len(wt), words_per_chunk):
                part = wt[k:k + words_per_chunk]
                a, z = max(part[0][1], b["src_in"]), min(part[-1][2], b["src_out"])
                chunks.append({"text": " ".join(w for w, _, _ in part),
                               "start": round(offset + a - b["src_in"], 2), "end": round(offset + z - b["src_in"], 2)})
        offset += b["src_out"] - b["src_in"]
    for cur, nxt in zip(chunks, chunks[1:]):
        cur["end"] = min(cur["end"], nxt["start"] - 0.02)
    return [c for c in chunks if c["end"] - c["start"] > 0.15]


def normalise_captions(run: Run, chunks: list[dict]) -> list[dict]:
    """One script for every caption: Roman-script Hinglish, same words as spoken, no new content."""
    t0 = time.time()
    try:
        out, usage = spark_json(
            "You normalise burned-in captions for Indian Reels. Return ONLY JSON.",
            "Rewrite each caption line in Roman-script Hinglish (Latin letters), keeping exactly the same words and "
            "meaning, fixing obvious speech-recognition misspellings, removing stutter repeats. Do not add or drop "
            "ideas. Keep the same number of lines in the same order.\n"
            f'Return {{"lines": [...]}}.\nLINES: {[c["text"] for c in chunks]}', effort="low", max_out=4000)
        lines = out["lines"]
        if len(lines) != len(chunks) or not all(isinstance(x, str) and x.strip() for x in lines):
            raise ValueError("line count mismatch")
    except Exception as e:
        run.trace("muse-spark", {"captions": len(chunks)}, {"error": str(e)[:100]}, started=t0, status="skipped",
                  decision="caption normalisation skipped, keeping raw transcript")
        return chunks
    run.trace("muse-spark", {"captions": len(chunks)}, out, started=t0,
              decision=f"normalised {len(chunks)} captions to Roman-script Hinglish; tokens={usage.get('tokens')}")
    return [{**c, "text": t.strip(), "raw": c["text"]} for c, t in zip(chunks, lines)]


def title_items(concept: dict) -> list[dict]:
    items, offset = [], 0.0
    for b in concept["beats"]:
        ln = b["src_out"] - b["src_in"]
        if b.get("on_screen_text") and b["on_screen_text"].strip() != (concept.get("cta") or "").strip():
            items.append({"text": b["on_screen_text"], "start": round(offset + 0.1, 2), "end": round(offset + min(ln, 3.2), 2)})
        offset += ln
    if concept.get("cta") and offset > 3:
        items.append({"text": concept["cta"], "start": round(offset + 0.1, 2), "end": round(offset + HOLD_S, 2)})
    return items


# ---------- rendering ----------
def _wrap(d: ImageDraw.ImageDraw, text: str, font, maxw: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        trial = f"{cur} {w}".strip()
        if d.textlength(trial, font=font) <= maxw or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    return lines + [cur]


def render_caption(text: str, ratio: str, path: Path) -> None:
    W, _ = SIZES[ratio]
    font = ImageFont.truetype(FONT, CAP_PT[ratio])
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines, lh = _wrap(d, text, font, int(W * CAP_W[ratio])), int(CAP_PT[ratio] * 1.55)
    im = Image.new("RGBA", (W, lh * len(lines) + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        d.text(((W - d.textlength(line, font=font)) / 2, 6 + i * lh), line, font=font,
               fill="white", stroke_width=5, stroke_fill="black")
    im.save(path)


def render_title(text: str, ratio: str, path: Path) -> None:
    W, _ = SIZES[ratio]
    font = ImageFont.truetype(FONT, TITLE_PT[ratio])
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines, lh = _wrap(d, text, font, int(W * 0.78)), int(TITLE_PT[ratio] * 1.5)
    tw = int(max(d.textlength(l, font=font) for l in lines)) + 64
    im = Image.new("RGBA", (W, lh * len(lines) + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x0 = (W - tw) // 2
    d.rounded_rectangle([x0, 4, x0 + tw, lh * len(lines) + 30], radius=28, fill=(15, 15, 15, 200))
    for i, line in enumerate(lines):
        d.text(((W - d.textlength(line, font=font)) / 2, 14 + i * lh), line, font=font, fill="white")
    im.save(path)


# ---------- ffmpeg ----------
def master(run: Run, concept: dict, work: Path) -> Path:
    segs = []
    for i, b in enumerate(concept["beats"], 1):
        t0, seg = time.time(), work / f"seg{i}.mp4"
        ff(["-ss", f"{b['src_in']:.3f}", "-to", f"{b['src_out']:.3f}", "-i", str(PRIV / "footage" / b["clip"]),
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1",
            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(seg)])
        segs.append(seg)
        run.trace("ffmpeg", {"clip": b["clip"], "in": round(b["src_in"], 2), "out": round(b["src_out"], 2)},
                  {"seg": seg.name}, started=t0, artifacts=[str(seg)], decision=f"cut beat {i}: {b['purpose']}")
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{s.name}'\n" for s in segs))
    out, t0 = work / "master_9x16.mp4", time.time()
    ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    run.trace("ffmpeg", {"segments": len(segs)}, {"master": out.name}, started=t0, artifacts=[str(out)],
              decision="concatenated beats into master 9:16 cut")
    held, t0 = work / "master_hold.mp4", time.time()
    ff(["-i", str(out), "-vf", f"tpad=stop_mode=clone:stop_duration={HOLD_S}", "-af", f"apad=pad_dur={HOLD_S}",
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-c:a", "aac", str(held)])
    run.trace("ffmpeg", {"hold_s": HOLD_S}, {"master": held.name}, started=t0, artifacts=[str(held)],
              decision=f"added {HOLD_S}s end hold so the CTA lands on a clean frame with no unfinished speech")
    return held


def variant(run: Run, src: Path, ratio: str, overlays: list[dict], work: Path, out_path: Path, crop_y: int, why: str) -> None:
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
    for i, o in enumerate(overlays, 1):
        png = work / f"ov_{ratio}_{i}.png"
        (render_title if o["kind"] == "title" else render_caption)(o["text"], ratio, png)
        y = int(H * (TITLE_Y if o["kind"] == "title" else CAP_Y)[ratio])
        inputs += ["-i", str(png)]
        chain.append(f"[{last}][{i}:v]overlay=0:{y}:enable='between(t,{o['start']},{o['end']})'[v{i}]")
        last = f"v{i}"
    ff([*inputs, "-filter_complex", ";".join(chain), "-map", f"[{last}]", "-map", "0:a", "-c:v", "libx264",
        "-crf", "21", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out_path)])
    run.trace("ffmpeg", {"ratio": ratio, "overlays": len(overlays)}, {"file": out_path.name}, started=t0,
              artifacts=[str(out_path)], decision=why)


def build(run: Run, concept: dict, clips: list[dict], track=None) -> dict:
    """track: callable(master_path) -> SAM stats dict (or None)."""
    work = run.dir / "work"
    work.mkdir(exist_ok=True)
    by_file = {c["file"]: c for c in clips}
    snap_beats(run, concept, by_file)
    trim_dangling(run, concept, by_file)
    chunks = normalise_captions(run, caption_chunks(concept, by_file))
    titles = title_items(concept)
    m = master(run, concept, work)
    stats = track(m) if track else None
    if stats and stats.get("frames"):
        crop_y = max(0, min(840, int(stats["top_frac"] * 1920 - 0.14 * 1080)))
        crop_why = f"SAM 3.1 head-top at {stats['top_frac']:.0%} of height, so square crop starts y={crop_y}"
    else:
        crop_y, crop_why = 160, "fixed y=160 crop (SAM unavailable)"
    overlays = [{**t, "kind": "title"} for t in titles] + [{**c, "kind": "caption"} for c in chunks]
    why = {"9x16": "native vertical for Reels/Stories", "1x1": crop_why,
           "16x9": "blurred-fill side bars, subject kept uncropped"}
    outs = {}
    for ratio in SIZES:
        p = run.dir / f"{concept['id']}_{ratio}.mp4"
        variant(run, m, ratio, overlays, work, p, crop_y, why[ratio] + "; captions + hook text burned in")
        outs[ratio] = p.name
    run.contract("assembler", "qc", {"concept": concept["id"], "captions": chunks, "titles": titles}, {"cuts": outs})
    return {"cuts": outs, "captions": chunks, "titles": titles, "master": str(m)}

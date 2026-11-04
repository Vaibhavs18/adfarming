"""Subject matte extractor: SAM 3.1 person mask -> frame-aligned grayscale matte video (and single-still alpha PNG).

Pipeline (video): downscale+resample to sample_fps -> upload -> SAM text-prompt stream -> parse one box+mask per frame ->
decode BOX-sized rasters in a process pool (placed at x1+x, y1+y) -> interpolate alpha between sampled frames to the
source fps -> pipe to ffmpeg (bicubic upscale to source size + gblur + curves feather) -> grayscale H.264 matte.

Never raises through: every failure returns {"error": ...}. Results are cached by sha256(video bytes + params).
"""
import base64
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid
import multiprocessing as mp
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from .llm import META, ROOT, _hdr
from .trace import Run

COST_PER_FRAME_USD = 0.0002   # listed $0.20 / 1k frames, 50-frame minimum per request (unverified vs billing)
COST_PER_IMAGE_USD = 0.0025   # listed $2.50 / 1k images
FEATHER_SIGMA = 2.0           # at 1080 px wide; scaled for other widths
EDGE = (0.35, 0.65)           # smoothstep-like curve bounds from the spike
FALLBACK_PROMPT = "person"

_BOX = re.compile(r"(?:<(\d+)f>\d*)?<\|box;x1=(-?\d+);y1=(-?\d+);x2=(-?\d+);y2=(-?\d+);w=(\d+);h=(\d+)\|>"
                  r"<\|mask;x=(-?\d+);y=(-?\d+)(?:;c=[\d.]+)?;data=(\d+),(\d+),(.)(.*?)\|>", re.S)


class MatteError(Exception):
    pass


def _cache_root(cache_dir) -> Path:
    return Path(cache_dir) if cache_dir else Path(os.getenv("ADF_OUT", str(ROOT / "out"))) / "_cache" / "matte"


def _feather_filter(out_w: int) -> str:
    sigma = max(0.5, FEATHER_SIGMA * out_w / 1080)
    lo, hi = EDGE
    return f"gblur=sigma={sigma:.3f},curves=all='0/0 {lo}/0 {hi}/1 1/1',format=gray"


# ---------------------------------------------------------------- parsing + decoding (pure)

def parse_masks(text: str) -> list[dict]:
    """All box+mask tokens in a SAM stream text. frame is None for image responses without a frame prefix."""
    out = []
    for i, m in enumerate(_BOX.finditer(text)):
        g = m.groups()
        out.append({"frame": int(g[0]) if g[0] is not None else None, "idx": i,
                    "box": tuple(map(int, g[1:5])), "w": int(g[5]), "h": int(g[6]),
                    "mx": int(g[7]), "my": int(g[8]), "mh": int(g[9]), "mw": int(g[10]),
                    "enc": g[11], "payload": g[12]})
    return out


def select_largest(tokens: list[dict]) -> dict[int, dict]:
    """Largest-box match per frame (selection BEFORE decode: decode is the expensive part)."""
    best: dict[int, dict] = {}
    for t in tokens:
        f = t["frame"] if t["frame"] is not None else 0
        x1, y1, x2, y2 = t["box"]
        area = max(0, x2 - x1) * max(0, y2 - y1)
        if f not in best or area > best[f]["_area"]:
            best[f] = {**t, "_area": area}
    return best


def place_mask(raster: np.ndarray, box, mx: int, my: int, w: int, h: int) -> np.ndarray:
    """Put a BOX-sized raster into a (h, w) uint8 frame at (x1+mx, y1+my), clipping to the frame."""
    full = np.zeros((h, w), np.uint8)
    ox, oy = box[0] + mx, box[1] + my
    rh, rw = raster.shape
    sx0, sy0 = max(0, -ox), max(0, -oy)
    dx0, dy0 = max(0, ox), max(0, oy)
    ww, hh = min(rw - sx0, w - dx0), min(rh - sy0, h - dy0)
    if ww > 0 and hh > 0:
        full[dy0:dy0 + hh, dx0:dx0 + ww] = raster[sy0:sy0 + hh, sx0:sx0 + ww]
    return full


def _decode_one(tok: dict) -> tuple[int, np.ndarray]:
    """Worker: decode one mask payload and place it. Returns (frame, uint8 0..255 mask at the (w,h) SAM saw)."""
    from meta_sam_parser import _mask_codec as mc
    enc, payload = tok["enc"], tok["payload"]
    if enc == "~":
        raw = mc._decode_lossless_raster(enc + payload, tok["mw"], tok["mh"])
        ras = np.frombuffer(bytes(raw), np.uint8).reshape(tok["mh"], tok["mw"])
    else:
        ras = np.frombuffer(mc._decode_raster(enc + payload, tok["mw"], tok["mh"]), np.uint8).reshape(tok["mh"], tok["mw"])
        ras = (ras > 0).astype(np.uint8) * 255
    return tok["frame"] if tok["frame"] is not None else 0, place_mask(ras, tok["box"], tok["mx"], tok["my"], tok["w"], tok["h"])


DECODE_POOL_TIMEOUT_S = 600.0   # whole decode pool; on expiry the pool is terminated and MatteError raised


def available_cpus() -> int:
    """CPUs this process may actually use (container/cgroup affinity aware; os.cpu_count() elsewhere)."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def decode_procs(procs: int | None = None) -> int:
    """Worker count for mask decode. Default = env ADF_DECODE_PROCS (default 2) so a 2 vCPU container is not
    oversubscribed; an explicit procs argument overrides the env default. Always capped by the usable CPUs."""
    if procs is None:
        try:
            procs = int(os.getenv("ADF_DECODE_PROCS", "2"))
        except ValueError:
            procs = 2
    return max(1, min(int(procs), available_cpus()))


def decode_tokens(chosen: dict[int, dict], procs: int | None = None) -> dict[int, np.ndarray]:
    jobs = [chosen[k] for k in sorted(chosen)]
    procs = decode_procs(procs)
    if procs <= 1 or len(jobs) <= 1:
        res = [_decode_one(j) for j in jobs]
    else:
        ctx = mp.get_context("fork" if "fork" in mp.get_all_start_methods() else None)
        pool = None
        try:
            pool = ctx.Pool(min(procs, len(jobs)))
            res = pool.map_async(_decode_one, jobs, chunksize=2).get(timeout=DECODE_POOL_TIMEOUT_S)
        except mp.TimeoutError:
            raise MatteError(f"mask decode exceeded {DECODE_POOL_TIMEOUT_S:.0f}s")
        except Exception:  # pool trouble must not lose the matte; fall back to serial
            res = [_decode_one(j) for j in jobs]
        finally:
            if pool is not None:
                pool.terminate()
                pool.join()
    return dict(res)


def fit_masks(masks: dict[int, np.ndarray], size: tuple[int, int]) -> dict[int, np.ndarray]:
    """Resize any mask whose size differs from size=(w,h) (SAM may report w,h differing from what we sent)."""
    w, h = size
    out = {}
    for k, m in masks.items():
        if m.shape != (h, w):
            m = np.asarray(Image.fromarray(m).resize((w, h), Image.BILINEAR))
        out[k] = m
    return out


def interp_alpha(masks: dict[int, np.ndarray], sampled_pos: float, shape: tuple[int, int]) -> np.ndarray:
    """Float32 0..255 alpha at fractional sampled-frame position. Gaps and out-of-range positions hold the nearest
    available mask; between two masks the blend is linear."""
    keys = sorted(masks)
    if not keys:
        return np.zeros(shape, np.float32)
    if sampled_pos <= keys[0]:
        return masks[keys[0]].astype(np.float32)
    if sampled_pos >= keys[-1]:
        return masks[keys[-1]].astype(np.float32)
    import bisect
    j = bisect.bisect_right(keys, sampled_pos)
    a, b = keys[j - 1], keys[j]
    t = (sampled_pos - a) / (b - a)
    if t <= 0:
        return masks[a].astype(np.float32)
    return masks[a].astype(np.float32) * (1 - t) + masks[b].astype(np.float32) * t


# ---------------------------------------------------------------- ffmpeg helpers

def _probe(video: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                        "-show_entries", "stream=width,height,r_frame_rate,nb_read_packets,duration:stream_tags=rotate:stream_side_data=rotation",
                        "-of", "json", str(video)], capture_output=True, text=True, check=True, timeout=HELPER_TIMEOUT_S)
    s = json.loads(r.stdout)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    fps = float(num) / float(den or 1)
    w, h = int(s["width"]), int(s["height"])
    rot = 0
    for sd in s.get("side_data_list", []) or []:
        rot = int(sd.get("rotation", rot) or 0)
    if abs(rot) in (90, 270):  # ffmpeg autorotates; report display size
        w, h = h, w
    n = int(s.get("nb_read_packets") or 0) or int(round(float(s.get("duration") or 0) * fps))
    return {"w": w, "h": h, "fps": fps, "fps_str": s["r_frame_rate"], "frames": n}


HELPER_TIMEOUT_S = 120


def _ffmpeg(args: list[str], **kw):
    kw.setdefault("timeout", HELPER_TIMEOUT_S)
    return subprocess.run(["ffmpeg", "-y", "-v", "error", *args], **kw)


# ---------------------------------------------------------------- SAM call

def _sam_call(content_part: dict, prompt: str, deadline: float, mask_encoding: str = "one_bit") -> str:
    """Streams a SAM response and returns the concatenated delta text. Raises MatteError on budget/HTTP problems."""
    r = requests.post(META + "/responses", headers={**_hdr(), "Content-Type": "application/json",
                                                     "Accept": "text/event-stream"},
                      stream=True, timeout=(30, 120), json={
        "model": "sam-3.1", "stream": True, "metadata": {"mask_encoding": mask_encoding},
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": prompt}, content_part]}]})
    if r.status_code >= 400:
        raise MatteError(f"SAM HTTP {r.status_code}: {r.text[:160]}")
    parts = []
    try:
        for raw in r.iter_lines():
            if time.time() > deadline:
                raise MatteError("time budget exceeded while streaming SAM response")
            s = raw.decode("utf-8", "replace")
            if not s.startswith("data: "):
                continue
            if s[6:] == "[DONE]":
                break
            ev = json.loads(s[6:])
            if ev.get("type") == "response.output_text.delta":
                parts.append(ev["delta"])
            elif ev.get("type") in ("response.failed", "error"):
                raise MatteError(f"SAM stream error: {str(ev)[:160]}")
    finally:
        r.close()
    return "".join(parts)


def _upload(path: Path) -> str:
    with path.open("rb") as fh:
        up = requests.post(META + "/files", headers=_hdr(), data={"purpose": "user_data"}, timeout=180,
                           files={"file": ("clip.mp4", fh, "video/mp4")})
    if up.status_code >= 400:
        raise MatteError(f"upload HTTP {up.status_code}: {up.text[:160]}")
    return up.json()["id"]


def _delete_file(fid: str):
    try:
        requests.delete(META + f"/files/{fid}", headers=_hdr(), timeout=30)
    except Exception:
        pass


# ---------------------------------------------------------------- video matte

def _write_matte(masks: dict[int, np.ndarray], size_in: tuple[int, int], src: dict, out_path: Path,
                 sample_fps: float) -> int:
    """Interpolate to source fps and pipe through ffmpeg feather chain into a gray H.264 mp4. Returns frames written."""
    sw, sh = size_in
    n = src["frames"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{sw}x{sh}",
           "-framerate", src["fps_str"], "-i", "-",
           "-vf", f"scale={src['w']}:{src['h']}:flags=bicubic,{_feather_filter(src['w'])}",
           "-c:v", "libx264", "-crf", "10", "-preset", "veryfast", "-pix_fmt", "gray", "-r", src["fps_str"],
           "-movflags", "+faststart", "-an", str(out_path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(n):
            pos = (i / src["fps"]) * sample_fps
            a = interp_alpha(masks, pos, (sh, sw))
            p.stdin.write(np.clip(a + 0.5, 0, 255).astype(np.uint8).tobytes())
        p.stdin.close()
        err = p.stderr.read().decode(errors="replace")
        if p.wait(timeout=HELPER_TIMEOUT_S) != 0:
            raise MatteError(f"ffmpeg matte encode failed: {err[:200]}")
    except BrokenPipeError:
        raise MatteError(f"ffmpeg matte encode failed: {p.stderr.read().decode(errors='replace')[:200]}")
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        if p.stderr:
            p.stderr.close()
    return n


def _cache_key(video: Path, params: dict) -> str:
    h = hashlib.sha256()
    with video.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    h.update(json.dumps(params, sort_keys=True).encode())
    return h.hexdigest()


def _head_top(chosen: dict[int, dict]) -> float | None:
    vals = [t["box"][1] / t["h"] for t in chosen.values() if t["h"]]
    return round(statistics.median(vals), 4) if vals else None


def subject_stats(chosen: dict[int, dict]) -> dict:
    """Median subject box over the chosen per-frame tokens, normalised to the SAM frame and clamped to 0..1:
    {subject_bbox_frac: [x1,y1,x2,y2], head_top_frac (= bbox y1), head_size_frac (= bbox height)}. Values are None
    when there are no usable tokens. head_size_frac lets the renderer normalise subject scale between clips."""
    rows = []
    for t in chosen.values():
        if t.get("w") and t.get("h"):
            x1, y1, x2, y2 = t["box"]
            rows.append([x1 / t["w"], y1 / t["h"], x2 / t["w"], y2 / t["h"]])
    if not rows:
        return {"subject_bbox_frac": None, "head_top_frac": None, "head_size_frac": None}
    bb = [round(min(1.0, max(0.0, statistics.median(r[i] for r in rows))), 4) for i in range(4)]
    return {"subject_bbox_frac": bb, "head_top_frac": bb[1], "head_size_frac": round(max(0.0, bb[3] - bb[1]), 4)}


def _atomic_write(dest: Path, data: bytes | None = None, src: Path | None = None):
    """Write dest via a sibling temp file + os.replace so readers never see a partial file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        if src is not None:
            shutil.copyfile(src, tmp)
        else:
            tmp.write_bytes(data or b"")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def extract_matte(run: Run, video_path, out_path, *, prompt="woman", sample_fps=15, width=540, cache_dir=None,
                  budget_s: float = 300.0, procs: int | None = None) -> dict:
    """Returns {matte, frames, sampled_frames, cost_usd, head_top_frac, seconds, ...} or {"error": msg, ...}.
    The matte is a gray H.264 mp4 at the source size and fps, frame-aligned with video_path (255 = subject)."""
    t0 = time.time()
    video, out_path = Path(video_path), Path(out_path)
    stats: dict = {}
    fid = None
    sam_calls = 0        # attempts, incremented BEFORE each call: a failed/timed-out call may still be billed
    est_frames = 50      # billing estimate for failure paths, refined once the clip is probed
    try:
        if not video.exists():
            raise MatteError(f"video not found: {video}")
        src = _probe(video)
        est_frames = max(50, int(round(src["frames"] / (src["fps"] or 1) * sample_fps)))
        params = {"prompt": prompt, "sample_fps": sample_fps, "width": width, "size": [src["w"], src["h"]],
                  "fps": src["fps_str"], "v": 2}
        key = _cache_key(video, params)
        croot = _cache_root(cache_dir)
        cdir = croot / key
        if (cdir / "matte.mp4").exists() and (cdir / "meta.json").exists():
            meta = json.loads((cdir / "meta.json").read_text())
            _atomic_write(out_path, src=cdir / "matte.mp4")
            res = {**meta, "matte": str(out_path), "cost_usd": 0.0, "seconds": round(time.time() - t0, 2), "cached": True}
            run.trace("matte", {"video": video.name, "prompt": prompt}, res, started=t0, cost_usd=0.0,
                      decision="matte served from cache", artifacts=[str(out_path)])
            return res
        deadline = t0 + budget_s
        sw = min(width, src["w"])
        sw -= sw % 2
        with tempfile.TemporaryDirectory() as td:
            small = Path(td) / "sam_in.mp4"
            _ffmpeg(["-i", str(video), "-vf", f"fps={sample_fps},scale={sw}:-2", "-an", "-c:v", "libx264", "-crf", "26",
                     "-preset", "veryfast", "-movflags", "+faststart", str(small)], check=True, capture_output=True)
            sam_size = _probe(small)
            ts = time.time()
            fid = _upload(small)
            stats["upload_s"] = round(time.time() - ts, 2)
            chosen: dict[int, dict] = {}
            used_prompt = prompt
            for pr in [prompt] + ([FALLBACK_PROMPT] if prompt != FALLBACK_PROMPT else []):
                ts = time.time()
                sam_calls += 1
                text = _sam_call({"type": "input_video", "file_id": fid}, pr, deadline)
                stats["stream_s"] = round(time.time() - ts, 2)
                chosen = select_largest(parse_masks(text))
                used_prompt = pr
                if chosen:
                    break
            if not chosen:
                raise MatteError(f"no match for prompts {prompt!r}/{FALLBACK_PROMPT!r}")
        ts = time.time()
        masks = decode_tokens(chosen, procs)
        stats["decode_s"] = round(time.time() - ts, 2)
        size_in = (sam_size["w"], sam_size["h"])
        masks = fit_masks(masks, size_in)
        sampled = len(masks)
        if time.time() > deadline + 120:
            raise MatteError("time budget exceeded before matte write")
        ts = time.time()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "matte.mp4"
            frames = _write_matte(masks, size_in, src, tmp, sample_fps)
            cdir.mkdir(parents=True, exist_ok=True)
            meta = {"frames": frames, "sampled_frames": sampled, **subject_stats(chosen), "prompt": used_prompt,
                    "size": [src["w"], src["h"]], "fps": src["fps"]}
            _atomic_write(cdir / "matte.mp4", src=tmp)
            _atomic_write(cdir / "meta.json", data=json.dumps(meta).encode())  # meta last: marks the entry complete
            _atomic_write(out_path, src=tmp)
        stats["write_s"] = round(time.time() - ts, 2)
        cost = round(max(sampled, 50) * COST_PER_FRAME_USD * sam_calls, 4)
        res = {**meta, "matte": str(out_path), "cost_usd": cost, "seconds": round(time.time() - t0, 2), "cached": False,
               "timing": stats, "sam_calls": sam_calls}
        run.trace("sam-3.1-matte", {"video": video.name, "prompt": used_prompt, "sample_fps": sample_fps}, meta, started=t0,
                  cost_usd=cost, artifacts=[str(out_path)],
                  decision=f"matte '{used_prompt}': {sampled} sampled frames -> {frames} source frames in {res['seconds']}s")
        return res
    except Exception as e:  # never raise through
        msg = f"{type(e).__name__}: {str(e)[:200]}" if not isinstance(e, MatteError) else str(e)[:240]
        cost = round(est_frames * COST_PER_FRAME_USD * sam_calls, 4)  # money spent even though no matte came back
        res = {"error": msg, "matte": None, "frames": 0, "sampled_frames": 0, "cost_usd": cost, "sam_calls": sam_calls,
               "head_top_frac": None, "subject_bbox_frac": None, "head_size_frac": None,
               "seconds": round(time.time() - t0, 2)}
        try:
            run.trace("sam-3.1-matte", {"video": video.name, "prompt": prompt}, res, started=t0, status="error",
                      cost_usd=cost, decision="matte failed; composed looks need the simple cut fallback")
        except Exception:
            pass
        return res
    finally:
        if fid:
            _delete_file(fid)


# ---------------------------------------------------------------- still matte

def feather_still(mask: np.ndarray, out_size: tuple[int, int], out_png: Path):
    """Upscale a uint8 mask to out_size (w,h) and write an 8-bit gray alpha PNG via the same ffmpeg feather chain."""
    with tempfile.TemporaryDirectory() as td:
        small = Path(td) / "m.png"
        Image.fromarray(mask).save(small)
        tmp_png = Path(td) / "out.png"
        r = _ffmpeg(["-i", str(small), "-vf", f"scale={out_size[0]}:{out_size[1]}:flags=bicubic,{_feather_filter(out_size[0])}",
                     "-frames:v", "1", "-pix_fmt", "gray", str(tmp_png)], capture_output=True, text=True)
        if r.returncode != 0:
            raise MatteError(f"ffmpeg feather failed: {r.stderr[:200]}")
        _atomic_write(Path(out_png), src=tmp_png)


def extract_matte_still(image_path, out_png, *, prompt="woman", max_side: int = 960, budget_s: float = 120.0,
                        cutout_png=None) -> dict:
    """Cut ONE still via SAM image segmentation (base64 data URL). Writes an 8-bit gray alpha PNG at the image's size.
    Optional cutout_png gets an RGBA cut-out. Returns {matte, size, cost_usd, head_top_frac, seconds} or {"error": ...}."""
    t0 = time.time()
    calls = 0   # attempts (billed even when the call fails or finds nothing)
    try:
        img = Image.open(image_path)
        img.load()
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img).convert("RGB")
        W, H = img.size
        s = min(1.0, max_side / max(W, H))
        small = img.resize((max(2, int(W * s)), max(2, int(H * s))), Image.LANCZOS) if s < 1 else img
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "in.jpg"
            small.save(p, quality=92)
            url = "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()
        deadline = t0 + budget_s
        chosen, used = {}, prompt
        for pr in [prompt] + ([FALLBACK_PROMPT] if prompt != FALLBACK_PROMPT else []):
            calls += 1
            text = _sam_call({"type": "input_image", "image_url": url}, pr, deadline)
            chosen = select_largest(parse_masks(text))
            used = pr
            if chosen:
                break
        if not chosen:
            raise MatteError(f"no match for prompts {prompt!r}/{FALLBACK_PROMPT!r}")
        tok = max(chosen.values(), key=lambda t: t["_area"])
        _, mask = _decode_one(tok)
        mask = fit_masks({0: mask}, small.size)[0]
        feather_still(mask, (W, H), Path(out_png))
        if cutout_png:
            alpha = Image.open(out_png).convert("L")
            rgba = img.convert("RGBA")
            rgba.putalpha(alpha)
            rgba.save(cutout_png)
        res = {"matte": str(out_png), "size": [W, H], "cost_usd": round(COST_PER_IMAGE_USD * calls, 4),
               **subject_stats({0: tok}), "prompt": used, "seconds": round(time.time() - t0, 2)}
        return res
    except Exception as e:
        msg = str(e)[:240] if isinstance(e, MatteError) else f"{type(e).__name__}: {str(e)[:200]}"
        return {"error": msg, "matte": None, "cost_usd": round(COST_PER_IMAGE_USD * calls, 4), "head_top_frac": None,
                "subject_bbox_frac": None, "head_size_frac": None, "seconds": round(time.time() - t0, 2)}

"""Segmenter agent: SAM 3.1 tracks the speaker through the master cut; box stats drive the 1:1 crop."""
import json
import re
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from .llm import META, _hdr
from .trace import Run

BOX = re.compile(r"<(\d+)f>.*?<\|box;x1=(\d+);y1=(\d+);x2=(\d+);y2=(\d+);w=(\d+);h=(\d+)\|>")
COST_PER_FRAME_USD = 0.0002  # listed $0.20 / 1k frames; 50-frame minimum per request. Unverified vs billing.


def track(run: Run, video: Path, prompt: str = "woman") -> dict:
    """Returns {frames, top_frac, bottom_frac, boxes:[(frame,x1,y1,x2,y2,w,h)]} for the largest match per frame."""
    t0 = time.time()
    small = Path(tempfile.mkdtemp()) / "sam_in.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vf", "scale=540:-2", "-an", "-c:v", "libx264",
                    "-crf", "26", "-preset", "veryfast", "-movflags", "+faststart", str(small)], check=True)
    up = requests.post(META + "/files", headers=_hdr(), data={"purpose": "user_data"}, timeout=180,
                       files={"file": ("clip.mp4", small.open("rb"), "video/mp4")})
    up.raise_for_status()
    fid = up.json()["id"]
    r = requests.post(META + "/responses", headers={**_hdr(), "Content-Type": "application/json",
                                                     "Accept": "text/event-stream"},
                      stream=True, timeout=400, json={
        "model": "sam-3.1", "stream": True, "metadata": {"mask_encoding": "one_bit"},
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": prompt}, {"type": "input_video", "file_id": fid}]}]})
    r.raise_for_status()
    best: dict[int, tuple] = {}
    for raw in r.iter_lines():
        s = raw.decode("utf-8", "replace")
        if not s.startswith("data: "):
            continue
        if s[6:] == "[DONE]":
            break
        ev = json.loads(s[6:])
        if ev.get("type") != "response.output_text.delta":
            continue
        for m in BOX.finditer(ev["delta"]):
            f, x1, y1, x2, y2, w, h = map(int, m.groups())
            if f not in best or (x2 - x1) * (y2 - y1) > (best[f][3] - best[f][1]) * (best[f][4] - best[f][2]):
                best[f] = (f, x1, y1, x2, y2, w, h)
    if not best:
        run.trace("sam-3.1", {"prompt": prompt}, {"frames": 0}, started=t0, status="error",
                  decision="no matches returned; falling back to fixed crop")
        return {"frames": 0}
    boxes = [best[k] for k in sorted(best)]
    top = statistics.median(b[2] / b[6] for b in boxes)
    bottom = statistics.median(b[4] / b[6] for b in boxes)
    out = {"frames": len(boxes), "top_frac": top, "bottom_frac": bottom, "prompt": prompt}
    run.trace("sam-3.1", {"prompt": prompt, "file": video.name}, out, started=t0,
              cost_usd=round(max(len(boxes), 50) * COST_PER_FRAME_USD, 4),
              decision=f"tracked '{prompt}' on {len(boxes)} frames; head top at median {top:.0%} of frame height")
    run.contract("segmenter", "assembler", {"video": video.name, "prompt": prompt}, out)
    return out

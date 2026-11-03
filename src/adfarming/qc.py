"""QC agent: Muse Wonder reviews the finished 9:16 cut (key frames + captions) against the brief."""
import base64
import subprocess
import time
from pathlib import Path

import requests

from .llm import META, _extract_json, _hdr
from .trace import Run

SYSTEM = ("You are a strict creative QC reviewer for Meta ads in India. You check a finished ad against its brief. "
          "Return ONLY JSON.")
PROMPT = """BRIEF
{brief}

CONCEPT
{concept}

CAPTIONS AS BURNED IN (time-ordered)
{captions}

Attached: 4 key frames from the finished 9:16 cut, in order: hook (0.6s), 2.5s, middle, and the LAST frame, which is
the end-hold CTA card (the soft CTA text is overlaid on it).
Check: (1) hook lands in the first 3s, (2) no invented claims/prices/discounts, (3) captions match the speech and
are readable, (4) the cut ends on a soft CTA direction, (5) subject stays framed, (6) length 12-25s.
JSON: {{"pass":true|false,"score":1-5,"issues":["..."],"fixes":["..."],"summary":"one sentence"}}"""


def keyframes(video: Path, n: int = 4) -> list[str]:
    """Hook, 3s mark, middle, and a frame inside the CTA end hold (so QC can see the end card)."""
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                         "-of", "csv=p=0", str(video)]))
    times = [0.6, 2.5, dur * 0.5, dur - 1.0][:n]
    urls = []
    for i, t in enumerate(times):
        out = video.parent / f"qc_{i}.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, min(t, dur - 0.1)):.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=540:-2", str(out)], check=True)
        urls.append("data:image/jpeg;base64," + base64.b64encode(out.read_bytes()).decode())
    return urls


def _call(text: str, video: Path) -> tuple[dict, dict]:
    parts = [{"type": "input_text", "text": text}] + [{"type": "input_image", "image_url": u}
                                                       for u in keyframes(video)]
    r = requests.post(META + "/responses", headers=_hdr(), timeout=240, json={
        "model": "muse-wonder-1.0", "instructions": SYSTEM, "max_output_tokens": 6000,
        "reasoning": {"effort": "low"},
        "input": [{"type": "message", "role": "user", "content": parts}]})
    r.raise_for_status()
    j = r.json()
    body = "".join(c.get("text", "") for o in j["output"] if o["type"] == "message" for c in o["content"])
    return _extract_json(body), j.get("usage", {})


def review(run: Run, brief: str, concept: dict, captions: list[dict], video: Path) -> dict:
    t0 = time.time()
    text = PROMPT.format(brief=brief, concept=concept,
                         captions="\n".join(f"{c['start']}-{c['end']}s: {c['text']}" for c in captions))
    verdict, tokens, err = None, {}, ""
    for _ in range(2):
        try:
            verdict, tokens = _call(text, video)
            break
        except Exception as e:  # QC must never crash a run after money was spent
            err = f"{type(e).__name__}: {str(e)[:100]}"
    if verdict is None:
        verdict = {"pass": None, "score": None, "issues": ["qc unavailable"], "fixes": [], "summary": "QC unavailable"}
        run.trace("muse-wonder", {"concept": concept["id"]}, {"error": err}, started=t0, status="error",
                  decision=f"QC call failed twice ({err}); verdict skipped, delivering unreviewed")
        run.contract("qc", "human", {"concept": concept["id"]}, verdict)
        return verdict
    cost = round((tokens.get("input_tokens", 0) * 4 + tokens.get("output_tokens", 0) * 20) / 1e6, 5)
    ok = verdict.get("pass") is True
    run.trace("muse-wonder", {"concept": concept["id"], "frames": 4}, verdict, started=t0, cost_usd=cost,
              decision=f"QC {'PASS' if ok else 'FAIL'} score {verdict.get('score')}: {verdict.get('summary', '')}")
    run.contract("qc", "human", {"concept": concept["id"]}, verdict)
    return verdict

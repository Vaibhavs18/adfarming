"""Copywriter agent: brief + timed transcripts + reference ads -> 3 concepts with real cut points."""
import time

from .llm import chat_json
from .trace import Run

SYSTEM = """You are a senior performance-creative strategist and editor for Indian D2C brands on Meta (Reels/Stories).
You edit ONLY from the footage that exists, using the timestamps given. Never invent clips or words she did not say.
Return ONLY a JSON object, no commentary."""

PROMPT = """BRIEF
{brief}

AVAILABLE CLIPS (vertical 9:16, one woman talking to camera). Each line is a spoken turn: [start-end seconds] text.
{clips}

REFERENCE ADS (long-running winners in this niche)
{refs}

Write 3 DIFFERENT concepts (different angles). Each must address the gap: awareness worked, conversion did not.
Build each as an edit: 2-4 beats, each beat a src_in/src_out range (seconds, inside the clip's duration, 2-9s long)
taken from the clips so the spoken words support the hook. Total length 12-25s.
JSON shape:
{{"concepts":[{{
  "id":"c1","title":"...","angle":"story|proof|objection|offer",
  "hook":"first 3 seconds line",
  "beats":[{{"clip":"<clip file name>","src_in":0.0,"src_out":5.0,"purpose":"why this moment",
             "on_screen_text":"short overlay text, optional","visual_note":"what the frame shows"}}],
  "cta":"soft call to action","length_s":18,
  "why_it_fits":"tie to the conversion gap and the footage","risks":"what could go wrong"}}]}}"""


def clips_block(clips: list[dict]) -> str:
    out = []
    for c in clips:
        turns = "\n".join(f"    [{t['start']}-{t['end']}] {t['text']}" for t in c["turns"])
        out.append(f"- {c['file']} ({c['duration_s']}s)\n{turns}")
    return "\n".join(out)


def validate(concepts: list[dict], clips: list[dict]) -> None:
    dur = {c["file"]: c["duration_s"] for c in clips}
    for c in concepts:
        for b in c["beats"]:
            if b["clip"] not in dur:
                raise ValueError(f"{c['id']}: unknown clip {b['clip']!r}")
            b["src_in"], b["src_out"] = float(b["src_in"]), float(b["src_out"])
            if not (0 <= b["src_in"] < b["src_out"] <= dur[b["clip"]] + 0.05):
                raise ValueError(f"{c['id']}: bad range {b['src_in']}-{b['src_out']} in {b['clip']}")
            b["src_out"] = min(b["src_out"], dur[b["clip"]])


def generate(run: Run, brief: str, clips: list[dict], refs: str, attempts: int = 3) -> list[dict]:
    prompt = PROMPT.format(brief=brief, clips=clips_block(clips), refs=refs or "(none provided yet)")
    last = None
    for i in range(attempts):
        t0 = time.time()
        out, usage = chat_json(SYSTEM, prompt + (f"\n\nFIX THIS ERROR FROM YOUR LAST ANSWER: {last}" if last else ""))
        try:
            concepts = out["concepts"]
            validate(concepts, clips)
        except (KeyError, ValueError) as e:
            last = str(e)
            run.trace("muse-spark", {"attempt": i + 1}, {"error": last}, started=t0, status="error",
                      decision=f"validation failed, retrying: {last}")
            continue
        run.trace("muse-spark", {"brief": brief, "clips": [c["file"] for c in clips]}, out, started=t0,
                  decision=f"{len(concepts)} concepts via {usage['model']}; tokens={usage.get('tokens')}"
                           + (f"; fallback: {usage['fallback_reason']}" if "fallback_reason" in usage else ""))
        run.contract("copywriter", "human", {"brief": brief}, out)
        return concepts
    raise RuntimeError(f"concepts failed validation {attempts}x: {last}")

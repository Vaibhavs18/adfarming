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
taken from the clips so the spoken words support the hook. Total length 12-25s. Never reuse the same moment twice; ranges from one clip must not overlap. Do not end on a half-finished sentence, and the cta is shown as an end card.
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
        spans = sorted((b["clip"], b["src_in"], b["src_out"]) for b in c["beats"])
        for (c1, a1, z1), (c2, a2, z2) in zip(spans, spans[1:]):
            if c1 == c2 and a2 < z1:
                raise ValueError(f"{c['id']}: beats overlap inside {c1} ({a1}-{z1} vs {a2}-{z2}); use non-overlapping ranges")


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


REVISE = """You are re-editing ONE ad concept after QC rejected it.

BRIEF
{brief}

CURRENT CONCEPT
{concept}

QC ISSUES
{issues}
QC FIXES SUGGESTED
{fixes}

AVAILABLE CLIPS (spoken turns [start-end s] text)
{clips}

Return ONLY JSON: {{"concept": {{same shape as the current concept, with corrected beats/hook/cta}}}}.
Rules: ranges must be inside each clip and must not overlap; every beat must start and end on a complete phrase;
never end on an unfinished sentence or a stutter (the clip's final words may be unfinished, so stop before them);
the cta is shown as a soft end card. Keep length 12-25s."""


def revise(run: Run, brief: str, concept: dict, verdict: dict, clips: list[dict]) -> dict:
    prompt = REVISE.format(brief=brief, concept=concept, issues="\n".join(verdict.get("issues", [])),
                           fixes="\n".join(verdict.get("fixes", [])), clips=clips_block(clips))
    last = None
    for i in range(3):
        t0 = time.time()
        out, usage = chat_json(SYSTEM, prompt + (f"\n\nFIX THIS ERROR: {last}" if last else ""))
        try:
            new = out["concept"]
            new["id"] = concept["id"]
            validate([new], clips)
        except (KeyError, ValueError) as e:
            last = str(e)
            continue
        run.trace("muse-spark", {"qc_issues": verdict.get("issues", [])}, out, started=t0,
                  decision=f"re-edit after QC score {verdict.get('score')}: {len(new['beats'])} beats; {usage['model']}")
        run.contract("copywriter", "assembler", {"qc": verdict}, out)
        return new
    raise RuntimeError(f"revision failed validation: {last}")

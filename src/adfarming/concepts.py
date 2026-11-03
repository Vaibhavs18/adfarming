"""Copywriter agent: brief + transcripts + reference ads -> 3 concepts (JSON contract)."""
import json
import time
from pathlib import Path

from .llm import chat_json
from .trace import Run

SYSTEM = """You are a senior performance-creative strategist for Indian D2C brands on Meta (Reels/Stories).
Write ad concepts built ONLY from the footage that exists. Never invent clips.
Return ONLY a JSON object, no commentary."""

PROMPT = """BRIEF
{brief}

AVAILABLE CLIPS (vertical 9:16, talking to camera). Transcripts are machine-made; Hindi parts may be garbled.
{clips}

REFERENCE ADS (long-running winners in this niche)
{refs}

Write 3 DIFFERENT concepts. Each must fix the stated gap: awareness worked, conversion did not.
JSON shape:
{{"concepts":[{{
  "id":"c1","title":"...","angle":"story|proof|objection|offer",
  "hook":"first 3 seconds, spoken or on-screen line",
  "beats":[{{"t":"0-3s","clip":"<clip file name>","line":"what is said/shown","on_screen_text":"..."}}],
  "cta":"...","length_s":15,
  "why_it_fits":"tie to the brief's conversion gap and the footage",
  "risks":"what could go wrong"}}]}}"""


def clips_block(clips: list[dict]) -> str:
    return "\n".join(f"- {c['file']} ({c['duration_s']}s): {c['transcript']}" for c in clips)


def generate(run: Run, brief: str, clips: list[dict], refs: str) -> list[dict]:
    inp = {"brief": brief, "clips": clips, "refs": refs}
    t0 = time.time()
    out, usage = chat_json(SYSTEM, PROMPT.format(brief=brief, clips=clips_block(clips),
                                                  refs=refs or "(none provided yet)"))
    concepts = out["concepts"]
    valid = {c["file"] for c in clips}
    for c in concepts:
        for b in c["beats"]:
            if b["clip"] not in valid:
                raise ValueError(f"concept {c['id']} uses unknown clip {b['clip']!r}")
    run.trace("muse-spark-open", inp, out, started=t0,
              decision=f"generated {len(concepts)} concepts; tokens={usage.get('total_tokens')}")
    run.contract("copywriter", "human", inp, out)
    return concepts

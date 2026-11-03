"""Visualizer agent: chosen concept -> one Muse Image frame per beat (preview before any video spend)."""
import time
from pathlib import Path

from .llm import image_b64
from .trace import Run

STYLE = ("Vertical 9:16 storyboard frame for an Instagram Reel. Warm natural light, Indian setting, "
         "realistic, no text overlay, no watermark. ")
COST_PER_IMAGE_USD = 0.01  # listed price per image; unverified against Meta billing


def frames(run: Run, concept: dict, subject: str) -> list[str]:
    out_dir = run.dir / "frames"
    out_dir.mkdir(exist_ok=True)
    paths = []
    for i, b in enumerate(concept["beats"], 1):
        prompt = f"{STYLE}{subject}. {b['visual_note']}. Mood: {b['purpose']}."
        t0 = time.time()
        data = image_b64(prompt)
        p = out_dir / f"{concept['id']}_beat{i}.webp"
        p.write_bytes(data)
        paths.append(str(p.relative_to(run.dir)))
        run.trace("muse-image", {"prompt": prompt}, {"bytes": len(data)}, started=t0,
                  cost_usd=COST_PER_IMAGE_USD, artifacts=[str(p)],
                  decision=f"beat {i}: preview frame for '{b['purpose']}'")
    run.contract("visualizer", "human", {"concept": concept["id"]}, {"frames": paths})
    return paths

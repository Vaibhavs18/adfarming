"""Shared builders for multi-client tests (ffmpeg testsrc clips + fake ASR caches; no API use)."""
import json
import subprocess
from pathlib import Path

def make_clip(path: Path, dur=2, size="160x90"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=d={dur}:s={size}:r=10",
                    "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", str(dur), "-shortest",
                    "-pix_fmt", "yuv420p", str(path)], check=True)


def fake_asr(text):
    return {"transcript": text, "turns": [{"startMs": 0, "endMs": 1500, "transcript": text}]}


def make_client(priv: Path, cid, name, brief, files, subject=None, asr=True):
    d = priv / "clients" / cid
    for f, dur in files:
        make_clip(d / "footage" / f, dur)
        if asr:
            (d / "asr").mkdir(exist_ok=True)
            (d / "asr" / f"{Path(f).stem}.json").write_text(json.dumps(fake_asr(f"{cid} says {f}")))
    (d / "brief.md").write_text(brief)
    if subject:
        (d / "subject.txt").write_text(subject)
    (d / "client.json").write_text(json.dumps({"id": cid, "name": name}))
    return d



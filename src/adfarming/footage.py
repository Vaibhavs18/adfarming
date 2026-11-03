"""Clip metadata + Meta Voice Transcribe turns (cached under private/asr/)."""
import json
import subprocess
from pathlib import Path

from .llm import ROOT, transcribe_wav

PRIV = ROOT / "private"


def duration(path: Path) -> float:
    return float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]))


def load_clips() -> list[dict]:
    """Each clip: file, duration_s, transcript, turns[{start,end,text}] (non-empty turns only)."""
    (PRIV / "asr").mkdir(exist_ok=True)
    (PRIV / "audio").mkdir(exist_ok=True)
    clips = []
    for f in sorted((PRIV / "footage").iterdir()):
        wav, cache = PRIV / "audio" / f"{f.stem}.wav", PRIV / "asr" / f"{f.stem}.json"
        if not cache.exists():
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(f), "-ac", "1", "-ar", "24000",
                            "-c:a", "pcm_s16le", "-map_metadata", "-1", str(wav)], check=True)
            cache.write_text(json.dumps(transcribe_wav(wav), ensure_ascii=False))
        j = json.loads(cache.read_text())
        turns = [{"start": round(t["startMs"] / 1000, 2), "end": round(t["endMs"] / 1000, 2),
                  "text": t["transcript"].strip()} for t in j["turns"] if t["transcript"].strip()]
        clips.append({"file": f.name, "duration_s": round(duration(f), 2),
                      "transcript": j["transcript"], "turns": turns})
    return clips

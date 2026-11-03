"""Clip metadata + Meta Voice Transcribe turns (cached per client under <client dir>/asr/)."""
import json
import os
import subprocess
import uuid
from pathlib import Path

from . import clients
from .llm import ROOT, transcribe_wav

# Import-time snapshot, kept for backward compatibility; code that must follow ADF_PRIVATE at call time
# uses clients.private_root(). Docker: /data (clients/<id>/..., or the legacy flat footage/, asr/, brief.md).
PRIV = Path(os.getenv("ADF_PRIVATE", str(ROOT / "private")))


def duration(path: Path) -> float:
    return float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]))


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def asr_for(f: Path, audio_dir: Path, asr_dir: Path, transcribe=None) -> dict:
    """Cached ASR JSON for one footage file. Writes are atomic (temp + rename) so two concurrent
    runs never see a half-written cache."""
    transcribe = transcribe or transcribe_wav
    cache = asr_dir / f"{f.stem}.json"
    if not cache.exists():
        wav = audio_dir / f"{f.stem}.wav"
        tmp = audio_dir / f".{f.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.wav"
        try:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(f), "-ac", "1", "-ar", "24000",
                            "-c:a", "pcm_s16le", "-map_metadata", "-1", "-f", "wav", str(tmp)], check=True)
            os.replace(tmp, wav)
        finally:
            tmp.unlink(missing_ok=True)
        _atomic_write(cache, json.dumps(transcribe(wav), ensure_ascii=False))
    return json.loads(cache.read_text())


def load_clips(client_id: str | None = None, transcribe=None) -> list[dict]:
    """Each clip: file, path (absolute, the client's own footage file), duration_s, transcript,
    turns[{start,end,text}] (non-empty turns only). client_id None = the default client."""
    base = clients.client_dir(client_id)
    (base / "asr").mkdir(exist_ok=True)
    (base / "audio").mkdir(exist_ok=True)
    clips = []
    for f in sorted((base / "footage").iterdir()):
        if f.name.startswith(".") or f.suffix.lower() not in clients.CLIP_EXT:
            continue
        j = asr_for(f, base / "audio", base / "asr", transcribe)
        turns = [{"start": round(t["startMs"] / 1000, 2), "end": round(t["endMs"] / 1000, 2),
                  "text": t["transcript"].strip()} for t in j["turns"] if t["transcript"].strip()]
        clips.append({"file": f.name, "path": str(f), "duration_s": round(duration(f), 2),
                      "transcript": j["transcript"], "turns": turns})
    return clips

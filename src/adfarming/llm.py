"""Model calls. Primary: Meta Model API (Muse Spark 1.3). Fallback: Muse Spark Open via Baseten."""
import base64
import json
import os
import re
import time
from pathlib import Path

import requests
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[2]
META = "https://api.meta.ai/v1"


def _env() -> dict:
    """Keys come from .env if present, else from the process environment (Docker/CI)."""
    env = {}
    f = ROOT / ".env"
    if f.exists():
        for l in f.read_text().splitlines():
            l = l.strip()
            if l and not l.startswith("#") and "=" in l:
                k, v = l.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    for k in ("META_MODEL_API_KEY", "BASETEN_API_KEY", "BASETEN_MODEL"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def _hdr() -> dict:
    return {"Authorization": "Bearer " + _env()["META_MODEL_API_KEY"]}


def _extract_json(text: str) -> dict:
    """First decodable JSON object in the text (tolerates prose and stray braces around it)."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"no JSON in model output: {text[:200]!r}")


def spark_json(system: str, user: str, effort: str = "low", max_out: int = 8000) -> tuple[dict, dict]:
    """Muse Spark 1.3 via /v1/responses. Returns (json, {model, tokens})."""
    r = requests.post(META + "/responses", headers=_hdr(), timeout=180, json={
        "model": "muse-spark-1.3", "instructions": system, "input": user,
        "max_output_tokens": max_out, "reasoning": {"effort": effort}})
    r.raise_for_status()
    j = r.json()
    text = "".join(c.get("text", "") for o in j["output"] if o["type"] == "message" for c in o["content"])
    if not text:
        raise ValueError(f"empty output, status={j.get('status')}, details={j.get('incomplete_details')}")
    return _extract_json(text), {"model": "muse-spark-1.3", "tokens": j.get("usage", {}).get("total_tokens")}


def baseten_json(system: str, user: str, max_tokens: int = 12000) -> tuple[dict, dict]:
    env = _env()
    c = OpenAI(api_key=env["BASETEN_API_KEY"], base_url="https://inference.baseten.co/v1")
    r = c.chat.completions.create(model=env["BASETEN_MODEL"], max_tokens=max_tokens, temperature=0.7,
                                  messages=[{"role": "system", "content": system},
                                            {"role": "user", "content": user}])
    text = r.choices[0].message.content or ""
    if not text:
        raise ValueError(f"empty content, finish_reason={r.choices[0].finish_reason}")
    return _extract_json(text), {"model": env["BASETEN_MODEL"], "tokens": r.usage.total_tokens}


def chat_json(system: str, user: str) -> tuple[dict, dict]:
    try:
        return spark_json(system, user)
    except Exception as e:  # fall back so a demo run never dies on one provider
        out, usage = baseten_json(system, user)
        usage["fallback_reason"] = f"{type(e).__name__}: {str(e)[:120]}"
        return out, usage


def image_b64(prompt: str) -> bytes:
    r = requests.post(META + "/images/generations", headers=_hdr(), timeout=180,
                      json={"model": "muse-image-1.0", "prompt": prompt, "n": 1})
    r.raise_for_status()
    return base64.b64decode(r.json()["data"][0]["b64_json"])


def transcribe_wav(path: Path) -> dict:
    req = json.dumps({"mode": "DIARIZATION", "model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV"})
    with path.open("rb") as f:
        r = requests.post(META + f"/asr/transcribe?sessionId=adf-{path.stem}-{int(time.time())}",
                          headers=_hdr(), timeout=180,
                          files={"request": (None, req, "application/json"), "audio": (path.name, f, "audio/wav")})
    r.raise_for_status()
    return r.json()

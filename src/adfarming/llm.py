"""Muse Spark Open via Baseten (OpenAI-compatible). Swap base_url/model for the Meta Model API later."""
import json
import re
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parents[2]


def _env() -> dict:
    return dict(l.strip().split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l)


def client_and_model():
    env = _env()
    return (OpenAI(api_key=env["BASETEN_API_KEY"], base_url="https://inference.baseten.co/v1"),
            env["BASETEN_MODEL"])


def chat_json(system: str, user: str, max_tokens: int = 12000) -> tuple[dict, dict]:
    """Returns (parsed_json, usage). Model output may be wrapped in code fences."""
    client, model = client_and_model()
    r = client.chat.completions.create(
        model=model, max_tokens=max_tokens, temperature=0.7,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    text = r.choices[0].message.content or ""
    if not text:
        raise ValueError(f"empty content, finish_reason={r.choices[0].finish_reason}, "
                         f"usage={r.usage.model_dump()}")
    m =re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in model output: {text[:200]!r}")
    return json.loads(m.group(0)), r.usage.model_dump()

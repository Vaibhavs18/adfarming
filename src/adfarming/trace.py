"""Trace records and agent contracts, validated against schemas/ and written as JSONL."""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
_TRACE = Draft202012Validator(json.loads((ROOT / "schemas/trace_record.schema.json").read_text()), format_checker=FormatChecker())
_CONTRACT = Draft202012Validator(json.loads((ROOT / "schemas/agent_contract.schema.json").read_text()),
                                 format_checker=FormatChecker())


def digest(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Run:
    def __init__(self, run_id: str, out_dir: Path | None = None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
            raise ValueError(f"invalid run_id {run_id!r}")
        self.run_id = run_id
        self.dir = (out_dir or Path(os.getenv("ADF_OUT", str(ROOT / "out")))) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        tf = self.dir / "trace.jsonl"
        self.step = sum(1 for _ in tf.open()) if tf.exists() else 0

    def _append(self, name: str, record: dict, validator: Draft202012Validator):
        validator.validate(record)
        with (self.dir / name).open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def trace(self, tool, inp, out, *, decision="", cost_usd=None, status="ok",
              started=None, artifacts=None):
        self.step += 1
        rec = {
            "run_id": self.run_id, "step": self.step, "tool": tool,
            "input_hash": digest(inp), "output_hash": digest(out),
            "artifact_paths": artifacts or [], "decision": decision,
            "overridden_by_human": False, "started_at": now(),
            "duration_ms": int((time.time() - started) * 1000) if started else 0,
            "status": status,
        }
        if cost_usd is not None:
            rec["cost_usd"] = cost_usd
        self._append("trace.jsonl", rec, _TRACE)
        return rec

    def contract(self, from_role, to_role, inp, out):
        rec = {"run_id": self.run_id, "step": self.step, "from_role": from_role,
               "to_role": to_role, "input": inp, "output": out, "created_at": now()}
        self._append("contracts.jsonl", rec, _CONTRACT)
        return rec

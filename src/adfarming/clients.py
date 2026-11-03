"""Per-client private data. One deployment serves several clients.

Layout under ADF_PRIVATE (default <repo>/private):
  clients/<client_id>/{client.json, brief.md, subject.txt, refs.md?, rules.md?|seed_memory.jsonl?,
                       footage/, asr/, audio/}
Backward compatible: when ADF_PRIVATE/clients does not exist, the flat layout
(ADF_PRIVATE/{brief.md, subject.txt, footage/, asr/, audio/}) is the single client STUDIO_CLIENT_ID.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .llm import ROOT

CLIENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
DEFAULT_SUBJECT_FLAT = "an Indian woman in a saree"
DEFAULT_SUBJECT = "a person speaking to camera"
CLIP_EXT = (".mov", ".mp4", ".m4v")


class ClientDataError(Exception):
    pass


def private_root() -> Path:
    """Read at call time so tests / deployments can change ADF_PRIVATE."""
    return Path(os.environ.get("ADF_PRIVATE") or str(ROOT / "private"))


def default_client_id() -> str:
    return os.environ.get("STUDIO_CLIENT_ID") or "dharaa"


def layout_enabled() -> bool:
    return (private_root() / "clients").is_dir()


def _flat_has_data() -> bool:
    r = private_root()
    return (r / "footage").is_dir() or (r / "brief.md").is_file()


@dataclass
class ClientInfo:
    id: str
    name: str
    dir: Path
    subject: str = ""
    language: str = ""
    flat: bool = False


def _read_meta(d: Path) -> dict:
    f = d / "client.json"
    if not f.is_file():
        return {}
    try:
        j = json.loads(f.read_text())
        return j if isinstance(j, dict) else {}
    except ValueError:
        return {}


def list_clients() -> list[ClientInfo]:
    """Clients discovered from clients/*/client.json, plus the flat default when flat data exists."""
    out: dict[str, ClientInfo] = {}
    cdir = private_root() / "clients"
    if cdir.is_dir():
        for d in sorted(cdir.iterdir()):
            if d.is_dir() and CLIENT_ID_RE.match(d.name) and (d / "client.json").is_file():
                m = _read_meta(d)
                out[d.name] = ClientInfo(d.name, str(m.get("name") or d.name), d,
                                         str(m.get("subject") or ""), str(m.get("language") or ""))
    flat_id = default_client_id()
    if flat_id not in out and (not cdir.is_dir() or _flat_has_data()):
        name = os.environ.get("STUDIO_CLIENT_NAME") or ("Dharaa by Smriti" if flat_id == "dharaa" else flat_id)
        out[flat_id] = ClientInfo(flat_id, name, private_root(), flat=True)
    return list(out.values())


def get_client(client_id: str | None) -> ClientInfo | None:
    if client_id is None:
        client_id = default_client_id()
    for c in list_clients():
        if c.id == client_id:
            return c
    return None


def client_dir(client_id: str | None) -> Path:
    """Data directory of the client. Flat layout (no clients/ dir): the private root for any id.
    Raises ClientDataError for an unknown client in the multi-client layout."""
    root = private_root()
    if client_id is None:
        client_id = default_client_id()
    if not (root / "clients").is_dir():
        return root
    if not CLIENT_ID_RE.match(client_id or ""):
        raise ClientDataError(f"invalid client id {client_id!r}")
    d = root / "clients" / client_id
    if d.is_dir():
        return d
    if client_id == default_client_id() and _flat_has_data():
        return root
    raise ClientDataError(f"no data directory for client {client_id!r}")


def footage_dir(client_id: str | None) -> Path:
    return client_dir(client_id) / "footage"


def _read(client_id: str | None, name: str) -> str | None:
    f = client_dir(client_id) / name
    return f.read_text() if f.is_file() else None


def load_brief(client_id: str | None = None) -> str:
    return _read(client_id, "brief.md") or ""


def load_refs(client_id: str | None = None) -> str:
    return _read(client_id, "refs.md") or ""


def load_subject(client_id: str | None = None) -> str:
    s = (_read(client_id, "subject.txt") or "").strip()
    if s:
        return s
    d = client_dir(client_id)
    info = _read_meta(d)
    if info.get("subject"):
        return str(info["subject"])
    return DEFAULT_SUBJECT_FLAT if d == private_root() else DEFAULT_SUBJECT


# ---------------------------------------------------------------- memory seed
_RULE_RE = re.compile(r"^\s*(?:(DO|DON'?T|DONT)\s*:|([+-]))\s*(.+?)\s*$", re.I)


def parse_rules(text: str) -> list[dict]:
    """Plain-text rules, one per line: 'DO: ...' / "DON'T: ..." / '+ ...' / '- ...' -> hard_rule items.
    A "don't" rule is phrased 'Never: <text>' unless it already starts with a negation."""
    items = []
    for line in text.splitlines():
        m = _RULE_RE.match(line)
        if not m:
            continue
        neg = (m.group(1) or "").upper().startswith("DON") or m.group(2) == "-"
        body = m.group(3).strip()
        if not body:
            continue
        if neg and not re.match(r"^(never|do not|don'?t|no|avoid)\b", body, re.I):
            body = "Never: " + body
        items.append({"id": f"rule_{len(items) + 1:03d}", "kind": "hard_rule", "text": body,
                      "source": "human", "status": "approved", "pinned": True,
                      "decided_by": "human"})
    return items


def seed_items(client_id: str | None) -> list[dict]:
    """Initial memory items: seed_memory.jsonl (existing seed format) then rules.md. Ids stay unique."""
    d = client_dir(client_id)
    items: list[dict] = []
    f = d / "seed_memory.jsonl"
    if f.is_file():
        items += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    r = d / "rules.md"
    if r.is_file():
        taken = {i["id"] for i in items}
        items += [i for i in parse_rules(r.read_text()) if i["id"] not in taken]
    return items

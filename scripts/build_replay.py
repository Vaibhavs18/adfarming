#!/usr/bin/env python3
"""Build the data for the Studio "replay" demo from ONE real run.

    python scripts/build_replay.py --run <run_id> --src private/replay/src --out private/replay/<name>

Expected layout under --src (all copies; the real run is never touched):

    <src>/out/<run_id>/            the run's output dir (frame_*.webp, c*_<ratio>.mp4, ...)
    <src>/api/<run_id>.run.json    GET /api/runs/{id}
    <src>/api/<run_id>.log.json    GET /api/runs/{id}/log
    <src>/api/memory.json          GET /api/clients/<id>/memory?status=all
    <src>/*.db                     the Studio SQLite DB (gates + gate cards), or pass --db

Writes <out>/fixtures.json and <out>/media/ (small frames, small cuts). The --out folder must
live under the gitignored private/ tree (or /tmp): the output contains real client material.
The builder refuses to write anywhere git tracks files unless --allow-tracked is passed.

web/mock/mock.js loads fixtures.json when the page is opened with ?mock=1&replay=<token>.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCHEMA = "adfarming-replay/1"
STEPS = ["brief", "ideas", "preview", "edit", "check", "ship"]
BANNER = "Replay of a real session on {client}. Nothing here calls any AI."
RATIOS = {"9x16": (540, 960), "1x1": (540, 540), "16x9": (960, 540)}
MAX_CUT_BYTES = int(2.5 * 1024 * 1024)
FRAME_W = 360
# Log rows that are plumbing, not decisions a person would want to read.
NOISE_KINDS = {"run_status", "tool_started", "tool_done"}
REQUIRED_TOP = ("schema", "banner", "client", "clients", "run", "memory", "gates", "tail", "artifacts")
REQUIRED_GATE = ("idx", "step", "card", "answer", "work_s", "commentary", "cost", "log_q", "log_a", "in_play")


# ----------------------------------------------------------------------------- helpers
def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _basename(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def fmt_duration(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m {sec % 60}s"


def _git_tracked_roots(path: Path) -> bool:
    """True when `path` is inside a git work tree and not ignored (i.e. a commit could pick it up)."""
    p = path.resolve()
    probe = p if p.exists() else p.parent
    while not probe.exists():
        probe = probe.parent
    try:
        inside = subprocess.run(["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return False
        ignored = subprocess.run(["git", "-C", str(probe), "check-ignore", "-q", str(p)], capture_output=True)
        return ignored.returncode != 0
    except FileNotFoundError:
        return False


# ----------------------------------------------------------------------------- DB (gate cards + answers)
def load_gates(db_path: Path, run_id: str) -> tuple[list[dict], str]:
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    cards: dict[int, dict] = {}
    refs: dict[int, list] = {}
    for r in db.execute("SELECT payload FROM events WHERE run_id=? AND kind='gate_requested' ORDER BY id", (run_id,)):
        p = json.loads(r["payload"])
        gid = p.get("gate_id")
        pl = p.get("payload") or {}
        cards[gid] = pl.get("card") or {}
        refs[gid] = pl.get("memory_refs") or []
    gates = []
    for g in db.execute("SELECT id, step, requested_at, answered_at, answer FROM gates WHERE run_id=? ORDER BY id", (run_id,)):
        ans = json.loads(g["answer"]) if g["answer"] else {}
        gates.append({
            "gate_id": g["id"], "step": g["step"], "answer": ans,
            "card": cards.get(g["id"], {"type": g["step"]}), "memory_refs": refs.get(g["id"], []),
        })
    client_name = ""
    try:
        row = db.execute("SELECT c.name FROM runs r JOIN clients c ON c.id=r.client_id WHERE r.id=?", (run_id,)).fetchone()
        client_name = (row["name"] if row else "") or ""
    except sqlite3.Error:
        pass
    db.close()
    return gates, client_name


# ----------------------------------------------------------------------------- memory
def clean_evidence(ev: dict | None, source: str) -> dict | None:
    """The UI reads evidence.quote. Real items carry human_quote / doc_ref: turn them into a readable provenance line."""
    ev = ev or {}
    quote = ev.get("quote") or ev.get("human_quote")
    doc = ev.get("doc_ref") or ""
    if not quote:
        m = re.match(r"^brief\.md#(.+)$", doc)
        if m:
            quote = f"From the client's own brief, section '{m.group(1).replace('-', ' ')}'."
        elif doc.startswith("steer@"):
            quote = f"Learned from a steer at the {doc.split('@', 1)[1]} step."
        elif doc.startswith("private/asr/"):
            quote = "From the transcript of the client's footage."
        elif source == "human":
            quote = "Added by a person on the team."
    out = {"quote": quote} if quote else {}
    if ev.get("run_id"):
        out["source_run"] = ev["run_id"]
    if doc:
        out["doc_ref"] = doc
    return out or None


def build_memory(items: list[dict], run_id: str, log_rows: list[dict], gate_of_row) -> list[dict]:
    """Approved items (what the agent read) + the proposals THIS run produced, each tied to the gate where it appeared."""
    proposed_at: dict[str, int] = {}
    for r in log_rows:
        if r.get("kind") == "memory_proposed":
            for mid in r.get("memory_ids") or []:
                proposed_at.setdefault(mid, gate_of_row(r))
    out = []
    for m in items:
        ev = m.get("evidence") or {}
        if m.get("status") == "approved":
            pass
        elif m.get("status") == "proposed" and ev.get("run_id") == run_id and m["id"] in proposed_at:
            pass
        else:
            continue
        item = {k: m.get(k) for k in ("id", "kind", "text", "source", "status", "pinned", "created_at")}
        item["pinned"] = bool(item["pinned"])
        item["evidence"] = clean_evidence(ev, m.get("source", ""))
        if m["id"] in proposed_at:
            item["appears_at_gate"] = proposed_at[m["id"]]
        out.append(item)
    return out


# ----------------------------------------------------------------------------- log
ARTIFACT_URL = re.compile(r"^/api/runs/[^/]+/artifacts/[^/]+$")
COST_RE = re.compile(r"^Spent \$[\d.]+ on (.+)$")


def split_log(raw: list[dict], n_gates: int):
    """Assign every kept log row to a gate: 'q' = revealed when gate k is raised, 'a' = revealed when it is answered.
    Returns (rows_with_slot, boundaries) where each row has _slot=('q'|'a'|'tail', k)."""
    rows = [r for r in raw if r.get("kind") not in NOISE_KINDS and "Server restarted" not in (r.get("text") or "")]
    # gate_requested rows are the boundaries between segments
    req_idx = [i for i, r in enumerate(rows) if r["kind"] == "gate_requested"]
    if len(req_idx) != n_gates:
        raise SystemExit(f"log has {len(req_idx)} gate_requested rows but the DB has {n_gates} gates; src files do not belong together")
    seen_mem = None
    kept = []
    k = 0  # next gate to be requested
    last_req = -1
    for i, r in enumerate(rows):
        row = {x: r.get(x) for x in ("ts", "step", "kind", "actor", "text", "cost_usd", "memory_ids")}
        row["memory_ids"] = row["memory_ids"] or []
        human = r["kind"] in ("gate_answered", "steer") or r.get("actor") == "human"
        if r["kind"] == "memory_read":
            if row["memory_ids"] and set(row["memory_ids"]) == seen_mem:
                continue
            seen_mem = set(row["memory_ids"])
        if human and r["kind"] != "run_created":
            row["_slot"] = ("a", max(k - 1, 0))
        elif k < n_gates:
            row["_slot"] = ("q", k)
        else:
            row["_slot"] = ("tail", n_gates)
        if r["kind"] == "gate_requested":
            k += 1
        kept.append(row)
    return kept


# ----------------------------------------------------------------------------- media
def make_frame(src: Path, dst: Path) -> None:
    from PIL import Image
    im = Image.open(src)
    if im.width > FRAME_W:
        im = im.resize((FRAME_W, round(im.height * FRAME_W / im.width)), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, "WEBP", quality=70)


def make_cut(src: Path, dst: Path, ratio: str) -> int:
    w, h = RATIOS[ratio]
    dst.parent.mkdir(parents=True, exist_ok=True)
    for crf in (28, 31, 34, 37):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
               "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1",
               "-c:v", "libx264", "-crf", str(crf), "-preset", "medium", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(dst)]
        subprocess.run(cmd, check=True)
        if dst.stat().st_size <= MAX_CUT_BYTES:
            break
    return dst.stat().st_size


# ----------------------------------------------------------------------------- main build
def build(run_id: str, src: Path, out: Path, db_path: Path | None = None, media: bool = True, client_name: str | None = None) -> dict:
    run_dir = src / "out" / run_id
    api = src / "api"
    run_state = _load(api / f"{run_id}.run.json")
    raw_log = _load(api / f"{run_id}.log.json")
    mem_all = _load(api / "memory.json")
    if db_path is None:
        dbs = sorted(src.glob("*.db"))
        if not dbs:
            raise SystemExit(f"no *.db found in {src}; pass --db")
        db_path = dbs[0]
    gates, db_client_name = load_gates(db_path, run_id)
    if not gates:
        raise SystemExit(f"no gates for run {run_id} in {db_path}")
    client_id = run_state.get("client_id") or "client"
    cname = client_name or db_client_name or client_id

    kept = split_log(raw_log, len(gates))
    n = len(gates)

    # ---- media + URL rewriting
    media_dir = out / "media"
    url_map: dict[str, str] = {}

    def add_media(url: str) -> str:
        base = _basename(url)
        if base in url_map:
            return url_map[base]
        m = re.match(r"^frame_.*?_(\d+)\.webp$", base)
        c = re.match(r"^.*_(9x16|1x1|16x9)\.mp4$", base)
        if m:
            dst_name = f"media/frame_{m.group(1)}.webp"
            if media:
                make_frame(run_dir / base, out / dst_name)
        elif c:
            dst_name = f"media/{c.group(1)}.mp4"
            if media:
                size = make_cut(run_dir / base, out / dst_name, c.group(1))
                print(f"  cut {c.group(1)}: {size / 1024:.0f} KB")
        else:
            return None  # zip etc.: not shipped to the static demo
        url_map[base] = dst_name
        return dst_name

    def rewrite(o):
        if isinstance(o, str):
            if ARTIFACT_URL.match(o):
                return add_media(o)
            return o
        if isinstance(o, list):
            return [x for x in (rewrite(v) for v in o) if x is not None]
        if isinstance(o, dict):
            res = {}
            for k, v in o.items():
                nv = rewrite(v)
                if k == "zip":
                    nv = None
                res[k] = nv
            return res
        return o

    if media:
        if media_dir.exists():
            shutil.rmtree(media_dir)
        media_dir.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    # ---- per-gate segments
    boundaries = [r for r in kept if r["kind"] == "gate_requested"]
    t_start = _ts(kept[0]["ts"]) if kept else 0.0
    answered_ts = [None] * n
    for r in kept:
        if r["kind"] == "gate_answered":
            k = r["_slot"][1]
            answered_ts[k] = _ts(r["ts"])
    gate_out = []
    prev_in_play: list[str] = []
    for k, g in enumerate(gates):
        card = rewrite(g["card"])
        if "memory_proposals" in card:
            card["memory_proposals"] = []  # mock.js fills this live from the replayed memory (this run's proposals only)
        step = g["step"]
        t_req = _ts(boundaries[k]["ts"])
        t_from = answered_ts[k - 1] if k > 0 and answered_ts[k - 1] else t_start
        q_rows = [r for r in kept if r["_slot"] == ("q", k)]
        a_rows = [r for r in kept if r["_slot"] == ("a", k)]
        commentary = [r["text"] for r in q_rows if r["kind"] == "commentary" and r["actor"] != "system"]
        cost: dict[str, float] = {}
        for r in q_rows:
            if r["kind"] == "cost" and r.get("cost_usd"):
                mt = COST_RE.match(r["text"] or "")
                tool = mt.group(1) if mt else "model"
                cost[tool] = round(cost.get(tool, 0.0) + r["cost_usd"], 6)
        in_play = list(card.get("memory_cited") or g["memory_refs"] or prev_in_play)
        prev_in_play = in_play
        ans = g["answer"] or {}
        payload = ans.get("payload") or {}
        gate_out.append({
            "idx": k, "step": step, "card": card,
            "answer": {"kind": ans.get("kind") or "approve", "text": ans.get("text"), "reason": ans.get("reason"),
                       "choice": payload.get("choice"), "fields": payload.get("fields")},
            "work_s": round(max(t_req - t_from, 0.0), 1), "commentary": commentary, "cost": cost,
            "log_q": [_strip(r) for r in q_rows], "log_a": [_strip(r) for r in a_rows], "in_play": in_play,
        })

    tail_rows = [r for r in kept if r["_slot"][0] == "tail"]
    t_end = max([_ts(r["ts"]) for r in kept] or [t_start])
    t_last_ans = answered_ts[-1] or t_end
    tail_cost: dict[str, float] = {}
    for r in tail_rows:
        if r["kind"] == "cost" and r.get("cost_usd"):
            mt = COST_RE.match(r["text"] or "")
            tool = mt.group(1) if mt else "model"
            tail_cost[tool] = round(tail_cost.get(tool, 0.0) + r["cost_usd"], 6)
    tail = {"work_s": round(max(t_end - t_last_ans, 0.0), 1),
            "commentary": [r["text"] for r in tail_rows if r["kind"] == "commentary" and r["actor"] != "system"],
            "cost": tail_cost, "log": [_strip(r) for r in tail_rows]}

    def gate_of_row(r):
        return r["_slot"][1] if r["_slot"][0] == "q" else min(r["_slot"][1] + 1, n)

    memory = build_memory(mem_all, run_id, kept, gate_of_row)

    # ---- artifacts of the finished run (used after "back", and for the final ship screen)
    art = run_state.get("artifacts") or {}
    artifacts = rewrite({
        "frames": art.get("frames") or [], "cuts": art.get("cuts") or {}, "captions": art.get("captions") or [],
        "qc": art.get("qc"), "zip": None,
    })
    cost_state = run_state.get("cost") or {}
    total_cost = round(sum(sum(g["cost"].values()) for g in gate_out) + sum(tail_cost.values()), 4)
    fixtures = {
        "schema": SCHEMA,
        "banner": BANNER.format(client=cname),
        "client": {"id": client_id, "name": cname},
        "clients": [{"id": client_id, "name": cname}],
        "run": {"run_id": run_id, "cap_usd": cost_state.get("cap_usd", 1.5), "total_cost_usd": total_cost,
                "total_real_s": round(t_end - t_start, 1), "real_started": kept[0]["ts"] if kept else None,
                "cost_by_tool": cost_state.get("by_tool") or {}},
        "memory": memory,
        "gates": gate_out,
        "tail": tail,
        "artifacts": artifacts,
    }
    validate(fixtures)
    (out / "fixtures.json").write_text(json.dumps(fixtures, ensure_ascii=False, indent=1), encoding="utf-8")
    return fixtures


def _strip(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def validate(fx: dict) -> None:
    """Raise ValueError when fixtures.json would not satisfy mock.js. Also used by the tests."""
    for k in REQUIRED_TOP:
        if k not in fx:
            raise ValueError(f"fixtures missing key {k!r}")
    if fx["schema"] != SCHEMA:
        raise ValueError("unknown schema")
    if not fx["gates"]:
        raise ValueError("no gates")
    if fx["gates"][0]["step"] != "brief":
        raise ValueError("first gate must be the brief")
    if fx["gates"][-1]["step"] != "ship":
        raise ValueError("last gate must be ship")
    for g in fx["gates"]:
        for k in REQUIRED_GATE:
            if k not in g:
                raise ValueError(f"gate {g.get('idx')} missing {k!r}")
        if g["step"] not in STEPS:
            raise ValueError(f"bad step {g['step']!r}")
        if g["card"].get("type") != g["step"]:
            raise ValueError(f"gate {g['idx']}: card type {g['card'].get('type')!r} != step {g['step']!r}")
        if g["answer"]["kind"] not in ("approve", "steer", "edit", "back"):
            raise ValueError("bad recorded answer kind")
    for m in fx["memory"]:
        for k in ("id", "kind", "text", "source", "status"):
            if k not in m:
                raise ValueError(f"memory item missing {k!r}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="run id (a folder under <src>/out/)")
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--client-name", default=None)
    ap.add_argument("--no-media", action="store_true", help="fixtures only, skip frame/cut encoding")
    ap.add_argument("--allow-tracked", action="store_true", help="allow --out inside git-tracked space (do not)")
    a = ap.parse_args(argv)
    if not a.allow_tracked and _git_tracked_roots(a.out):
        print(f"refusing: {a.out} is not gitignored. Replay output holds real client material; use private/replay/<name>.", file=sys.stderr)
        return 2
    fx = build(a.run, a.src, a.out, a.db, media=not a.no_media, client_name=a.client_name)
    size = sum(f.stat().st_size for f in a.out.rglob("*") if f.is_file())
    print(f"built {len(fx['gates'])} gates, {len(fx['memory'])} memory items -> {a.out} ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

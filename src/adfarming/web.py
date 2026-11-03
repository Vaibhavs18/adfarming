"""Studio HTTP backend (FastAPI). Contract: docs/API_CONTRACT.md. App object: `adfarming.web:app`.

Env: STUDIO_PASSWORD, STUDIO_SECRET, STUDIO_RUNNER=stub|real, STUDIO_RUN_CAP_USD (1.5),
STUDIO_DAILY_CAP_USD (15), ADFARMING_DB, ADFARMING_OUT (run output root, default ./out),
STUDIO_WEB_DIR (default <repo>/web), STUDIO_MEMORY_SEED (optional seed dir for client dharaa),
STUDIO_HEARTBEAT_S (SSE heartbeat, default 15), STUDIO_COOKIE_SECURE (1/0, default: auto).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import runner as runner_mod
from .runner import CostCapExceeded
from .studio import db, events, gates, memory

COOKIE = "adf_session"
SESSION_MAX_AGE = 12 * 3600
MAX_RUNS_PER_SESSION = 5
RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MEDIA_TYPES = {".mp4": "video/mp4", ".webp": "image/webp", ".json": "application/json",
               ".zip": "application/zip", ".png": "image/png", ".jpg": "image/jpeg",
               ".srt": "text/plain; charset=utf-8"}
REPO_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_DIR = (REPO_ROOT / "private").resolve()
STEP_NAMES = events.STEPS


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


# ------------------------------------------------------------------ runner / lifespan
_runner: runner_mod.AgentRunner | None = None


def get_runner() -> runner_mod.AgentRunner:
    global _runner
    if _runner is None:
        _runner = runner_mod.make_runner()
    return _runner


def set_runner(r: runner_mod.AgentRunner | None) -> None:
    global _runner
    _runner = r


def _startup() -> None:
    with db.tx() as c:
        cid = os.environ.get("STUDIO_CLIENT_ID") or "dharaa"
        cname = os.environ.get("STUDIO_CLIENT_NAME") or ("Dharaa by Smriti" if cid == "dharaa" else cid)
        db.ensure_client(c, cid, cname)
        c.execute("UPDATE clients SET name=? WHERE id=?", (cname, cid))
        rows = c.execute("SELECT id FROM runs").fetchall()
    seed = os.environ.get("STUDIO_MEMORY_SEED")
    if seed and Path(seed).exists() and not memory.list_memory(cid):
        memory.load_seed(cid, seed)  # only a client with no memory yet; never re-seeds over edits
    for r in rows:
        if runner_mod.get_status(r["id"]) == "running":
            runner_mod.set_status(r["id"], "interrupted")
            events.append_event(r["id"], "commentary", "system",
                                {"text": "Server restarted while this run was working; it was interrupted."})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _startup()
    yield


app = FastAPI(title="AdFarming Studio", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)


@app.exception_handler(ApiError)
async def _api_error(_r: Request, e: ApiError):
    return _err(e.status, e.code, e.message)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_r: Request, e: StarletteHTTPException):
    code = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "error")
    return _err(e.status_code, code, str(e.detail))


@app.exception_handler(RequestValidationError)
async def _validation_error(_r: Request, e: RequestValidationError):
    msg = "; ".join(f"{'.'.join(str(x) for x in er['loc'][1:])}: {er['msg']}" for er in e.errors())
    return _err(422, "invalid_request", msg or "invalid request")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/") and "/artifacts/" not in request.url.path:
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# ------------------------------------------------------------------ auth
_attempts: dict[str, deque] = defaultdict(deque)
_EPHEMERAL_SECRET = secrets.token_hex(32)


def reset_rate_limit() -> None:
    _attempts.clear()


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("STUDIO_SECRET") or _EPHEMERAL_SECRET  # unset: sessions die on restart
    return URLSafeTimedSerializer(secret, salt="adf-session")


def _session_id(request: Request) -> str | None:
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    try:
        data = _serializer().loads(tok, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    sid = data.get("sid") if isinstance(data, dict) else None
    return sid if isinstance(sid, str) else None


def require_auth(request: Request) -> str:
    sid = _session_id(request)
    if not sid:
        raise ApiError(401, "unauthorized", "login required")
    return sid


class LoginBody(BaseModel):
    password: str


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    q = _attempts[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= 5:
        raise ApiError(429, "rate_limited", "too many login attempts; wait a minute")
    q.append(now)
    expected = os.environ.get("STUDIO_PASSWORD") or ""
    ok = bool(expected) and hmac.compare_digest(body.password.encode(), expected.encode())
    if not ok:
        raise ApiError(401, "bad_password", "wrong password")
    resp = JSONResponse({"ok": True})
    secure_env = os.environ.get("STUDIO_COOKIE_SECURE")
    secure = (secure_env == "1") if secure_env in ("0", "1") else (
        request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    resp.set_cookie(COOKIE, _serializer().dumps({"sid": secrets.token_hex(16)}),
                    max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=secure, path="/")
    return resp


api = APIRouter(dependencies=[Depends(require_auth)])


@api.get("/api/me")
def me():
    return {"ok": True}


# ------------------------------------------------------------------ memory
def _mi(i: dict) -> dict:
    return {"id": i["id"], "kind": i["kind"], "text": i["text"], "source": i["source"],
            "status": i["status"], "pinned": bool(i["pinned"]),
            "evidence": i.get("evidence") or None, "created_at": i["created_at"]}


def _client_or_404(cid: str) -> None:
    with db.read() as c:
        if not c.execute("SELECT 1 FROM clients WHERE id=?", (cid,)).fetchone():
            raise ApiError(404, "client_not_found", f"unknown client {cid!r}")


def _item_or_404(cid: str, mid: str) -> dict:
    _client_or_404(cid)
    for i in memory.list_memory(cid):
        if i["id"] == mid:
            return i
    raise ApiError(404, "memory_not_found", f"unknown memory item {mid!r}")


class MemoryCreate(BaseModel):
    kind: str
    text: str
    pinned: bool = False


class MemoryPatch(BaseModel):
    text: str


class MemoryDecide(BaseModel):
    decision: Literal["accept", "reject"]
    note: str | None = None


def _kind_check(kind: str) -> None:
    if kind not in memory.KINDS:
        raise ApiError(422, "invalid_request", f"kind must be one of {list(memory.KINDS)}")


def _get_item(cid: str, mid: str) -> dict:
    return _mi(_item_or_404(cid, mid))


@api.get("/api/clients")
def clients():
    with db.read() as c:
        return [{"id": r["id"], "name": r["name"] or r["id"]}
                for r in c.execute("SELECT id,name FROM clients ORDER BY id")]


@api.get("/api/clients/{cid}/memory")
def memory_list(cid: str, status: Literal["approved", "proposed", "all"] = "approved"):
    _client_or_404(cid)
    return [_mi(i) for i in memory.list_memory(cid, None if status == "all" else status)]


@api.post("/api/clients/{cid}/memory")
def memory_add(cid: str, body: MemoryCreate):
    _client_or_404(cid)
    _kind_check(body.kind)
    if not body.text.strip():
        raise ApiError(422, "invalid_request", "text must not be empty")
    mid = memory.add_human_memory(cid, body.kind, body.text, pinned=body.pinned)
    return _get_item(cid, mid)


@api.patch("/api/clients/{cid}/memory/{mid}")
def memory_patch(cid: str, mid: str, body: MemoryPatch):
    old = _item_or_404(cid, mid)
    if not body.text.strip():
        raise ApiError(422, "invalid_request", "text must not be empty")
    try:
        if old["status"] == "approved":
            new_id = memory.supersede_memory(mid, old["kind"], body.text)
        elif old["status"] == "proposed":
            new_id = memory.decide_memory(mid, "edit", text=body.text)["new_item_id"]
        else:
            raise ApiError(409, "not_editable", f"item is {old['status']}")
    except ValueError as e:
        raise ApiError(409, "not_editable", str(e))
    return _get_item(cid, new_id)


@api.post("/api/clients/{cid}/memory/{mid}/decide")
def memory_decide(cid: str, mid: str, body: MemoryDecide):
    _item_or_404(cid, mid)
    try:
        memory.decide_memory(mid, body.decision, note=body.note)
    except ValueError as e:
        raise ApiError(409, "not_proposed", str(e))
    return _get_item(cid, mid)


# ------------------------------------------------------------------ runs
def _valid_run(run_id: str) -> dict:
    if not RUN_ID_RE.match(run_id):
        raise ApiError(404, "run_not_found", "unknown run")
    with db.read() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        raise ApiError(404, "run_not_found", "unknown run")
    return dict(r)


def _url(run_id: str, name: str) -> str:
    return f"/api/runs/{run_id}/artifacts/{name}"


def _card_of(evs: list[dict], step: str) -> dict | None:
    for e in reversed(evs):
        if e["kind"] == "gate_requested" and e["payload"]["step"] == step:
            return (e["payload"].get("payload") or {}).get("card")
    return None


def _state(run: dict) -> dict:
    rid = run["id"]
    evs = events.list_events(rid)
    st = events.replay(evs)
    status = runner_mod.get_status(rid)
    cur = st["current_step"]
    cur_step = "ship" if cur == "done" else cur
    steps = []
    stale_steps = {a["step"] for a in st["artifacts"].values() if a["status"] == "stale"}
    for n in STEP_NAMES:
        if n in st["step_approval"]:
            s = "done"
        elif n == cur_step and status not in ("cancelled",):
            s = "current"
        elif n in stale_steps:
            s = "stale"
        else:
            s = "pending"
        steps.append({"name": n, "state": s})
    gate = None
    pg = st["pending_gate"]
    if pg and status == "awaiting_human":
        gate = {"id": pg["gate_id"], "step": pg["step"], "card": (pg.get("payload") or {}).get("card")}
    ideas, prev, edit, chk, ship = (_card_of(evs, s) for s in ("ideas", "preview", "edit", "check", "ship"))
    arts = {
        "concepts": ideas.get("concepts") if ideas else None,
        "frames": (prev or {}).get("frames", []),
        "cuts": (ship or {}).get("cuts") if ship else ((edit or {}).get("cuts") or _cuts_if_present(rid, edit)),
        "captions": (edit or {}).get("captions", []),
        "qc": ({**chk["verdict"]} if chk else None),
        "zip": (ship or {}).get("zip"),
    }
    mem = [_mi(i) for i in memory.read_memory(run["client_id"])]
    comm = [{"ts": e["ts"], "text": e["payload"].get("text", "")} for e in evs if e["kind"] == "commentary"][-50:]
    return {"run_id": rid, "client_id": run["client_id"], "status": status,
            "current_step": cur_step, "steps": steps, "gate": gate, "memory_in_play": mem,
            "artifacts": arts,
            "cost": {"usd": st["cost_usd"], "cap_usd": runner_mod.run_cap_usd(),
                     "by_tool": st["cost_by_tool"]},
            "commentary": comm}


def _cuts_if_present(rid: str, edit: dict | None):
    if not edit:
        return None
    d = runner_mod.run_dir(rid)
    cuts = {}
    for k, n in getattr(runner_mod.StubRunner, "CUTS", {}).items():
        if (d / n).is_file():
            cuts[k] = _url(rid, n)
    return cuts or None


class RunCreate(BaseModel):
    client_id: str
    goal: str | None = None


class AnswerBody(BaseModel):
    gate: int
    action: Literal["approve", "steer", "edit", "back"]
    text: str | None = None
    choice: str | None = None
    fields: dict[str, Any] | None = None
    reason: str | None = None
    to: str | None = None


def _cap_error(e: CostCapExceeded) -> ApiError:
    return ApiError(402, "cost_cap", str(e))


@api.post("/api/runs", status_code=200)
def run_create(body: RunCreate, sid: str = Depends(require_auth)):
    _client_or_404(body.client_id)
    try:
        runner_mod.check_caps(None)
    except CostCapExceeded as e:
        raise _cap_error(e)
    with db.read() as c:
        n = c.execute("SELECT COUNT(*) FROM events WHERE kind='run_owner' "
                      "AND json_extract(payload,'$.sid')=?", (sid,)).fetchone()[0]
        running = [r["id"] for r in c.execute("SELECT id FROM runs")]
    if n >= MAX_RUNS_PER_SESSION:
        raise ApiError(429, "session_run_limit", f"max {MAX_RUNS_PER_SESSION} runs per session")
    if any(runner_mod.get_status(r) == "running" for r in running):
        raise ApiError(409, "busy", "another run is currently working; wait for it to pause or finish")
    run_id = str(uuid4())
    events.create_run(body.client_id, run_id=run_id, brief=body.goal)
    events.append_event(run_id, "run_owner", "system", {"sid": sid})
    runner_mod.set_status(run_id, "running")
    try:
        get_runner().start(run_id, body.client_id, body.goal)
    except CostCapExceeded as e:
        _fail(run_id, str(e))
        raise _cap_error(e)
    except Exception as e:  # runner crash must not leave the run 'running'
        _fail(run_id, runner_mod.RealRunner.SAFE_ERROR)
    return {"run_id": run_id}


def _fail(run_id: str, msg: str) -> None:
    events.append_event(run_id, "error", "system", {"message": msg})
    runner_mod.set_status(run_id, "error")


@api.get("/api/runs")
def run_list():
    with db.read() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY created_at DESC")]
    out = []
    for r in rows:
        st = events.rebuild_run_state(r["id"])
        cur = st["current_step"]
        out.append({"run_id": r["id"], "client_id": r["client_id"],
                    "status": runner_mod.get_status(r["id"]),
                    "current_step": "ship" if cur == "done" else cur,
                    "created_at": r["created_at"], "cost_usd": st["cost_usd"]})
    return out


@api.get("/api/runs/{run_id}")
def run_get(run_id: str):
    return _state(_valid_run(run_id))


@api.post("/api/runs/{run_id}/answer", status_code=202)
def run_answer(run_id: str, body: AnswerBody):
    run = _valid_run(run_id)
    status = runner_mod.get_status(run_id)
    st = events.rebuild_run_state(run_id)
    pg = st["pending_gate"]
    if status != "awaiting_human" or not pg or pg["gate_id"] != body.gate:
        raise ApiError(409, "gate_not_pending", "that gate is not the pending one")
    try:
        runner_mod.check_caps(run_id)
    except CostCapExceeded as e:
        raise _cap_error(e)
    _assert_not_busy(run_id)
    payload: dict[str, Any] = {}
    if body.choice is not None:
        payload["choice"] = body.choice
    if body.fields is not None:
        payload["fields"] = body.fields
    if body.to is not None:
        payload["to"] = body.to
        if body.to not in STEP_NAMES:
            raise ApiError(422, "invalid_request", f"to must be one of {STEP_NAMES}")
    if pg["step"] == "ideas" and body.action == "approve":
        ids = [c["id"] for c in ((pg.get("payload") or {}).get("card") or {}).get("concepts", [])]
        if body.choice is None and ids:
            payload["choice"] = ((pg["payload"]["card"]).get("recommended")) or ids[0]
        if payload.get("choice") not in ids:
            raise ApiError(422, "invalid_request", "choice must be one of the concept ids")
    try:
        gates.answer_gate(body.gate, body.action, payload, text=body.text, reason=body.reason)
    except gates.GateAlreadyAnswered:
        raise ApiError(409, "gate_not_pending", "that gate was already answered")
    except gates.GateBlocked as e:
        raise ApiError(409, "gate_blocked", str(e))
    except (ValueError, gates.GateError) as e:
        raise ApiError(422, "invalid_request", str(e))
    try:
        get_runner().resume(run_id)
    except CostCapExceeded as e:
        _fail(run_id, str(e))
        raise _cap_error(e)
    except gates.GateBlocked as e:
        _fail(run_id, str(e))
        raise ApiError(409, "gate_blocked", str(e))
    except Exception as e:
        _fail(run_id, runner_mod.RealRunner.SAFE_ERROR)
    return {"status": "accepted"}


def _assert_not_busy(run_id: str) -> None:
    with db.read() as c:
        others = [r["id"] for r in c.execute("SELECT id FROM runs WHERE id<>?", (run_id,))]
    if any(runner_mod.get_status(r) == "running" for r in others):
        raise ApiError(409, "busy", "another run is currently working; wait for it to pause or finish")


@api.post("/api/runs/{run_id}/resume", status_code=202)
def run_resume(run_id: str):
    """Restart a run left 'interrupted' (server restart) or 'error'. Nothing already approved is redone."""
    _valid_run(run_id)
    if runner_mod.get_status(run_id) not in ("interrupted", "error"):
        raise ApiError(409, "not_resumable", "only an interrupted or failed run can be resumed")
    try:
        runner_mod.check_caps(run_id)
    except CostCapExceeded as e:
        raise _cap_error(e)
    _assert_not_busy(run_id)
    try:
        get_runner().recover(run_id)
    except CostCapExceeded as e:
        _fail(run_id, str(e))
        raise _cap_error(e)
    except Exception as e:
        _fail(run_id, runner_mod.RealRunner.SAFE_ERROR)
    return {"status": "accepted"}


@api.post("/api/runs/{run_id}/cancel", status_code=202)
def run_cancel(run_id: str):
    _valid_run(run_id)
    if runner_mod.get_status(run_id) not in runner_mod.TERMINAL:
        runner_mod.set_status(run_id, "cancelled")
        try:
            get_runner().cancel(run_id)
        except Exception:
            pass
    return {"status": "accepted"}


# ------------------------------------------------------------------ decision log
def _render(e: dict) -> tuple[str | None, str, float | None, list[str]] | None:
    k, p = e["kind"], e["payload"]
    step = p.get("step")
    cost, mids = None, []
    if k == "run_created":
        t = "Started a session" + (f": {p['brief']}" if p.get("brief") else "")
    elif k == "memory_read":
        mids = list(p.get("ids", []))
        t = f"Read {len(mids)} approved client memory item(s) to guide this run"
    elif k == "gate_requested":
        card = (p.get("payload") or {}).get("card") or {}
        mids = list(card.get("memory_cited") or [])
        for c in card.get("concepts", []) or []:
            mids += [m for m in c.get("cites", []) if m not in mids]
        t = f"Asked you to review the {step} step"
    elif k == "gate_answered":
        verb = {"approve": "Approved", "steer": "Steered", "edit": "Edited", "back": "Sent back"}[p["kind"]]
        t = f"{verb} the {step} step" + (f" (back to {p['to']})" if p.get("to") else "")
    elif k == "steer":
        t = f"Steer note: {p.get('text')}"
    elif k == "artifact_edited":
        t = f"Edited fields on the {step} step: {', '.join(sorted((p.get('fields') or {}).keys()))}"
    elif k == "cost":
        cost = float(p["usd"])
        t = f"Spent ${cost:.2f} on {p['tool']}"
    elif k == "memory_proposed":
        t = f"Proposed a client memory ({p.get('kind')}): {p.get('text')} [{p.get('status')}]"
        mids = [p["item_id"]] if p.get("item_id") else []
    elif k == "memory_decided":
        t = f"Memory item {p.get('decision')}ed"
        mids = [p["item_id"]]
    elif k == "gate_invalidated":
        t = f"Approval of the {step} step was revoked ({p.get('reason', '')})"
    elif k == "artifact_stale":
        t = f"Marked {p.get('artifact')} as out of date ({p.get('reason', '')})"
    elif k == "artifact_proposed":
        t = f"Produced {p.get('artifact')} for the {step} step"
    elif k == "commentary":
        t = p.get("text", "")
    elif k == "tool_started":
        t = f"Started {p.get('tool')}"
    elif k == "tool_done":
        t = f"Finished {p.get('tool')}"
    elif k == "run_status":
        t = f"Run is now {p.get('status')}"
    elif k == "error":
        t = f"Error: {p.get('message')}"
    else:
        return None
    return step, t, cost, mids


@api.get("/api/runs/{run_id}/log")
def run_log(run_id: str):
    _valid_run(run_id)
    rows = []
    for e in events.list_events(run_id):
        r = _render(e)
        if r is None:
            continue
        step, text, cost, mids = r
        rows.append({"seq": e["id"], "ts": e["ts"], "step": step, "kind": e["kind"],
                     "actor": e["actor"], "text": text, "cost_usd": cost, "memory_ids": mids})
    return rows


# ------------------------------------------------------------------ artifacts
def _protected_roots() -> list[tuple[Path, bool]]:
    """(root, out_root_may_live_inside). Client data and the DB directory are never served."""
    roots = [(PRIVATE_DIR, False)]
    if os.environ.get("ADF_PRIVATE"):
        roots.append((Path(os.environ["ADF_PRIVATE"]).resolve(), False))
    try:
        from .footage import PRIV
        roots.append((Path(PRIV).resolve(), False))
    except Exception:
        pass
    if os.environ.get("ADFARMING_DB"):
        roots.append((Path(os.environ["ADFARMING_DB"]).resolve().parent, True))
    return roots


def _is_protected(rp: Path) -> bool:
    out = runner_mod.out_root().resolve()
    in_out = out == rp or out in rp.parents
    for r, out_ok in _protected_roots():
        if (rp == r or r in rp.parents) and not (out_ok and in_out):
            return True
    return False


def safe_artifact_path(run_id: str, name: str) -> Path:
    """Flat filename that exists directly in the run's output dir. Raises ApiError(404)."""
    nf = ApiError(404, "not_found", "no such artifact")
    if not RUN_ID_RE.match(run_id) or not ARTIFACT_NAME_RE.match(name) or ".." in name:
        raise nf
    ext = Path(name).suffix.lower()
    if ext not in MEDIA_TYPES:
        raise nf
    base = runner_mod.run_dir(run_id).resolve()
    p = base / name
    try:
        rp = p.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise nf
    if rp.parent != base or not rp.is_file() or _is_protected(rp):
        raise nf
    return rp


@api.get("/api/runs/{run_id}/artifacts/{name}")
def artifact(run_id: str, name: str):
    _valid_run(run_id)
    p = safe_artifact_path(run_id, name)
    return FileResponse(p, media_type=MEDIA_TYPES[p.suffix.lower()])


# ------------------------------------------------------------------ SSE
def _sse_frames(evs: list[dict], after: int) -> list[tuple[int, str, dict]]:
    out, total = [], 0.0
    cap = runner_mod.run_cap_usd()
    for e in evs:
        k, p = e["kind"], e["payload"]
        if k == "cost":
            total += float(p["usd"])
        if e["id"] <= after:
            continue
        if k in ("commentary", "tool_started", "tool_done", "error"):
            out.append((e["id"], k, {kk: p.get(kk) for kk in
                                     {"commentary": ("text",), "tool_started": ("tool",),
                                      "tool_done": ("tool", "usd"), "error": ("message",)}[k]}))
        elif k == "gate_requested":
            out.append((e["id"], "card_ready", {"gate": p["gate_id"], "step": p["step"]}))
        elif k == "cost":
            out.append((e["id"], "cost_update", {"usd": round(total, 6), "cap_usd": cap}))
        elif k == "run_status":
            out.append((e["id"], "run_status", {"status": p["status"]}))
    return out


async def sse_stream(run_id: str, last_id: int = 0, heartbeat_s: float | None = None,
                     poll_s: float = 0.2, max_seconds: float | None = None):
    hb = heartbeat_s if heartbeat_s is not None else float(os.environ.get("STUDIO_HEARTBEAT_S") or 15)
    cursor, last_sent, t0 = last_id, time.monotonic(), time.monotonic()
    yield "retry: 3000\n\n"
    while True:
        evs = await asyncio.to_thread(events.list_events, run_id)
        for eid, name, data in _sse_frames(evs, cursor):
            yield f"id: {eid}\nevent: {name}\ndata: {json.dumps(data)}\n\n"
            last_sent = time.monotonic()
        if evs:
            cursor = max(cursor, evs[-1]["id"])
        if time.monotonic() - last_sent >= hb:
            yield ": heartbeat\n\n"
            last_sent = time.monotonic()
        if max_seconds is not None and time.monotonic() - t0 >= max_seconds:
            return
        await asyncio.sleep(poll_s)


@api.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    await asyncio.to_thread(_valid_run, run_id)
    try:
        last = int(request.headers.get("last-event-id") or 0)
    except ValueError:
        last = 0

    async def gen():
        async for chunk in sse_stream(run_id, last):
            if await request.is_disconnected():
                return
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.include_router(api)

# ------------------------------------------------------------------ static UI (must be last)
_web = Path(os.environ.get("STUDIO_WEB_DIR") or REPO_ROOT / "web")
if _web.is_dir():
    app.mount("/", StaticFiles(directory=_web, html=True), name="ui")

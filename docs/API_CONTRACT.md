# Studio API contract (JSON + SSE)

Single source of truth for the backend (`src/adfarming/web.py`) and the UI (`web/index.html`, `web/app.js`).
All routes except `/healthz`, `/api/login` and static files need the session cookie set by `/api/login`. All JSON uses snake_case.
Artifact URLs in responses are relative and ready to use, e.g. `/api/runs/{id}/artifacts/c2_9x16.mp4`.

## Auth and health
- `GET /healthz` -> `{"ok": true}` (no auth, no paid calls)
- `POST /api/login {"password": str}` -> 200 sets cookie `adf_session`; 401 on wrong password. Password from env `STUDIO_PASSWORD`.
- `GET /api/me` -> `{"ok": true}` or 401 (lets the UI show the login form)

## Clients and memory
- `GET /api/clients` -> `[{"id": "dharaa", "name": "Dharaa by Smriti"}]`
- `GET /api/clients/{cid}/memory?status=approved|proposed|all` -> `[MemoryItem]`
- `POST /api/clients/{cid}/memory {"kind": str, "text": str, "pinned": bool?}` -> `MemoryItem` (human-sourced, approved)
- `PATCH /api/clients/{cid}/memory/{id} {"text": str}` -> `MemoryItem` (supersedes the old row)
- `POST /api/clients/{cid}/memory/{id}/decide {"decision": "accept"|"reject", "note": str?}` -> `MemoryItem`
- `MemoryItem` = `{"id": str, "kind": "brand_voice|hard_rule|performance|asset|preference|rejection", "text": str, "source": "human|agent_inferred", "status": "proposed|approved|rejected|superseded", "pinned": bool, "evidence": object|null, "created_at": str}`

## Runs
- `POST /api/runs {"client_id": str, "goal": str?}` -> `{"run_id": str}` (server-generated uuid; starts the agent loop)
- `GET /api/runs` -> `[{"run_id", "client_id", "status", "current_step", "created_at", "cost_usd"}]`
- `GET /api/runs/{id}` -> `RunState`
- `POST /api/runs/{id}/answer {"gate": int, "action": "approve|steer|edit|back", "text": str?, "choice": str?, "fields": object?, "reason": str?, "to": str?}` -> 202 `{"status": "accepted"}`; 409 if the gate is not the pending one; 422 on invalid action. `choice` = concept id on the ideas gate. `to` = step name for back.
- `POST /api/runs/{id}/resume` -> 202. Only for a run whose status is `interrupted` (server restarted mid-work) or `error`; 409 `not_resumable` otherwise, 409 `busy`, 402 `cost_cap`. Restarts the agent from the event log (a pending gate goes straight back to `awaiting_human`; approvals are never redone).
- `POST /api/runs/{id}/cancel` -> 202
- `GET /api/runs/{id}/log` -> `[{"seq": int, "ts": str, "step": str|null, "kind": str, "actor": "human|agent|system", "text": str, "cost_usd": float|null, "memory_ids": [str]}]` (plain-language decision log)
- `GET /api/runs/{id}/artifacts/{name}` -> file (mp4 with HTTP Range, json, webp, zip). Whitelist: names that exist directly under the run's output dir; no `/`, no `..`; never anything from `private/`.
- `GET /api/runs/{id}/events` -> SSE (`Last-Event-ID` resume). Event names: `commentary` `{"text"}`, `tool_started` `{"tool"}`, `tool_done` `{"tool","usd"}`, `card_ready` `{"gate","step"}`, `cost_update` `{"usd","cap_usd"}`, `run_status` `{"status"}`, `error` `{"message"}`. UI refetches `GET /api/runs/{id}` on `card_ready`/`run_status`.

## RunState
```json
{
  "run_id": "uuid", "client_id": "dharaa",
  "status": "running|awaiting_human|interrupted|done|error|cancelled",
  "current_step": "brief|ideas|preview|edit|check|ship",
  "steps": [{"name": "brief", "state": "done|current|pending|stale"}],
  "gate": null,
  "memory_in_play": [MemoryItem],
  "artifacts": {"concepts": [Concept]|null, "frames": [url], "cuts": {"9x16": url, "1x1": url, "16x9": url}|null, "captions": [{"start":0.0,"end":1.7,"text":"..."}], "qc": {"pass": true, "score": 5, "issues": [], "fixes": [], "summary": ""}|null, "zip": url|null},
  "cost": {"usd": 0.31, "cap_usd": 1.5, "by_tool": {"muse-spark": 0.01}},
  "commentary": [{"ts": "...", "text": "..."}]
}
```
`gate` when a person must act: `{"id": int, "step": str, "card": Card}`. `Concept` = `{"id","title","angle","hook","beats":[{"clip","src_in","src_out","purpose","on_screen_text"}],"cta","length_s","why_it_fits","risks","cites":[memory_id]}`.

## Cards (one per step; `card.type` = step name)
- `brief`: `{"type":"brief","text": str,"memory_cited":[id],"cost_estimate_usd": float}` actions: approve, steer(text), edit(fields.text)
- `ideas`: `{"type":"ideas","concepts":[Concept],"recommended": id|null,"cost_estimate_usd": float}` actions: approve with `choice` (if omitted, defaults to `recommended`; unknown choice -> 422), steer(text), edit(fields: `{concept_id?, hook?, cta?}`; no concept_id means the chosen/recommended concept), reject via steer with `reason`
- `preview`: `{"type":"preview","frames":[url],"cost_estimate_usd": float}` (cost of the NEXT paid step) actions: approve, steer, back
- `edit`: `{"type":"edit","beats":[...],"captions":[...],"hook": str,"cta": str,"cost_estimate_usd": float}` actions: approve, steer, edit(fields: `{hook?, cta?}`), back. Caption TEXT cannot be edited (burned-in captions are generated from speech; the agent reports `captions` edits as ignored), so the UI only sends hook/cta and steers for caption changes. The card also carries `cuts` `{ratio: url}`.
- `check`: `{"type":"check","verdict":{...},"history":[{"attempt":1,"score":2,"pass":false,"summary":""}],"proposed_fixes":[str]}` actions: approve (accept result), steer (apply fixes), back
- `ship`: `{"type":"ship","cuts":{...},"zip": url,"memory_proposals":[MemoryItem]}` actions: approve (finish), back. Memory proposals are decided through the memory endpoints.

## Errors
Extra codes (implemented in web.py): 409 `busy` (another run is running; also on answer/resume), 409 `not_resumable`, 409 `gate_not_pending`, 409 `not_proposed` / `not_editable` (memory routes), 429 `session_run_limit` (over 5 runs per session), 429 `rate_limited` (login), 404 `run_not_found` / `client_not_found` / `memory_not_found`.
`{"error": {"code": str, "message": str}}` with proper HTTP status. Paid tool blocked by gate -> 409 code `gate_blocked`. Cost cap hit -> 402 code `cost_cap`.

## Implementation notes
- `POST /api/runs` returns 200. `current_step` stays `ship` after a run finishes (status `done`). A cancelled run has `gate: null`.
- Env: `STUDIO_RUNNER=stub|real`; `STUDIO_AGENT=wonder|scripted|deterministic` (default wonder, with per-turn scripted fallback; `auto` is accepted as wonder); `STUDIO_CLIENT_ID` (default `dharaa`), `STUDIO_CLIENT_NAME`, `STUDIO_MEMORY_SEED` (seed dir, loaded only when that client has no memory yet). See docs/CLIENT_SWAP.md.
- With the real runner, `POST /answer` and `/resume` return immediately; status goes `running` -> `awaiting_human|done|error`. Errors reaching the client use a fixed safe message.
- Run status is stored as `run_status` events; any real runner must call `runner.set_status` the same way and do slow work in a thread, returning after `set_status('running')`.

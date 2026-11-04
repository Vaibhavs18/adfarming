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
  "artifacts": {"concepts": [Concept]|null, "frames": [url], "frames_label": str|null, "look": Look|null, "cuts": {"9x16": url, "1x1": url, "16x9": url}|null, "captions": [{"start":0.0,"end":1.7,"text":"..."}], "qc": {"pass": true, "score": 5, "issues": [], "fixes": [], "summary": ""}|null, "zip": url|null},
  "cost": {"usd": 0.31, "cap_usd": 1.5, "by_tool": {"muse-spark": 0.01}},
  "commentary": [{"ts": "...", "text": "..."}]
}
```
`gate` when a person must act: `{"id": int, "step": str, "card": Card}`. `Concept` = `{"id","title","angle","hook","beats":[{"clip","src_in","src_out","purpose","on_screen_text"}],"cta","length_s","why_it_fits","risks","cites":[memory_id]}`.

## Cards (one per step; `card.type` = step name)
- `brief`: `{"type":"brief","text": str,"memory_cited":[id],"cost_estimate_usd": float}` actions: approve, steer(text), edit(fields.text)
- `ideas`: `{"type":"ideas","concepts":[Concept],"recommended": id|null,"cost_estimate_usd": float,"cost_items":[{"what","usd"}]?}` (composed look: the estimate is the Preview step = plan + matte stills + plate) actions: approve with `choice` (if omitted, defaults to `recommended`; unknown choice -> 422), steer(text), edit(fields: `{concept_id?, hook?, cta?}`; no concept_id means the chosen/recommended concept), reject via steer with `reason`
- `preview`: `{"type":"preview","frames":[url],"frames_kind":"composed|storyboard","frames_label": str,"look": Look|null,"summary": str,"cost_estimate_usd": float,"cost_items":[{"what": str,"usd": float}]?}` (cost of the NEXT paid step, itemised: SAM video + render + QC, plus any plate still missing) actions: approve, steer, edit(fields: `{hook?, cta?, title?, callout?, preset?}`; `headline` is an alias of `hook`), back. `frames_kind:"composed"` = stills of the REAL speaker cut out (SAM) over the planned backgrounds with the planned text (PNG, 540x960, one per background); `look` then describes the plan and `frames_label` is its label. `frames_kind:"storyboard"` (STUDIO_LOOK=simple, or the composed preview failed and fell back) = the old Muse Image stand-ins, `look: null`, `frames_label` says they are AI stand-ins. Steer text goes to the planner verbatim and makes a new plan version; edited hook/cta/title/callout/preset are applied to the plan by code, locked (they survive every re-plan) and the stills are re-rendered.
- `Look` = `{"summary":[str] (plain-language bullets: preset, backgrounds, headline, callout, captions, picture card), "plan_id":"plan.v2","preset":"cutout_on_brand_colour|cutout_on_textile_plate|cutout_over_blurred_scene|split_with_pip_card","mode":"composed|simple_fallback","label": str,"notes":[str] (e.g. a plate that could not be made),"source":"spark|default","editable":["hook","cta","title","callout","preset"]}`. On the `edit`/`check` cards `look.mode` is `composed`, or `simple_fallback` when the composed render failed and the simple cuts were delivered (then `look.reason` says why and `summary` is empty); `look` is `null` for the simple look.
- `edit`: `{"type":"edit","beats":[...],"captions":[...],"hook": str,"cta": str,"look": Look|null,"cost_estimate_usd": float}` actions: approve, steer (look steers such as "calmer background" re-plan and re-render with the master reused; content steers rebuild the cut), edit(fields: `{hook?, cta?, title?, callout?, preset?}`), back. Caption TEXT cannot be edited (burned-in captions are generated from speech; the agent reports `captions` edits as ignored), so the UI only sends hook/cta and steers for caption changes. The card also carries `cuts` `{ratio: url}`.
- `check`: `{"type":"check","verdict":{...},"history":[{"attempt":1,"score":2,"pass":false,"summary":""}],"proposed_fixes":[str]}` actions: approve (accept result), steer (apply fixes), back
- `ship`: `{"type":"ship","cuts":{...},"zip": url,"memory_proposals":[MemoryItem]}` actions: approve (finish), back. Memory proposals are decided through the memory endpoints.

## Composed look (agent tools, STUDIO_LOOK)
`STUDIO_LOOK=composed|simple` (default `composed`; `simple` = the old talking-head cut + AI storyboard, instant demo fallback). A run that hits a composed failure writes `look.json` = simple and logs a `fallback` event (`{"step","from":"composed","to":"simple","scope":"look","reason","detail"}`, shown in `/log` as "Fell back from composed to simple: ...").
Tools (agent-side; the HTTP API is unchanged) and their gates, enforced in code in `agent/tools.py`: `plan_composition` (free/cheap, Spark; step preview/edit/check; new `plan.vN.json` per call; the human's locked hook/cta/title/callout/preset are re-applied by code), `backdrops` (PAID, needs the Ideas approval; Muse Image plates, tinted/dimmed, cached per run), `preview_composition` (PAID cheap, needs the Ideas approval; SAM image cut-out of the real speaker at each background's midpoint + the deterministic preview renderer; falls back to `storyboard`), `render_composition` (PAID, needs the Preview approval; SAM video matte of the master (cached) + composed 9:16 + derived 1:1 (crop around the cut-out from `head_top_frac`) and 16:9 (the composed 9:16 centred over a blurred fill of itself) + QC on the composed 9:16; falls back to the simple cuts). `build_cuts` still builds the speech-snapped master and the simple cuts (the fallback look); in the composed look it skips QC. A paid tool in the same batch as `ask_human`, or before its approval, is refused and logged (`gate_blocked_attempt`).
Costs are recorded under `sam-3.1-still`, `sam-3.1-matte`, `muse-image`, `ffmpeg-render`, `muse-spark`, `muse-wonder`; run seconds of the render are in `tool_done.secs` and `meta.json.composed.render_seconds`. `cost_estimate_usd` on the Ideas card is the Preview step and on the Preview card the Edit step (SAM video seconds x 15 fps x $0.0002 with the 50-frame minimum, render, QC). Artifacts: `plan` (step preview), `edit_plan` (step edit), `frames`, `cuts`, `qc`; Back marks later ones stale. Files in a run dir (all flat, servable): `look_<concept>_v<N>_<i>.png` (composed stills), `look_<concept>_v<N>_contact.png`, `<concept>_9x16|1x1|16x9.mp4`, `<concept>_simple_<ratio>.mp4` (the fallback cuts), `plan.json`, `plan.vN.json`.

## Errors
Extra codes (implemented in web.py): 409 `busy` (another run is running; also on answer/resume), 409 `not_resumable`, 409 `gate_not_pending`, 409 `not_proposed` / `not_editable` (memory routes), 429 `session_run_limit` (over 5 runs per session), 429 `rate_limited` (login), 404 `run_not_found` / `client_not_found` / `memory_not_found`.
`{"error": {"code": str, "message": str}}` with proper HTTP status. Paid tool blocked by gate -> 409 code `gate_blocked` (tool result codes inside the agent also include `frames_outdated`, `missing_artifact`, `not_composed`, `not_allowed_in_step`). Cost cap hit -> 402 code `cost_cap`.

## Implementation notes
- `POST /api/runs` returns 200. `current_step` stays `ship` after a run finishes (status `done`). A cancelled run has `gate: null`.
- Env: `STUDIO_RUNNER=stub|real`; `STUDIO_AGENT=wonder|scripted|deterministic` (default wonder, with per-turn scripted fallback; `auto` is accepted as wonder); `STUDIO_CLIENT_ID` (default `dharaa`), `STUDIO_CLIENT_NAME`, `STUDIO_MEMORY_SEED` (seed dir, loaded only when that client has no memory yet). Multi-client: when `ADF_PRIVATE/clients/` exists, `GET /api/clients` lists exactly the clients discovered from `clients/*/client.json` (plus the flat default if flat data exists), `POST /api/runs` returns 404 `client_not_found` for any other id, and every run reads that client's own brief/footage/ASR/memory. Without a `clients/` dir the old flat single-client layout applies. See docs/CLIENT_SWAP.md.
- With the real runner, `POST /answer` and `/resume` return immediately; status goes `running` -> `awaiting_human|done|error`. Errors reaching the client use a fixed safe message.
- Run status is stored as `run_status` events; any real runner must call `runner.set_status` the same way and do slow work in a thread, returning after `set_status('running')`.

# Studio web UI

No-build static app (Preact + htm from jsDelivr, exact versions pinned in `index.html`). Coded against `docs/API_CONTRACT.md`. The step-by-step guide for people is in the repo `README.md`.

## Run against the real backend
Serve this folder from the FastAPI app (`src/adfarming/web.py`), same origin as `/api`.

## Mock mode (no backend, no AI)
```
python3 -m http.server 8777 --directory web
open "http://localhost:8777/?mock=1"
```
Any non-empty password works. Fictional client "Demo Saree Brand". Fixtures: `mock/fixtures.json`, engine: `mock/mock.js`, placeholder video: `mock/demo.mp4`.

Jump straight to a step (for screenshots): `?mock=1&at=ideas|preview|edit|check|ship#/run/demo-run-1`.

## Replay mode (a real session, still no AI)
`?mock=1&replay=<token>` loads `/replay-<token>/fixtures.json` and `/replay-<token>/media/*` from the site root instead of the fictional fixtures, and shows a banner saying so. The Studio walks through the recorded gates: whatever you click, it shows what the real reviewer did next (and says so when you chose differently), with short pauses that report the real duration ("That took 3m 1s in the real run"). `&at=<step>` works here too. A wrong or missing token falls back to the fictional demo with a warning banner.

The data is real client material, so it never lives in git. Build and stage it:
```
# 1. copy the run dir(s), the Studio SQLite DB and the API JSON of the run into private/replay/src/ (layout: scripts/build_replay.py docstring)
.venv/bin/python scripts/build_replay.py --run <run_id> --src private/replay/src --out private/replay/<name>
# 2. assemble the Vercel folder in /tmp/adf_vercel/adfarming-studio-preview (does not deploy; prints the private link to the terminal)
scripts/stage_vercel_preview.sh <name>
# 3. check it locally
python3 -m http.server 8792 --directory /tmp/adf_vercel/adfarming-studio-preview
open "http://localhost:8792/?mock=1&replay=$(cat private/replay/token.txt)"
```
`private/` is gitignored; `token.txt` is a random folder name that makes the link unguessable. Do not paste the link into tracked files or public chats. Tests: `pytest tests/replay`.

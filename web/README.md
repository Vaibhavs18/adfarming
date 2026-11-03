# Studio web UI

No-build static app (Preact + htm from jsDelivr, exact versions pinned in `index.html`). Coded against `docs/API_CONTRACT.md`.

## Run against the real backend
Serve this folder from the FastAPI app (`src/adfarming/web.py`), same origin as `/api`.

## Mock mode (no backend)
```
python3 -m http.server 8777 --directory web
open "http://localhost:8777/?mock=1"
```
Any non-empty password works. Fictional client "Demo Saree Brand". Fixtures: `mock/fixtures.json`, engine: `mock/mock.js`, placeholder video: `mock/demo.mp4`.

Jump straight to a step (for screenshots): `?mock=1&at=ideas|preview|edit|check|ship#/run/demo-run-1`.

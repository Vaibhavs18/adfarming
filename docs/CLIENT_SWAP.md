# Swapping the client (no code changes)

The Studio is client-agnostic. To run it for a different (consented) client, change files and env only.

## Files (all under the private data dir, `ADF_PRIVATE`, default `<repo>/private`; in Docker `/data`)
| Path | Purpose |
|---|---|
| `brief.md` | The client brief the agent reads at every step (tone, product, goals, hard rules) |
| `subject.txt` | One line describing the on-camera subject, used for subject tracking (e.g. `an Indian woman in a saree`) |
| `refs.md` | Optional reference notes |
| `footage/*.mov` (or .mp4) | Source clips. Each is transcribed once by Voice Transcribe and cached in `asr/<stem>.json` (delete stale `asr/` and `audio/` caches when swapping footage with the same file names) |
| seed dir | `approved.jsonl`, one JSON memory item per line (`id, kind, text, source, status, pinned, evidence, ...`); see `private/research/seed_memory_dharaa/approved.jsonl` for the format |

## Env
```
ADF_PRIVATE=/path/to/standin_private      # brief.md, subject.txt, footage/, asr/, audio/
STUDIO_CLIENT_ID=standin                  # default dharaa
STUDIO_CLIENT_NAME="Stand-in Client"      # shown in the UI picker
STUDIO_MEMORY_SEED=/path/to/standin_seed  # directory containing approved.jsonl
STUDIO_RUNNER=real
```
Use a fresh `ADFARMING_DB` (or accept that old clients stay listed; they are harmless). The seed is loaded at server start only when the client has no memory yet, so edits made in the UI are never overwritten. Memory ids are namespaced `<client_id>:<seed id>`.

## Checklist
1. Create the private dir with `brief.md`, `subject.txt`, `footage/`.
2. Write the seed `approved.jsonl` (hard rules, brand voice, preferences) for the new client.
3. Start the server with the env above; `GET /api/clients` should list the new client.
4. Run `scripts/http_e2e.py --client standin` once before the demo.

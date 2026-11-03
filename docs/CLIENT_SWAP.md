# Clients: adding and swapping (no code changes)

One deployment serves several clients. Each has its own footage, brief, subject, refs, ASR cache and client memory,
selectable in the UI (client picker when creating a run). A run is bound to its client, so two clients never see each
other's data.

## Layout (under the private data dir `ADF_PRIVATE`, default `<repo>/private`; in Docker `/data/private`)
```
ADF_PRIVATE/clients/<client_id>/
  client.json          {"id","name","subject","language"}   (its presence makes the client discoverable)
  brief.md             the brief the agent reads at every step (tone, product, goals, hard rules)
  subject.txt          one line on the on-camera subject, used for subject tracking / storyboard
  refs.md              optional reference / past-ad notes
  rules.md             optional, one rule per line: "DO: ..." / "DON'T: ..." / "+ ..." / "- ..." -> hard_rule memory
  seed_memory.jsonl    optional, initial memory items in the seed JSONL format (id, kind, text, source, status, pinned, ...)
  footage/*.mov|mp4    source clips (never modified)
  asr/<stem>.json      Voice Transcribe cache (written atomically; ship it to save spend)
  audio/<stem>.wav     extracted audio cache
```
`client_id`: lowercase letters, digits, `-`, `_` (max 40). Memory ids are namespaced `<client_id>:<seed id>`.

**Backward compatible flat layout.** If `ADF_PRIVATE/clients/` does not exist, the old layout (`ADF_PRIVATE/{brief.md,subject.txt,
footage/,asr/,audio/}`) is the single client `STUDIO_CLIENT_ID` (default `dharaa`; name from `STUDIO_CLIENT_NAME`; seed from
`STUDIO_MEMORY_SEED`). If both exist, `clients/<id>` wins and the flat default is listed only while it still has flat data.
Those three env vars are ignored for clients that live under `clients/`.

## Add a client
```
python scripts/add_client.py --id acme --name 'Acme Tea' --from ~/Downloads/acme_pack [--subject 'a tea taster'] [--language Hinglish] [--no-asr] [--force]
python scripts/list_clients.py
```
`--from` is any folder: loose `*.mp4/*.mov/*.m4v` anywhere inside are copied (not transcoded) to `footage/`; `brief.md|txt|docx`
becomes `brief.md`; `rules.*` / `dos_and_donts.*` become `seed_memory.jsonl` (`hard_rule`, `source=human`, `status=approved`);
`refs.*` / `notes.*` / `past*` become `refs.md`. A missing brief or subject is asked on stdin (Enter keeps a clearly marked
placeholder; edit it before the demo). It prints the estimated ASR cost first (Meta Voice Transcribe, about $0.003 per clip)
and then transcribes each clip; `--no-asr` skips this (clips are then transcribed on first use, which is paid too). Re-running
without `--force` is refused; with `--force` it refreshes brief/rules/client.json and adds new clips, never deleting files.

## Start / deploy
```
ADF_PRIVATE=/path/to/private STUDIO_RUNNER=real python -m uvicorn adfarming.web:app
```
At start the server lists every `clients/*/client.json` in `GET /api/clients` and seeds each client's memory from its seed file
only when that client has no memory yet (idempotent; edits made in the UI are never overwritten). Only listed clients can be
used for runs. On the VM push one client at a time (rsyncs only `private/clients/<id>/`, asks you to type "yes"):
```
bash deploy/scripts/push_client_data.sh acme deploy@<IP> --dry-run
bash deploy/scripts/push_client_data.sh acme deploy@<IP>
```
then restart the app. The whole `ADF_PRIVATE` tree (including `clients/`) is never served as an artifact.

## Checklist for a new client
1. `add_client.py` with the human's pack (confirm clips are consented for use).
2. Read `brief.md` and `subject.txt`; fix placeholders; add rules to `rules.md` and re-run with `--force` before the first start
   (memory is only seeded while empty; later edits go through the UI memory panel).
3. Start the server; `GET /api/clients` lists the client.
4. Run `scripts/http_e2e.py --client <id>` once before the demo.
5. CLI: `scripts/run_pipeline.py RUN_ID --client <id>`.

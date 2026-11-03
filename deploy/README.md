# Deploying the Studio to one Vultr VM

Stack: Docker Compose on one Ubuntu VM. `caddy` (80/443, automatic HTTPS, basic-auth gate) in front of `app`
(`uvicorn adfarming.web:app`, port 8000 internal only, never published). State lives in bind mounts under
`/opt/adfarming/data` (`private/` client data, `out/` runs, `db/studio.db`). Nothing confidential is ever in git
or in the image. Steps marked **[HUMAN]** need you (account, money, secrets, decisions). Everything else is a command.

Layout on the VM: `/opt/adfarming/{.env, src/, data/{private,out,db}, backups/, releases.log, current_tag}`.
Scripts live in `deploy/scripts/`, run from the repo root on your Mac. `HOST` below means `deploy@<VM_IP>`.

## Runbook

| # | Step | Who | Time |
|---|---|---|---|
| 1 | Vultr account and billing | HUMAN | 10-20 min |
| 2 | SSH key | HUMAN | 2 min |
| 3 | Create the VM | HUMAN | 5 min |
| 4 | Firewall group | HUMAN | 5 min |
| 5 | Decide hostname (sslip.io or domain) | HUMAN | 2 min |
| 6 | Decide which client data may be on the server | HUMAN | 5 min, a real decision |
| 7 | Bootstrap the VM | run | 5 min |
| 8 | Type secrets into `/opt/adfarming/.env` | HUMAN | 5 min |
| 9 | Choose the judge password and write the hash | HUMAN + run | 3 min |
| 10 | Deploy | run | 5-10 min first time (image build), 1-2 min later |
| 11 | Push the allowed client data | HUMAN confirms, run | 2-15 min depending on footage |
| 12 | Smoke test, go/no-go | run | 2 min |

### 1. [HUMAN] Vultr account and billing
Create the account at vultr.com with an email never used on Vultr (needed for the Agent Rush $200 credit; the code
arrives at kickoff, redeem it under Billing). Add a payment method or small prepay if asked. Set a billing alert at
$10 in the console. Stop and ask if anything wants more than about $10.

### 2. [HUMAN] SSH key
```bash
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C adfarming
cat ~/.ssh/id_ed25519.pub        # paste into Vultr: Account > SSH Keys > Add
```

### 3. [HUMAN] Create the VM (console)
Products > Deploy > Cloud Compute, Regular Performance (not GPU), location **New Jersey (ewr)**, image **Ubuntu 24.04 x64**,
plan **vc2-2c-4gb** (2 vCPU, 4 GB, about $20/month billed hourly), tick your SSH key, hostname `adfarming`, disable
auto-backups unless you want the extra cost. Note the public IPv4 as `IP`. Wait until status is Running (1-2 min).

### 4. [HUMAN] Firewall group (console: Products > Network > Firewall)
Create group `adfarming`, rules: TCP 22 from **your IP only**, TCP 80 and TCP 443 from anywhere (`0.0.0.0/0`, `::/0`),
nothing else. Attach it to the VM. (The VM's own `ufw` is a second layer, set in step 7. Do NOT add port 8000.)

### 5. [HUMAN] Hostname
No domain needed: use `<IP with dashes>.sslip.io`, for example IP `203.0.113.10` gives `203-0-113-10.sslip.io`. If cert
issuance fails (see troubleshooting) try `.nip.io`, or buy a domain and add an A record to the IP. You will type the
chosen value as `SITE_ADDRESS` in step 8.

### 6. [HUMAN] Which client data may go on the server
The server is public-facing. Default: **demo/synthetic client only**. The confidential Dharaa footage stays off the
server unless Kiran agrees in writing. Decide now, because step 11 uploads exactly the directory you name.

### 7. Bootstrap the VM (as root, once)
```bash
IP=203.0.113.10
ssh root@$IP 'bash -s' < deploy/scripts/bootstrap_vm.sh
ssh deploy@$IP 'docker compose version && ls -ld /opt/adfarming/data/*'    # check: user "deploy" works and owns the dirs
```
Installs Docker plus the compose plugin, ufw (22 rate-limited, 80, 443), fail2ban, user `deploy` (uid 1000, same as the
container user), the directories, an empty `/opt/adfarming/.env` (mode 600), and key-only ssh. Re-running is safe.

### 8. [HUMAN] Type the secrets on the VM yourself
Never paste keys into chat or into a file in this repo.
```bash
ssh deploy@$IP
nano /opt/adfarming/.env          # fill in from deploy/.env.example (copy the names; the file is mode 600)
openssl rand -hex 32              # run this to get a value for STUDIO_SECRET
exit
```
Required: `META_MODEL_API_KEY`, `BASETEN_API_KEY`, `STUDIO_PASSWORD`, `STUDIO_SECRET`, `STUDIO_RUNNER=real`,
`STUDIO_AGENT`, `SITE_ADDRESS`, `CADDY_BASIC_AUTH_HASH` (step 9). Optional: caps `STUDIO_RUN_CAP_USD` (1.5) and
`STUDIO_DAILY_CAP_USD` (15), `STUDIO_CLIENT_ID/NAME`, `STUDIO_MEMORY_SEED`. To test the plumbing for free first, set
`STUDIO_RUNNER=stub` (no paid calls).

### 9. [HUMAN] Choose the judge password and create its hash
Caddy refuses to start with an invalid hash, so create a real one before the first deploy (any password, you replace it
in step 10b with a generated one):
```bash
ssh -t deploy@$IP 'docker run --rm -it caddy:2.10-alpine caddy hash-password'   # type a password (hidden), prints a $2a$ hash
```
Put it in `.env` in **single quotes**: `CADDY_BASIC_AUTH_HASH='$2a$14$...'`, and set `STUDIO_PASSWORD` to the same password.
Afterwards `rotate_judge.sh` (10b) replaces all three of `CADDY_BASIC_AUTH_HASH`, `STUDIO_PASSWORD`, `STUDIO_SECRET`
with a strong generated credential. Use 24+ random characters if you keep your own.

### 10. Deploy
```bash
bash deploy/scripts/deploy.sh deploy@$IP
```
It refuses if `.env` is missing, not mode 600, or lacks the required names. It rsyncs code (never `private/`, `out/`,
`.venv/`, `.git/`, `.env`), builds the amd64 image on the VM, starts the stack, waits for the container to be healthy and
for `https://SITE_ADDRESS/healthz`, and appends the release tag to `releases.log`.

10b. **[HUMAN] rotate to a fresh judge credential** (do this once now, then after recording and after judging):
```bash
bash deploy/scripts/rotate_judge.sh deploy@$IP     # prints the new password ONCE; copy it into your password manager
```

### 11. [HUMAN confirms] Push client data (one client at a time)
Each client lives in `private/clients/<id>/` (`client.json`, `brief.md`, `subject.txt`, `footage/`, `asr/`, optional
`refs.md`, `rules.md` or `seed_memory.jsonl`). Create one locally with `python scripts/add_client.py --id <id> --name '<Name>' --from <dir>`
(see docs/CLIENT_SWAP.md), then:
```bash
bash deploy/scripts/push_client_data.sh <id> deploy@$IP --dry-run   # list only
bash deploy/scripts/push_client_data.sh <id> deploy@$IP             # asks you to type "yes"
```
Only `private/clients/<id>/` is sent (to `/opt/adfarming/data/private/clients/<id>/`; the container sees `/data/private/clients/<id>/`),
env/key files are refused, nothing on the server is deleted. Restart the app afterwards (`docker compose restart app`, or re-run
`deploy.sh`): at start it discovers every `clients/*/client.json`, lists it in the UI and seeds the client's memory from
`seed_memory.jsonl`/`rules.md` only if that client has no memory yet. Shipping `asr/` saves transcription spend.

### 12. Smoke test, go/no-go
```bash
bash deploy/scripts/smoke.sh https://203-0-113-10.sslip.io        # prompts for the judge password (hidden)
```
**GO** only if it prints `SMOKE: GO`. It checks: `/healthz` open with a valid cert, `/` and `/api/clients` return 401
without credentials, 200 with them, `/.env /private/ /out/` hidden, app login works, and `http://IP:8000/` is
unreachable. Then do the human checks: open the URL on your phone, log in, start a stub or real run, restart the app
mid-run (`ssh deploy@$IP 'cd /opt/adfarming/src && docker compose --env-file /opt/adfarming/.env -f deploy/docker-compose.yml restart app'`),
reload, and confirm the same pending card shows. If healthz is not live behind auth by the go/no-go time, stop and
decide with Vaibhav (alternate cheap VPS with the same compose files); a laptop tunnel is never the submitted URL.

## Day-2 commands
| Task | Command |
|---|---|
| Redeploy after code changes | `bash deploy/scripts/deploy.sh deploy@$IP` |
| Roll back to the previous build | `bash deploy/scripts/rollback.sh deploy@$IP` (`--list` shows tags, or pass a TAG) |
| Backup runs and DB to your Mac | `bash deploy/scripts/backup.sh deploy@$IP` (default `~/adfarming-backups`) |
| Rotate the judge password | `bash deploy/scripts/rotate_judge.sh deploy@$IP` (interrupts runs in progress) |
| Logs | `ssh deploy@$IP 'cd /opt/adfarming/src && docker compose --env-file /opt/adfarming/.env -f deploy/docker-compose.yml logs -f --tail=100'` |
| Pipeline CLI inside the image | `docker run --rm --entrypoint python -v /opt/adfarming/data/private:/data/private -v /opt/adfarming/data/out:/data/out --env-file /opt/adfarming/.env adfarming:latest scripts/run_pipeline.py --help` |
| Snapshot before demo day | Vultr console > Instance > Snapshots > Take [HUMAN] |
| Tear down after judging | Vultr console > Instance > Destroy [HUMAN, destroys data: back up first] |

Env switches worth knowing: `PRIVATE_MODE=ro` in `.env` mounts client data read-only (only once `asr/` and `audio/` are
pushed and nothing new is transcribed, otherwise the pipeline cannot write its cache).

## Troubleshooting
- **HTTPS cert not issued** (deploy says healthz did not answer; `docker compose logs caddy` shows `429`, `rateLimited`
  or challenge errors): check the firewall allows 80 and 443 from anywhere and `ufw status`; confirm `dig +short $SITE_ADDRESS`
  returns the VM IP; sslip.io is a shared domain and can hit Let's Encrypt limits. Caddy falls back to ZeroSSL by itself;
  wait a few minutes. Otherwise change `SITE_ADDRESS` to `<ip-dashes>.nip.io` in `.env`, then
  `ssh deploy@$IP 'cd /opt/adfarming/src && docker compose --env-file /opt/adfarming/.env -f deploy/docker-compose.yml up -d --force-recreate caddy'`.
  Last resort: own domain with an A record. Keep the `caddy_data` volume; deleting it burns issuance quota.
- **Live progress (SSE) arrives in bursts or only at the end**: something is buffering. `/api/runs/*/events` is proxied
  with `flush_interval -1` and no compression in `Caddyfile`; check no other proxy or CDN sits in front, and test with
  `curl -N -u judge:... https://HOST/api/runs/<id>/events`. Check the app heartbeat (`STUDIO_HEARTBEAT_S`, default 15 s).
- **Out of disk** (`df -h /`, `docker system df`): run outputs and old images pile up. Back up first (`backup.sh`), then
  `ssh deploy@$IP 'docker image prune -af --filter until=72h; docker builder prune -af'` (keeps recent tags; after that,
  rollback only reaches the images still present). Old runs: delete directories under `/opt/adfarming/data/out/` only after
  backup, and stop the app first if the DB references them.
- **Meta rate limits or 429s / 5-minute slow calls**: the app has per-run and per-day spend caps
  (`STUDIO_RUN_CAP_USD`, `STUDIO_DAILY_CAP_USD`); Caddy waits up to 5 minutes for a response. Retry after a minute; check the
  Meta balance on their dashboard; lower concurrency by running one session at a time.
- **App unhealthy or restarting**: `docker compose ... logs --tail=100 app`; usual causes are a missing key name in `.env`,
  `STUDIO_RUNNER` misspelled (only `stub` or `real`), or data dirs not writable (`ls -ln /opt/adfarming/data` should show uid 1000).
- **Login always fails after deploy**: you changed `STUDIO_SECRET`/`STUDIO_PASSWORD` without `up -d`. Env changes need
  `docker compose ... up -d --force-recreate app`.
- **`docker compose` reports a missing variable**: always pass `--env-file /opt/adfarming/.env`, or use the scripts.
- **Docker published ports bypass ufw**: only Caddy publishes 80/443; `app` uses `expose` only. Never add `ports:` to `app`.

## Security notes
- Two gates: Caddy basic auth (judge) and the app's own login. `rotate_judge.sh` keeps both passwords equal and invalidates
  old session cookies. `/healthz` is the only open path. `.env*`, `/private`, `/out` are 404 even with credentials.
- Keys exist only in `/opt/adfarming/.env` (mode 600) and the app container environment; Caddy only gets the hash.
  Logs and viewer pages must not contain keys; the leak grep from the plan applies.
- The `deploy` user is in the `docker` group, which is root-equivalent: keep it key-only (bootstrap disables ssh passwords).
- Client data: never in git, never in the image (`.dockerignore`). Back up (`backup.sh`) excludes client data on purpose.

## Vultr Agent Rush Challenge 2 notes ("Powering the Future of Work")
What to say in the submission:
- **Vultr VM backend**: the whole product runs on one Vultr Cloud Compute instance (`vc2-2c-4gb`, ewr): FastAPI app,
  ffmpeg/Pillow render workers, SQLite run state, Caddy for HTTPS. State and outputs on the VM's own disk.
- **Public web deployment**: a real HTTPS URL on the VM (sslip.io or own domain), judged without any local setup, behind a
  judge credential, with the app port closed to the internet and a firewall of 22/80/443.
- **Multi-step enterprise workflow**: an agency ad-production workflow (marketing operations) with six steps
  (brief, concepts, approval, storyboard, cuts, QC/ship), human approval gates, spend caps and approvals before paid tools,
  a durable run log (survives restarts mid-pause), client memory/rules that persist across runs, and rollback, backup and
  credential rotation scripts as the operational story.
- Honest limits: single VM, no autoscaling; Serverless Inference is not used unless you add it (optional, separate key).
- Evidence to attach: the live URL, `docs/BUILD_LOG.md`, `deploy/` (reproducible deploy), and a short screen recording.

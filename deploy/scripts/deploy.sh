#!/usr/bin/env bash
# Deploy the current working tree to the VM and (re)start the stack.
#   bash deploy/scripts/deploy.sh deploy@<IP>
# rsyncs code (never private/, out/, .venv/, .git/, .env), builds the image ON the VM (amd64),
# `docker compose up -d`, waits for https /healthz, prints status. Refuses if /opt/adfarming/.env
# is missing or lacks the required names. Idempotent. No secret values are read or printed.
set -euo pipefail
# shellcheck source=deploy/scripts/_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

HOST="${1:-${DEPLOY_HOST:-}}"
[ -n "$HOST" ] || die "usage: deploy.sh deploy@<IP>   (or set DEPLOY_HOST)"
check_host "$HOST"
need ssh; need rsync
ROOT="$(repo_root)"
[ -f "$ROOT/Dockerfile" ] && [ -f "$ROOT/deploy/docker-compose.yml" ] || die "run from the adfarming repo (Dockerfile / deploy/docker-compose.yml missing)"

REV="nogit"
if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
  REV="$(git -C "$ROOT" rev-parse --short HEAD)"
  [ -z "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ] || REV="${REV}-dirty"
fi
TAG="${REV}-$(date -u +%m%d%H%M%S)"

info "checking the VM and /opt/adfarming/.env (names only)"
remote_bash "$HOST" <<'REMOTE'
set -euo pipefail
ENVF=/opt/adfarming/.env
[ -s "$ENVF" ] || { echo "REFUSING: $ENVF is missing or empty. Type your secrets into it on the VM (deploy/README.md step 8)." >&2; exit 1; }
mode="$(stat -c %a "$ENVF")"
[ "$mode" = "600" ] || { echo "REFUSING: $ENVF mode is $mode, expected 600 (chmod 600 $ENVF)" >&2; exit 1; }
missing=""
for k in SITE_ADDRESS CADDY_BASIC_AUTH_HASH STUDIO_PASSWORD STUDIO_SECRET; do
  grep -Eq "^${k}=.+" "$ENVF" || missing="$missing $k"
done
runner="$(grep -E '^STUDIO_RUNNER=' "$ENVF" | tail -n1 | cut -d= -f2- || true)"
if [ "${runner:-stub}" = "real" ]; then
  for k in META_MODEL_API_KEY BASETEN_API_KEY; do
    grep -Eq "^${k}=.+" "$ENVF" || missing="$missing $k"
  done
fi
[ -z "$missing" ] || { echo "REFUSING: empty or missing in $ENVF:$missing" >&2; exit 1; }
command -v docker >/dev/null && docker compose version >/dev/null || { echo "docker/compose missing: run bootstrap_vm.sh" >&2; exit 1; }
echo "env file OK (STUDIO_RUNNER=${runner:-stub})"
REMOTE

info "rsync code -> ${HOST}:${REMOTE_SRC}/ (excluding private, out, .venv, .git, .env)"
rsync -az --delete \
  --exclude '/.git/' --exclude '/.venv/' --exclude '/private/' --exclude '/out/' --exclude '/data/' \
  --exclude '.env' --exclude '.env.*' \
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/' --exclude '.DS_Store' \
  --exclude 'node_modules/' --exclude '/.claude/' --exclude '*.mp4' --exclude '*.mov' --exclude 'footage/' \
  -e "ssh ${SSH_OPTS[*]}" \
  "$ROOT"/ "${HOST}:${REMOTE_SRC}/"

info "building ${TAG} on the VM and starting the stack (first build takes a few minutes)"
remote_bash "$HOST" "$TAG" <<REMOTE
$REMOTE_PRELUDE
TAG="\$1"
export TAG
dc build app
docker tag "adfarming:\$TAG" adfarming:latest
log=\$ROOT/releases.log
touch "\$log"
printf '%s %s\n' "\$(date -u +%FT%TZ)" "\$TAG" >> "\$log"
printf '%s\n' "\$TAG" > "\$ROOT/current_tag"
dc up -d --remove-orphans

cid="\$(dc ps -q app)"
echo "waiting for the app container to become healthy..."
for i in \$(seq 1 45); do
  st="\$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "\$cid" 2>/dev/null || echo gone)"
  [ "\$st" = "healthy" ] && break
  [ "\$st" = "gone" ] && { echo "app container is gone"; dc logs --tail=40 app; exit 1; }
  sleep 4
done
[ "\$st" = "healthy" ] || { echo "app is \$st, not healthy. Last logs:"; dc logs --tail=60 app; exit 1; }

site="\$(env_val SITE_ADDRESS)"
echo "waiting for https://\$site/healthz (Caddy may still be fetching the certificate)..."
ok=0
for i in \$(seq 1 45); do
  if curl -fsS --max-time 8 --resolve "\$site:443:127.0.0.1" "https://\$site/healthz" >/dev/null 2>&1; then ok=1; break; fi
  sleep 4
done
dc ps
docker image ls adfarming --format 'image {{.Repository}}:{{.Tag}} {{.Size}}' | head -n 6
docker image prune -f >/dev/null
if [ "\$ok" -ne 1 ]; then
  echo "HTTPS healthz did not answer in 3 minutes. Caddy log tail:"; dc logs --tail=40 caddy
  exit 1
fi
echo "healthz OK on https://\$site/healthz"
REMOTE
info "deployed ${TAG}. Next: bash deploy/scripts/smoke.sh https://<SITE_ADDRESS>"

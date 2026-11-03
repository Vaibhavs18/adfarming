#!/usr/bin/env bash
# Roll the app back to a previously built image tag (images stay on the VM; no rebuild).
#   bash deploy/scripts/rollback.sh deploy@<IP> [TAG]       (no TAG: the release before the current one)
#   bash deploy/scripts/rollback.sh deploy@<IP> --list
# Note: this reverts code only. Data under /opt/adfarming/data is untouched.
set -euo pipefail
# shellcheck source=deploy/scripts/_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

HOST="${1:-${DEPLOY_HOST:-}}"; WANT="${2:-}"
[ -n "$HOST" ] || die "usage: rollback.sh deploy@<IP> [TAG|--list]"
check_host "$HOST"
need ssh
case "$WANT" in
  "" | --list) : ;;
  *[!A-Za-z0-9._-]*) die "TAG may only contain letters, digits, . _ -";;
esac

remote_bash "$HOST" "${WANT:-auto}" <<REMOTE
$REMOTE_PRELUDE
WANT="\$1"
log="\$ROOT/releases.log"
[ -s "\$log" ] || { echo "no releases.log on the VM: nothing to roll back to" >&2; exit 1; }
cur="\$(cat "\$ROOT/current_tag" 2>/dev/null || true)"
echo "current: \${cur:-unknown}"
echo "releases (oldest first):"; awk '{print "  " \$1 "  " \$2}' "\$log"
[ "\$WANT" != "--list" ] || exit 0
if [ "\$WANT" = "auto" ]; then
  prev=""
  for t in \$(awk '{print \$2}' "\$log" | awk '!seen[\$0]++'); do
    [ "\$t" = "\$cur" ] && break
    prev="\$t"
  done
  [ -n "\$prev" ] || { echo "no release older than \$cur" >&2; exit 1; }
  WANT="\$prev"
fi
docker image inspect "adfarming:\$WANT" >/dev/null 2>&1 || { echo "image adfarming:\$WANT not on this VM (pruned?)" >&2; exit 1; }
echo "rolling back to \$WANT"
docker tag "adfarming:\$WANT" adfarming:latest
TAG="\$WANT"; export TAG
printf '%s\n' "\$WANT" > "\$ROOT/current_tag"
dc up -d --no-build --remove-orphans
cid="\$(dc ps -q app)"
for i in \$(seq 1 30); do
  st="\$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "\$cid" 2>/dev/null || echo gone)"
  [ "\$st" = "healthy" ] && { echo "app healthy on \$WANT"; dc ps; exit 0; }
  sleep 4
done
echo "app is not healthy after rollback (\$st):" >&2; dc logs --tail=40 app >&2; exit 1
REMOTE

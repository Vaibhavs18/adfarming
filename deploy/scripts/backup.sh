#!/usr/bin/env bash
# Pull a backup of run outputs (/data/out) and a consistent copy of studio.db to THIS machine.
#   bash deploy/scripts/backup.sh deploy@<IP> [local_dir]      (default local_dir: ~/adfarming-backups)
# Client data (/data/private) is NOT included on purpose; it already lives on your Mac.
# The archive may contain generated run material: keep it outside git (default dir is outside the repo).
set -euo pipefail
# shellcheck source=deploy/scripts/_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

HOST="${1:-${DEPLOY_HOST:-}}"; DEST="${2:-$HOME/adfarming-backups}"
[ -n "$HOST" ] || die "usage: backup.sh deploy@<IP> [local_dir]"
check_host "$HOST"
need ssh
umask 077
mkdir -p "$DEST"
OUT="$DEST/adfarming-backup-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"

# Only the tar stream goes to stdout; everything else to stderr.
remote_bash "$HOST" > "$OUT" <<'REMOTE'
set -euo pipefail
DATA=/opt/adfarming/data
tmp="$(mktemp -d /opt/adfarming/backups/tmp.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT
if [ -f "$DATA/db/studio.db" ]; then
  python3 - "$DATA/db/studio.db" "$tmp/studio.db" >&2 <<'PY'
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
else
  echo "no studio.db yet, backing up out/ only" >&2
fi
tar czf - -C "$DATA" out -C "$tmp" .
REMOTE

[ -s "$OUT" ] || { rm -f "$OUT"; die "empty backup, removed"; }
tar tzf "$OUT" >/dev/null || die "archive is corrupt: $OUT"
info "backup written: $OUT ($(du -h "$OUT" | cut -f1))"

#!/usr/bin/env bash
# Upload ONE client-data directory (footage/, asr/, brief.md, subject.txt, ...) to the VM.
#   bash deploy/scripts/push_client_data.sh <local_dir> deploy@<IP> [--dry-run]
# Only the directory YOU name is sent, to /opt/adfarming/data/private/. Shows what will be
# uploaded and asks for a typed "yes". Refuses anything containing .env / key files.
# Never deletes server files. Client data is confidential: only push what you are allowed to.
set -euo pipefail
# shellcheck source=deploy/scripts/_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

SRC="${1:-}"; HOST="${2:-${DEPLOY_HOST:-}}"; MODE="${3:-}"
[ -n "$SRC" ] && [ -n "$HOST" ] || die "usage: push_client_data.sh <local_dir> deploy@<IP> [--dry-run]"
check_host "$HOST"
need rsync; need ssh; need du; need find
[ -d "$SRC" ] || die "not a directory: $SRC"
SRC="$(cd "$SRC" && pwd)"
ROOT="$(repo_root)"

case "$SRC" in
  "$ROOT" | "/" | "$HOME") die "refusing to upload $SRC (too broad). Name the specific client/data directory.";;
esac
case "$SRC" in
  *.env* | */.ssh* | */.aws* ) die "refusing: path contains .env / credential directory: $SRC";;
esac
[ "$SRC" != "$ROOT/private" ] || die "refusing the whole private/ directory (it holds research notes and other clients). Name a subdirectory."

# Hard refusals inside the tree: env files and key material.
bad="$(find "$SRC" \( -iname '.env' -o -iname '.env.*' -o -iname '*.pem' -o -iname '*.key' -o -iname 'id_rsa*' -o -iname 'id_ed25519*' -o -iname 'credentials*' \) -print | head -n 5)"
if [ -n "$bad" ]; then
  printf '%s\n' "$bad" >&2
  die "refusing: the directory contains env/key files (listed above). Remove them or choose a cleaner directory."
fi

EXCLUDES=(--exclude '.DS_Store' --exclude '.git/' --exclude 'research/' --exclude '__pycache__/')
RSYNC_FLAGS=(-rltz --no-owner --no-group --chmod=D755,F644 --partial --human-readable)

echo "Source      : $SRC"
echo "Destination : ${HOST}:${REMOTE_DATA}/private/   (existing server files are never deleted)"
echo "Total size  : $(du -sh "$SRC" | cut -f1)"
echo "Top level   :"
find "$SRC" -mindepth 1 -maxdepth 1 -exec basename {} \; | sort | sed 's/^/  - /'
echo
echo "Files that rsync would send (first 40):"
rsync "${RSYNC_FLAGS[@]}" "${EXCLUDES[@]}" --dry-run --itemize-changes --out-format='  %n' \
  "$SRC"/ "${HOST}:${REMOTE_DATA}/private/" -e "ssh ${SSH_OPTS[*]}" | grep -v '/$' | head -n 40 || true
echo

[ "$MODE" != "--dry-run" ] || { info "dry run only, nothing uploaded"; exit 0; }

printf 'Upload this confidential data to the public-facing VM? Type "yes" to continue: '
read -r answer
[ "$answer" = "yes" ] || die "aborted, nothing uploaded"

rsync "${RSYNC_FLAGS[@]}" "${EXCLUDES[@]}" --progress "$SRC"/ "${HOST}:${REMOTE_DATA}/private/" -e "ssh ${SSH_OPTS[*]}"
info "done. Files are owned by the deploy user (uid 1000) = the container user."

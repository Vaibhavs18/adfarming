#!/usr/bin/env bash
# Upload ONE client's data directory (clients/<id>/: footage/, asr/, brief.md, subject.txt, ...) to the VM.
#   bash deploy/scripts/push_client_data.sh <client_id | path/to/clients/<id>> deploy@<IP> [--dry-run]
# A bare <client_id> resolves to ${ADF_PRIVATE:-<repo>/private}/clients/<id>. Only that one directory is sent,
# to /opt/adfarming/data/private/clients/<id>/ (the container sees it as /data/clients/<id>/). Shows what will be
# uploaded and asks for a typed "yes". Refuses anything containing .env / key files. Never deletes server files.
# Client data is confidential: only push what you are allowed to. Create a client with scripts/add_client.py.
set -euo pipefail
# shellcheck source=deploy/scripts/_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

ARG="${1:-}"; HOST="${2:-${DEPLOY_HOST:-}}"; MODE="${3:-}"
[ -n "$ARG" ] && [ -n "$HOST" ] || die "usage: push_client_data.sh <client_id | .../clients/<id>> deploy@<IP> [--dry-run]"
check_host "$HOST"
need rsync; need ssh; need du; need find
ROOT="$(repo_root)"
PRIVATE_ROOT="${ADF_PRIVATE:-$ROOT/private}"
if [[ "$ARG" =~ ^[a-z0-9][a-z0-9_-]{0,39}$ ]] && [ ! -d "$ARG" ]; then
  SRC="$PRIVATE_ROOT/clients/$ARG"
else
  SRC="$ARG"
fi
[ -d "$SRC" ] || die "not a directory: $SRC (create the client first: python scripts/add_client.py --id <id> --name '<Name>' --from <dir>)"
SRC="$(cd "$SRC" && pwd)"
CLIENT_ID="$(basename "$SRC")"
[ "$(basename "$(dirname "$SRC")")" = "clients" ] || die "refusing: $SRC is not a clients/<id> directory. Only one client's directory is ever uploaded."
[[ "$CLIENT_ID" =~ ^[a-z0-9][a-z0-9_-]{0,39}$ ]] || die "bad client id: $CLIENT_ID"
[ -f "$SRC/client.json" ] || die "$SRC has no client.json (not a client created by add_client.py)"
case "$SRC" in
  *.env* | */.ssh* | */.aws* ) die "refusing: path contains .env / credential directory: $SRC";;
esac
DEST="${REMOTE_DATA}/private/clients/${CLIENT_ID}"

# Hard refusals inside the tree: env files and key material.
bad="$(find "$SRC" \( -iname '.env' -o -iname '.env.*' -o -iname '*.pem' -o -iname '*.key' -o -iname 'id_rsa*' -o -iname 'id_ed25519*' -o -iname 'credentials*' \) -print | head -n 5)"
if [ -n "$bad" ]; then
  printf '%s\n' "$bad" >&2
  die "refusing: the directory contains env/key files (listed above). Remove them or choose a cleaner directory."
fi

EXCLUDES=(--exclude '.DS_Store' --exclude '.git/' --exclude 'research/' --exclude '__pycache__/')
RSYNC_FLAGS=(-rltz --no-owner --no-group --chmod=D755,F644 --partial --human-readable)

echo "Source      : $SRC"
echo "Client      : $CLIENT_ID"
echo "Destination : ${HOST}:${DEST}/   (existing server files are never deleted)"
echo "Total size  : $(du -sh "$SRC" | cut -f1)"
echo "Top level   :"
find "$SRC" -mindepth 1 -maxdepth 1 -exec basename {} \; | sort | sed 's/^/  - /'
echo
echo "Files that rsync would send (first 40):"
listing="$(rsync "${RSYNC_FLAGS[@]}" "${EXCLUDES[@]}" --dry-run --itemize-changes --out-format='  %n' \
  "$SRC"/ "${HOST}:${DEST}/" -e "ssh ${SSH_OPTS[*]}" 2>/dev/null | grep -v '/$' | head -n 40 || true)"
if [ -n "$listing" ]; then printf '%s\n' "$listing"
else (cd "$SRC" && find . -type f ! -name .DS_Store | sed 's|^\./|  |' | head -n 40); echo "  (new client on the server, or nothing changed: local file list shown)"; fi
echo

[ "$MODE" != "--dry-run" ] || { info "dry run only, nothing uploaded"; exit 0; }

printf 'Upload this confidential data to the public-facing VM? Type "yes" to continue: '
read -r answer
[ "$answer" = "yes" ] || die "aborted, nothing uploaded"

ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p '${DEST}'"
rsync "${RSYNC_FLAGS[@]}" "${EXCLUDES[@]}" --progress "$SRC"/ "${HOST}:${DEST}/" -e "ssh ${SSH_OPTS[*]}"
info "done. Restart the app so it discovers the client and seeds its memory (docker compose restart / deploy.sh). Files are owned by the deploy user (uid 1000) = the container user."

#!/usr/bin/env bash
# Shared helpers, sourced by the other scripts. Not meant to be run directly.
# Works on macOS bash 3.2 and Ubuntu bash 5. Never prints secrets.

REMOTE_ROOT="/opt/adfarming"
REMOTE_ENV="${REMOTE_ROOT}/.env"
REMOTE_SRC="${REMOTE_ROOT}/src"
REMOTE_DATA="${REMOTE_ROOT}/data"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)

info() { printf '[%s] %s\n' "$(basename "$0")" "$*" >&2; }
warn() { printf '[%s] WARNING: %s\n' "$(basename "$0")" "$*" >&2; }
die() { printf '[%s] ERROR: %s\n' "$(basename "$0")" "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }

# repo root = two levels above deploy/scripts
repo_root() { (cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd); }

# Accept user@host only (no spaces, no shell metacharacters).
check_host() {
  case "$1" in
    *[!A-Za-z0-9@._:-]* | "" ) die "host must look like deploy@203.0.113.10 (got an unsafe value)";;
  esac
  case "$1" in
    *@*) : ;;
    *) die "host must include the user, e.g. deploy@203.0.113.10";;
  esac
}

# Run a bash script (read from stdin) on the VM. Arguments are passed as $1.. on the VM.
remote_bash() {
  local host="$1"; shift
  ssh "${SSH_OPTS[@]}" "$host" bash -s -- "$@"
}

# Remote shell prelude defining the compose wrapper `dc` (project root = ${REMOTE_SRC}).
# shellcheck disable=SC2016  # intentional: this text is evaluated on the VM, not here
REMOTE_PRELUDE='
set -euo pipefail
ROOT=/opt/adfarming
dc() { (cd "$ROOT/src" && docker compose --env-file "$ROOT/.env" -f deploy/docker-compose.yml "$@"); }
env_val() { grep -E "^$1=" "$ROOT/.env" | tail -n1 | cut -d= -f2- | sed -e "s/^[\"'"'"']//" -e "s/[\"'"'"']$//"; }
'

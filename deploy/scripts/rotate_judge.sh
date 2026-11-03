#!/usr/bin/env bash
# Rotate the judge credential. Generates a NEW random password, prints it ONCE on this terminal,
# writes the Caddy bcrypt hash plus the app's STUDIO_PASSWORD (same value) and a fresh STUDIO_SECRET
# (invalidates old session cookies) into /opt/adfarming/.env on the VM, then recreates app + caddy.
#   bash deploy/scripts/rotate_judge.sh deploy@<IP> [-y]
# The password is sent to the VM over ssh stdin (never on a command line) and is not stored locally.
# Heads up: restarting the app interrupts any run in progress.
set -euo pipefail
# shellcheck source=deploy/scripts/_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

HOST="${1:-${DEPLOY_HOST:-}}"; YES="${2:-}"
[ -n "$HOST" ] || die "usage: rotate_judge.sh deploy@<IP> [-y]"
check_host "$HOST"
need ssh; need openssl

if [ "$YES" != "-y" ]; then
  printf 'Rotate the judge password now? The old one stops working and runs in progress are interrupted. Type "yes": '
  read -r a; [ "$a" = "yes" ] || die "aborted"
fi

# 28 chars, alphanumeric (safe in .env, URLs and basic auth). base64 of 48 random bytes, filtered.
PW="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-28)"
[ "${#PW}" -eq 28 ] || die "could not generate a password"
SECRET="$(openssl rand -hex 32)"

# The ssh stdin stream is: two assignment lines (alphanumeric values), then the script. Nothing
# secret appears in any argv, here or on the VM.
{
printf "PW='%s'\nSECRET='%s'\n" "$PW" "$SECRET"
cat <<REMOTE
$REMOTE_PRELUDE
HASH="\$(printf '%s\n' "\$PW" | docker run --rm -i caddy:2.10-alpine caddy hash-password | tail -n1)"
case "\$HASH" in
  '\$2'*) : ;;
  *) echo "could not compute a bcrypt hash (got an unexpected value); .env unchanged" >&2; exit 1;;
esac
cp -p "\$ROOT/.env" "\$ROOT/.env.bak"        # one local backup on the VM, mode 600
umask 077
tmp="\$(mktemp "\$ROOT/.env.XXXXXX")"
grep -Ev '^(CADDY_BASIC_AUTH_HASH|STUDIO_PASSWORD|STUDIO_SECRET)=' "\$ROOT/.env" > "\$tmp" || true
{
  printf "CADDY_BASIC_AUTH_HASH='%s'\n" "\$HASH"
  printf 'STUDIO_PASSWORD=%s\n' "\$PW"
  printf 'STUDIO_SECRET=%s\n' "\$SECRET"
} >> "\$tmp"
chmod 600 "\$tmp"
mv "\$tmp" "\$ROOT/.env"
TAG="\$(cat "\$ROOT/current_tag" 2>/dev/null || echo latest)"
export TAG
dc up -d --force-recreate --no-build app caddy
rm -f "\$ROOT/.env.bak"                      # do not leave the old password lying around
echo "app and caddy recreated"
REMOTE
} | remote_bash "$HOST"

SITE_USER_HINT="judge"
echo
echo "================ NEW JUDGE CREDENTIAL (shown once, copy it now) ================"
echo "user:     ${SITE_USER_HINT}  (or whatever CADDY_BASIC_AUTH_USER is on the VM)"
echo "password: ${PW}"
echo "(the same password also logs into the Studio app itself)"
echo "================================================================================"
echo "Verify: bash deploy/scripts/smoke.sh https://<SITE_ADDRESS>   (it prompts for the password)"

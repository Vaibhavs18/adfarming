#!/usr/bin/env bash
# Post-deploy go/no-go checks against the public URL.
#   bash deploy/scripts/smoke.sh https://203-0-113-10.sslip.io [judge_user]
# The judge password is read from $JUDGE_PASSWORD or typed at a hidden prompt; it is never echoed
# or put on a command line (curl gets it through a config on stdin). The app password defaults to
# the same value (rotate_judge.sh keeps them equal); override with $STUDIO_APP_PASSWORD.
# Wrong-password login attempts are limited to 1 so the app's 5/min limiter is not tripped.
set -euo pipefail
# shellcheck source=deploy/scripts/_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

URL="${1:-}"; USER_NAME="${2:-judge}"
[ -n "$URL" ] || die "usage: smoke.sh https://<SITE_ADDRESS> [judge_user]"
URL="${URL%/}"
case "$URL" in https://*) : ;; *) die "URL must start with https://";; esac
need curl
HOSTPORT="${URL#https://}"; HOSTNAME_ONLY="${HOSTPORT%%[:/]*}"

PW="${JUDGE_PASSWORD:-}"
if [ -z "$PW" ]; then
  printf 'Judge password (hidden): ' >&2
  IFS= read -rs PW; echo >&2
fi
[ -n "$PW" ] || die "empty password"
APP_PW="${STUDIO_APP_PASSWORD:-$PW}"

JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT
fails=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# code <expected-regex> <label> <path> [curl args...]  (auth added only when AUTH=1)
code() {
  local want="$1" label="$2" path="$3"; shift 3
  local got
  if [ "${AUTH:-0}" = "1" ]; then
    got="$(printf 'user = "%s:%s"\n' "$USER_NAME" "$PW" | curl -sS -K - -o /dev/null -w '%{http_code}' --max-time 20 "$@" "$URL$path" || true)"
  else
    got="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$@" "$URL$path" || true)"
  fi
  if printf '%s' "$got" | grep -Eq "^(${want})\$"; then pass "$label -> $got"; else fail "$label -> $got (wanted ${want})"; fi
}

echo "Smoke test: $URL"
echo "1. TLS and health"
body="$(curl -sS --max-time 20 "$URL/healthz" || true)"
if printf '%s' "$body" | grep -q '"ok"'; then pass "GET /healthz open, valid TLS, body ok"; else fail "GET /healthz (body: ${body:-empty})"; fi
vr="$(curl -sS -o /dev/null -w '%{ssl_verify_result}' --max-time 20 "$URL/healthz" || true)"
[ "$vr" = "0" ] && pass "TLS certificate verifies" || fail "TLS verify result $vr"

echo "2. Gate closed without credentials"
AUTH=0 code 401 "GET / without auth" /
AUTH=0 code 401 "GET /api/clients without auth" /api/clients
AUTH=0 code 401 "POST /api/login without basic auth" /api/login -X POST -H 'Content-Type: application/json' -d '{"password":"x"}'

echo "3. Gate open with credentials"
AUTH=1 code 200 "GET / with auth" /

echo "4. Hidden paths stay hidden (even with credentials)"
AUTH=1 code '401|404' "GET /.env" /.env
AUTH=1 code '401|404' "GET /private/" /private/
AUTH=1 code '401|404' "GET /out/" /out/

echo "5. App login behind the gate"
AUTH=1 code 401 "login with a wrong password" /api/login -X POST -H 'Content-Type: application/json' -d '{"password":"definitely-wrong"}'
# right password: need the JSON body, so build it without a command-line secret
login_code="$( { printf 'user = "%s:%s"\n' "$USER_NAME" "$PW"; printf 'data = "{\\"password\\":\\"%s\\"}"\n' "$APP_PW"; } \
  | curl -sS -K - -c "$JAR" -o /dev/null -w '%{http_code}' --max-time 20 -X POST -H 'Content-Type: application/json' "$URL/api/login" || true)"
if [ "$login_code" = "200" ]; then
  pass "login with the app password -> 200"
  me_code="$(printf 'user = "%s:%s"\n' "$USER_NAME" "$PW" | curl -sS -K - -b "$JAR" -o /dev/null -w '%{http_code}' --max-time 20 "$URL/api/me" || true)"
  [ "$me_code" = "200" ] && pass "GET /api/me with session cookie -> 200" || fail "GET /api/me -> $me_code"
else
  fail "login with the app password -> $login_code (is STUDIO_APP_PASSWORD needed? rotate_judge.sh keeps them equal)"
fi

echo "6. App port is not reachable from outside"
if curl -sS -m 5 -o /dev/null "http://${HOSTNAME_ONLY}:8000/" 2>/dev/null; then
  fail "http://${HOSTNAME_ONLY}:8000/ ANSWERED. The app port must not be published."
else
  pass "http://${HOSTNAME_ONLY}:8000/ unreachable (timeout or refused)"
fi

echo
if [ "$fails" -eq 0 ]; then echo "SMOKE: GO (all checks passed)"; else echo "SMOKE: NO-GO ($fails failed)"; exit 1; fi

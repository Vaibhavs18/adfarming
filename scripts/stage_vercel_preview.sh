#!/usr/bin/env bash
# Assemble the static Studio demo for Vercel in /tmp. Does NOT deploy (the lead does that).
#
#   scripts/stage_vercel_preview.sh [replay-name]      default replay-name: session-clean
#
# Output: /tmp/adf_vercel/adfarming-studio-preview/
#   web/ (without README.md) + vercel.json
#   replay-<TOKEN>/  (only if private/replay/<name>/ exists; TOKEN is in private/replay/token.txt, gitignored)
# The private link is printed to the terminal only. Never paste it into a tracked file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="${1:-session-clean}"
STAGE_ROOT="${STAGE_ROOT:-/tmp/adf_vercel}"
STAGE="$STAGE_ROOT/adfarming-studio-preview"
HOST="${PREVIEW_HOST:-https://adfarming-studio-preview.vercel.app}"

mkdir -p "$STAGE_ROOT"
# Keep Vercel's project link (.vercel/) if a previous stage had one; rebuild everything else.
if [ -d "$STAGE" ]; then
  find "$STAGE" -mindepth 1 -maxdepth 1 ! -name '.vercel' -exec rm -rf {} +
else
  mkdir -p "$STAGE"
fi

# 1. the static app (no build step), minus the dev README
rsync -a --exclude 'README.md' --exclude '.DS_Store' "$ROOT/web/" "$STAGE/"

# 2. Vercel config: land on the fictional demo when ?mock is missing, keep the page out of search engines
cat > "$STAGE/vercel.json" <<'JSON'
{
  "redirects": [
    {"source": "/", "missing": [{"type": "query", "key": "mock"}], "destination": "/?mock=1", "permanent": false}
  ],
  "headers": [
    {"source": "/(.*)", "headers": [
      {"key": "X-Robots-Tag", "value": "noindex, nofollow"},
      {"key": "Referrer-Policy", "value": "no-referrer"}
    ]}
  ]
}
JSON
printf '.vercel\n' > "$STAGE/.gitignore"

# 3. the private replay, behind an unguessable folder name
LINK=""
SRC="$ROOT/private/replay/$NAME"
if [ -f "$SRC/fixtures.json" ]; then
  TOKEN_FILE="$ROOT/private/replay/token.txt"
  if [ ! -s "$TOKEN_FILE" ]; then
    umask 077
    openssl rand -hex 8 > "$TOKEN_FILE"
  fi
  TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
  mkdir -p "$STAGE/replay-$TOKEN"
  rsync -a --exclude '.DS_Store' "$SRC/" "$STAGE/replay-$TOKEN/"
  LINK="$HOST/?mock=1&replay=$TOKEN"
else
  echo "note: $SRC/fixtures.json not found; staged the fictional demo only (build it with scripts/build_replay.py)." >&2
fi

SIZE="$(du -sh "$STAGE" | cut -f1)"
echo "Staged: $STAGE ($SIZE)"
echo "Fictional demo: $HOST/"
if [ -n "$LINK" ]; then
  echo "PRIVATE replay link (terminal only, do not commit or post publicly):"
  echo "  $LINK"
fi
echo "Local check:  python3 -m http.server 8792 --directory $STAGE"
echo "Not deployed. The lead runs: cd $STAGE && vercel deploy --prod"

#!/usr/bin/env bash
# deploy.sh — single-command deploy from your laptop.
#
# SSHes to the savvy host, pulls latest from main, and verifies the
# savvy-upload.service is still active.
#
# Success contract: prints exactly ONE final line. Either:
#   [deploy] DEPLOY OK  host=<sha-prev>→<sha-new>  service=<state>
# OR:
#   [deploy] DEPLOY FAILED step="<step>" rc=<code>
# OR (silent abort, e.g. SIGTERM, network drop):
#   [deploy] DEPLOY DID NOT COMPLETE — exited at step "<step>" rc=<code>
#
# Don't trust a deploy report that doesn't quote one of those three lines
# back at you verbatim. The trap below enforces the third path so silent
# exits surface instead of looking like success.
#
# Usage:
#   ./deploy.sh                       # uses $SAVVY_DEPLOY_HOST from your env
#   ./deploy.sh <ssh-host>            # explicit ssh host (alias or user@host)
#   REPO_PATH=somedir ./deploy.sh ... # override remote path (default: $HOME/savvy)
#
# Env:
#   SAVVY_DEPLOY_HOST   ssh target (host alias or user@host). Required if no arg.
#   REPO_PATH           remote checkout path relative to $HOME (default: savvy)
#
# Tip: put `export SAVVY_DEPLOY_HOST=...` in your shell profile so the
# common case is just `./deploy.sh`.
set -euo pipefail

HOST="${1:-${SAVVY_DEPLOY_HOST:-}}"
REPO_PATH="${REPO_PATH:-savvy}"

if [ -z "$HOST" ]; then
  cat >&2 <<'MSG'
deploy.sh: SAVVY_DEPLOY_HOST is not set.
  - pass as arg:   ./deploy.sh <ssh-host>
  - or export:     export SAVVY_DEPLOY_HOST=<host> && ./deploy.sh

If you're running from an automation context (an agent, cron, CI),
your interactive shell profile probably wasn't sourced — prefer the
arg form, or pass the var inline:
                   SAVVY_DEPLOY_HOST=<host> ./deploy.sh
MSG
  exit 2
fi

CURRENT_STEP="(init)"
DEPLOY_COMPLETE=0
FINAL_PRINTED=0

step() {
  CURRENT_STEP="$1"
  echo
  echo "→ $1"
}

bail_final() {
  rc=$1
  reason="${2:-}"
  echo >&2
  echo "[deploy] DEPLOY FAILED step=\"$CURRENT_STEP\" rc=$rc ${reason}" >&2
  FINAL_PRINTED=1
  exit "$rc"
}

trap '
  rc=$?
  if [ "$DEPLOY_COMPLETE" = "1" ] || [ "$FINAL_PRINTED" = "1" ]; then exit $rc; fi
  echo >&2
  echo "[deploy] DEPLOY DID NOT COMPLETE — exited at step \"$CURRENT_STEP\" rc=$rc" >&2
' EXIT

# ---------------------------------------------------------------------- #

step "pull latest on $HOST:~/$REPO_PATH"
pull_result=$(ssh "$HOST" "cd ~/$REPO_PATH && {
  prev=\$(git rev-parse --short HEAD)
  git fetch --quiet origin main
  git merge --ff-only --quiet origin/main
  new=\$(git rev-parse --short HEAD)
  echo \"\$prev \$new\"
}") || bail_final $? "(could not pull)"
prev_sha=$(printf '%s\n' "$pull_result" | awk '{print $1}')
new_sha=$(printf '%s\n' "$pull_result" | awk '{print $2}')
if [ "$prev_sha" = "$new_sha" ]; then
  echo "  no new commits (still at $new_sha)"
  sha_summary="$new_sha"
else
  echo "  $prev_sha → $new_sha"
  sha_summary="${prev_sha}→${new_sha}"
fi

step "verify savvy-upload.service is active"
service_state=$(ssh "$HOST" "systemctl is-active savvy-upload.service 2>&1") \
  || bail_final $? "(service check failed)"
echo "  service state: $service_state"

DEPLOY_COMPLETE=1
echo
echo "[deploy] DEPLOY OK  host=$sha_summary  service=$service_state"

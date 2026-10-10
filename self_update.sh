#!/usr/bin/env bash
# self_update.sh - keep the host's checkout on origin/main.
#
# deploy.sh pulls main when someone runs it from a laptop; nothing pulls when
# nobody does, so merged fixes can sit undeployed for months. Run this from
# savvy-self-update.timer (every 15 min) instead. Each run:
#   1. fetches origin main;
#   2. refuses, loudly (exit 1, optional Healthchecks /fail), when the checkout
#      is not on main, has edits to tracked files, or main has commits
#      origin/main lacks. Never stashes, resets or discards anything;
#   3. fast-forwards main to origin/main;
#   4. restarts savvy-upload.service when savvy_watch.sh changed after the
#      service started, and only while the watcher is idle. bash holds the
#      running savvy_watch.sh in memory; savvy_cron.sh and savvy_upload.py
#      start fresh on every batch, so only the watcher script needs it. A
#      restart during the 60 s debounce or an upload would drop the pending
#      inotify event, so a busy watcher is restarted on a later run (the check
#      is stateless: savvy_watch.sh mtime vs the service's start time).
#
# Python packages in venv/ are installed by hand (see README); this script
# does not touch them.
#
# Env (all optional):
#   SELF_UPDATE_HEALTHCHECK_URL  Healthchecks URL; pinged /start, success, /fail
#   SELF_UPDATE_REPO      checkout to update (default: this script's dir)
#   SELF_UPDATE_SERVICE   (default savvy-upload.service)
#   SELF_UPDATE_SYSTEMCTL (default "sudo -n systemctl")
#   SELF_UPDATE_WATCHER_PS  command printing "comm<TAB>cmdline" per process in
#                           the service (default: read its cgroup); for tests
#
#   ./self_update.sh [--dry-run]     (--dry-run: fetch and report only)
set -uo pipefail

REPO="${SELF_UPDATE_REPO:-$(cd "$(dirname "$0")" && pwd)}"
SERVICE="${SELF_UPDATE_SERVICE:-savvy-upload.service}"
read -r -a SYSTEMCTL <<< "${SELF_UPDATE_SYSTEMCTL:-sudo -n systemctl}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

log() { echo "[self-update] $*"; }
hc_ping() {
    [[ -n "${SELF_UPDATE_HEALTHCHECK_URL:-}" && "$DRY_RUN" -eq 0 ]] || return 0
    curl -fsS --max-time 15 "${SELF_UPDATE_HEALTHCHECK_URL}${1:-}" -o /dev/null || true
}
fail() { log "FAILED - $1" >&2; hc_ping /fail; exit 1; }
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

watcher_ps() {
    if [[ -n "${SELF_UPDATE_WATCHER_PS:-}" ]]; then
        bash -c "$SELF_UPDATE_WATCHER_PS"
        return
    fi
    local cg pid
    cg="$("${SYSTEMCTL[@]}" show -p ControlGroup --value "$SERVICE" 2>/dev/null)" || return 1
    [[ -n "$cg" && -r "/sys/fs/cgroup$cg/cgroup.procs" ]] || return 1
    while read -r pid; do
        [[ -r "/proc/$pid/comm" ]] || continue
        printf '%s\t%s\n' "$(cat "/proc/$pid/comm")" "$(tr '\0' ' ' < "/proc/$pid/cmdline")"
    done < "/sys/fs/cgroup$cg/cgroup.procs"
}

# Idle = only savvy_watch.sh's bash processes and inotifywait. A sleep (the
# debounce), savvy_cron.sh, python or a browser means it is mid-batch.
watcher_idle() {
    local out comm args n=0
    out="$(watcher_ps)" || return 1
    while IFS=$'\t' read -r comm args; do
        [[ -n "$comm" ]] || continue
        n=$((n + 1))
        case "$comm" in
            inotifywait) ;;
            bash) [[ "$args" == *savvy_watch.sh* ]] || return 1 ;;
            *) return 1 ;;
        esac
    done <<< "$out"
    [[ "$n" -gt 0 ]]
}

maybe_restart_watcher() {
    local state started changed
    state="$("${SYSTEMCTL[@]}" is-active "$SERVICE" 2>/dev/null)" || true
    if [[ "$state" != active ]]; then
        log "$SERVICE is ${state:-unknown}; not touching it"
        return 0
    fi
    started="$("${SYSTEMCTL[@]}" show --timestamp=unix -p ExecMainStartTimestamp --value "$SERVICE" 2>/dev/null)"
    started="${started#@}"
    changed="$(mtime "$REPO/savvy_watch.sh")"
    if [[ ! "$started" =~ ^[0-9]+$ || -z "$changed" ]]; then
        log "WARNING: could not compare savvy_watch.sh with $SERVICE's start time; not restarting"
        return 0
    fi
    [[ "$changed" -gt "$started" ]] || return 0
    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "dry run: savvy_watch.sh is newer than the running $SERVICE; would restart it when idle"
    elif ! watcher_idle; then
        log "savvy_watch.sh changed but the watcher is mid-batch; restarting it on a later run"
    elif "${SYSTEMCTL[@]}" restart "$SERVICE"; then
        log "restarted $SERVICE (savvy_watch.sh changed)"
    else
        log "WARNING: restarting $SERVICE failed"
    fi
}

hc_ping /start
cd "$REPO" || fail "no checkout at $REPO"
git fetch --quiet origin main || fail "git fetch origin main failed"

branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
[[ "$branch" == main ]] || fail "checkout is on '${branch:-a detached HEAD}', not main; left as it is"
dirty="$(git status --porcelain --untracked-files=no)"
[[ -z "$dirty" ]] || fail "tracked files have local edits; left as they are:
$dirty"
ahead="$(git rev-list --count origin/main..HEAD)"
[[ "$ahead" -eq 0 ]] || fail "main has $ahead commit(s) origin/main lacks; left as it is"

behind="$(git rev-list --count HEAD..origin/main)"
if [[ "$behind" -eq 0 ]]; then
    log "up to date at $(git rev-parse --short HEAD)"
elif [[ "$DRY_RUN" -eq 1 ]]; then
    log "dry run: $behind commit(s) behind origin/main:"
    git log --oneline HEAD..origin/main
else
    pre="$(git rev-parse --short HEAD)"
    git merge --ff-only --quiet origin/main || fail "fast-forward to origin/main failed"
    log "fast-forwarded $pre -> $(git rev-parse --short HEAD) ($behind commit(s)):"
    git log --oneline "$pre..HEAD"
fi

maybe_restart_watcher
hc_ping
exit 0

#!/usr/bin/env bash
# Regression test for the savvy_watch.sh consumer-loop wedge.
#
# Background: savvy_watch.sh runs
#     inotifywait -m ... | while read ...; do "$SCRIPT_DIR/savvy_cron.sh"; done
# under `set -euo pipefail`. If the upload call exits non-zero and is NOT
# guarded, `set -e` terminates the `while read` subshell while inotifywait
# keeps running — the watcher then looks "active (running)" but silently
# ignores every later file until it is manually restarted.
#
# This test reproduces that consumer structure with a stub "upload" that
# fails on one item, and asserts:
#   1. a GUARDED loop (the fix) processes every item, including those after
#      the failing one;
#   2. an UNGUARDED loop (the old bug) stops at the failing item — proving
#      this test actually exercises the wedge it is guarding against;
#   3. the real savvy_watch.sh ships the guard (and has no bare invocation).
#
# Pure bash: no inotifywait (Linux-only) and no 60s debounce, because the bug
# lives in the shell loop's error handling, not in inotify itself.
#
# Run:  bash tests/test_watch_loop_survives_failure.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCH_SCRIPT="$REPO_ROOT/savvy_watch.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0
ok()  { echo "OK: $1"; }
bad() { echo "FAIL: $1"; fail=1; }

# Stand-in for savvy_cron.sh: logs the item it was handed, exits 1 on "FAIL".
# A real external script (not a shell function) so its non-zero exit is seen
# by the loop exactly as savvy_cron.sh's would be under `set -e`.
STUB="$TMP/stub_upload.sh"
cat > "$STUB" <<'EOF'
#!/usr/bin/env bash
echo "$1" >> "$PROCESSED_LOG"
[[ "$1" == "FAIL" ]] && exit 1
exit 0
EOF
chmod +x "$STUB"

# Three items; the middle one drives the stub to a non-zero exit.
ITEMS='a
FAIL
c'

# --- Scenario 1: GUARDED loop (mirrors the fix in savvy_watch.sh) ---
guarded_log="$TMP/guarded.log"
: > "$guarded_log"
PROCESSED_LOG="$guarded_log" STUB="$STUB" ITEMS="$ITEMS" bash -c '
set -euo pipefail
printf "%s\n" "$ITEMS" | while read -r item; do
    rc=0
    "$STUB" "$item" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        echo "  (stub failed on $item, rc=$rc — loop continues)" >&2
    fi
done
' 2>/dev/null
guarded_count=$(wc -l < "$guarded_log" | tr -d ' ')

if [[ "$guarded_count" -eq 3 ]]; then
    ok "guarded loop keeps processing after a failed upload ($guarded_count/3)"
else
    bad "guarded loop should process all 3 items, processed $guarded_count"
fi

# --- Scenario 2: UNGUARDED loop (the old bug) wedges at the failure ---
unguarded_log="$TMP/unguarded.log"
: > "$unguarded_log"
PROCESSED_LOG="$unguarded_log" STUB="$STUB" ITEMS="$ITEMS" bash -c '
set -euo pipefail
printf "%s\n" "$ITEMS" | while read -r item; do
    "$STUB" "$item"
done
' 2>/dev/null
unguarded_count=$(wc -l < "$unguarded_log" | tr -d ' ')

if [[ "$unguarded_count" -lt 3 ]]; then
    ok "unguarded loop wedges at the failing item ($unguarded_count/3) — confirms the bug"
else
    bad "unguarded loop processed all $unguarded_count items; test no longer exercises the wedge"
fi

# --- Scenario 3: the real savvy_watch.sh ships the guard ---
# These patterns match the literal text "$SCRIPT_DIR/savvy_cron.sh" in the
# script, so the '$' is intentionally not expanded (SC2016 suppressed).
# shellcheck disable=SC2016
if grep -Eq '"\$SCRIPT_DIR/savvy_cron\.sh"[[:space:]]*\|\|' "$WATCH_SCRIPT"; then
    ok "savvy_watch.sh guards the upload invocation"
else
    bad "savvy_watch.sh upload invocation is not guarded with '|| ...'"
fi

# shellcheck disable=SC2016
if grep -Eq '^[[:space:]]*"\$SCRIPT_DIR/savvy_cron\.sh"[[:space:]]*$' "$WATCH_SCRIPT"; then
    bad "savvy_watch.sh still has a bare (unguarded) savvy_cron.sh invocation"
else
    ok "savvy_watch.sh has no bare unguarded invocation"
fi

echo
if [[ "$fail" -eq 0 ]]; then
    echo "All tests passed."
    exit 0
fi
echo "Some tests FAILED."
exit 1

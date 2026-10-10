#!/usr/bin/env bash
# tests/test_self_update.sh - self_update.sh against a temp checkout.
#
# A bare repo stands in for GitHub and a clone for the host's checkout.
# systemctl and curl are shims that log their calls; SELF_UPDATE_WATCHER_PS
# stands in for the service's cgroup. Nothing reaches the network or systemd.
#
#   bash tests/test_self_update.sh
# shellcheck disable=SC2016,SC2034  # check() evals its single-quoted conditions later
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="${SELF_UPDATE_SCRIPT:-$HERE/self_update.sh}"
command -v git >/dev/null || { echo "SKIP: git not installed"; exit 0; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/self-update-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
ok()  { echo "  ok   $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL $1"; FAIL=$((FAIL + 1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"; }

mkdir -p "$WORK/bin"
cat > "$WORK/bin/systemctl" <<'SHIM'
#!/usr/bin/env bash
echo "$*" >> "$SHIM_LOG"
case "$1" in
    is-active) echo "${SHIM_STATE:-active}" ;;
    show) echo "@${SHIM_STARTED:-0}" ;;
esac
SHIM
cat > "$WORK/bin/curl" <<'SHIM'
#!/usr/bin/env bash
for a in "$@"; do [[ "$a" == https://* ]] && echo "$a" >> "$CURL_LOG"; done
exit 0
SHIM
chmod +x "$WORK/bin/systemctl" "$WORK/bin/curl"
export PATH="$WORK/bin:$PATH"
export SHIM_LOG="$WORK/systemctl.log" CURL_LOG="$WORK/curl.log"
export SELF_UPDATE_SYSTEMCTL="$WORK/bin/systemctl"
export SELF_UPDATE_HEALTHCHECK_URL="https://hc.example/abc"
printf 'bash\tbash /srv/savvy/savvy_watch.sh\ninotifywait\tinotifywait -m /csvs\nbash\tbash /srv/savvy/savvy_watch.sh\n' > "$WORK/ps-idle"
{ cat "$WORK/ps-idle"; printf 'python3\tpython3 savvy_upload.py\n'; } > "$WORK/ps-busy"
export SELF_UPDATE_WATCHER_PS="cat $WORK/ps-idle"

git init -q --bare -b main "$WORK/origin.git"
git clone -q "$WORK/origin.git" "$WORK/dev" 2>/dev/null
G() { git -C "$1" -c user.email=t@t -c user.name=t "${@:2}"; }
echo 'echo watch v1' > "$WORK/dev/savvy_watch.sh"
echo 'print(1)' > "$WORK/dev/savvy_upload.py"
G "$WORK/dev" add -A && G "$WORK/dev" commit -qm init && G "$WORK/dev" push -q origin main
git clone -q "$WORK/origin.git" "$WORK/host"
export SELF_UPDATE_REPO="$WORK/host"

push_change() {
    echo "$2" > "$WORK/dev/$1"
    G "$WORK/dev" add -A && G "$WORK/dev" commit -qm "$3" && G "$WORK/dev" push -q origin main
}
run() { : > "$SHIM_LOG"; : > "$CURL_LOG"; bash "$SCRIPT" "$@" > "$WORK/out" 2>&1; echo $?; }
head_is_origin() { [[ "$(git -C "$WORK/host" rev-parse HEAD)" == "$(git -C "$WORK/origin.git" rev-parse main)" ]]; }
restarted() { grep -q '^restart savvy-upload.service' "$SHIM_LOG"; }
pinged_fail() { grep -q '/fail$' "$CURL_LOG"; }
pinged_ok() { grep -qx 'https://hc.example/abc' "$CURL_LOG"; }
started_now() { export SHIM_STARTED=$(( $(mtime "$WORK/host/savvy_watch.sh") + 100 )); }

echo "behind, watcher script unchanged:"
started_now
push_change savvy_upload.py 'print(2)' 'upload v2'
rc=$(run)
check "exits 0, fast-forwarded" '[[ $rc -eq 0 ]] && head_is_origin'
check "no restart" '! restarted'
check "pings success" 'pinged_ok && ! pinged_fail'

echo "watcher script changed, idle:"
export SHIM_STARTED=1
push_change savvy_watch.sh 'echo watch v2' 'watch v2'
rc=$(run)
check "fast-forwarded and restarted" '[[ $rc -eq 0 ]] && head_is_origin && restarted'

echo "watcher script changed, mid-upload, then idle:"
export SHIM_STARTED=1
push_change savvy_watch.sh 'echo watch v3' 'watch v3'
rc=$(SELF_UPDATE_WATCHER_PS="cat $WORK/ps-busy" run)
check "busy: fast-forwarded, no restart" '[[ $rc -eq 0 ]] && head_is_origin && ! restarted'
rc=$(run)
check "idle next run: restarts" 'restarted'

echo "service inactive:"
push_change savvy_watch.sh 'echo watch v4' 'watch v4'
rc=$(SHIM_STATE=inactive run)
check "never starts or restarts it" '[[ $rc -eq 0 ]] && ! grep -qE "^(restart|start) " "$SHIM_LOG"'

echo "untracked files do not block:"
started_now
touch "$WORK/host/debug_x.png"
push_change savvy_upload.py 'print(3)' 'upload v3'
rc=$(run)
check "exits 0 and fast-forwards" '[[ $rc -eq 0 ]] && head_is_origin'

echo "edits to a tracked file:"
echo '# local' >> "$WORK/host/savvy_upload.py"
push_change savvy_upload.py 'print(4)' 'upload v4'
before="$(git -C "$WORK/host" rev-parse HEAD)"
rc=$(run)
check "exits 1, pings /fail, untouched" '[[ $rc -eq 1 ]] && pinged_fail && [[ "$(git -C "$WORK/host" rev-parse HEAD)" == "$before" ]] && grep -q "# local" "$WORK/host/savvy_upload.py"'
git -C "$WORK/host" checkout -q -- savvy_upload.py

echo "not on main:"
git -C "$WORK/host" checkout -q -b side
rc=$(run)
check "exits 1, left on the branch" '[[ $rc -eq 1 ]] && pinged_fail && [[ "$(git -C "$WORK/host" symbolic-ref --short HEAD)" == side ]]'
git -C "$WORK/host" checkout -q main

echo "dry run:"
before="$(git -C "$WORK/host" rev-parse HEAD)"
rc=$(run --dry-run)
check "reports, changes nothing, no pings" '[[ $rc -eq 0 ]] && grep -q "upload v4" "$WORK/out" && [[ "$(git -C "$WORK/host" rev-parse HEAD)" == "$before" ]] && [[ ! -s "$CURL_LOG" ]]'
rc=$(run)

echo "diverged main:"
G "$WORK/host" commit -q --allow-empty -m 'local commit'
before="$(git -C "$WORK/host" rev-parse HEAD)"
rc=$(run)
check "exits 1, pings /fail, untouched" '[[ $rc -eq 1 ]] && pinged_fail && [[ "$(git -C "$WORK/host" rev-parse HEAD)" == "$before" ]]'

echo
echo "$PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]

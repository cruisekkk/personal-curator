#!/bin/bash
# curator weekly review: unattended, idempotent cron wrapper.
#
# launchd fires this every 3h (StartInterval) and at load. This script is the
# guard: it runs only when (a) today is Sunday and (b) this ISO week's review
# does not exist yet.
# To produce a specific week by hand (skips the Sunday check):
#   CURATOR_WEEK=2026-W10 bash scripts/weekly_digest_cron.sh
#
# Auth, timeout, lock, retries and alerts live in cron_lib.sh.
# Exit codes: 0 done or nothing to do; 2 lock problem; 3 owner must
# re-authorize; 4 claude failed or timed out; 5 weekly file was not written;
# 6 transient failure.
set -u
source "$(dirname "$0")/cron_lib.sh"

LOCKDIR="$DIG/.weekly.lock"
WEEK="${CURATOR_WEEK:-$(date +%G-W%V)}"   # ISO year-week, same as the weekly JSON file name
DOW="$(date +%u)"                         # 1 = Monday .. 7 = Sunday
WJSON="$DIG/json/$WEEK.json"
WEEKLY_TIMEOUT="${CURATOR_WEEKLY_TIMEOUT:-2700}"

[ -n "${CURATOR_WEEK:-}" ] || [ "$DOW" = "7" ] || exit 0
[ -f "$WJSON" ] && exit 0

mkdir -p "$DIG"
acquire_lock "$LOCKDIR"
case $? in
  0) ;;
  1) exit 0 ;;
  *) alert weekly_lock "curator weekly lock has been held for too long, see the weekly log" now; exit 2 ;;
esac
trap 'rm -rf "$LOCKDIR"; rm -f "${RUN_OUT:-}"' EXIT

cd "$CURATOR_REPO" || exit 1
log "weekly wrapper started, producing the $WEEK review"

preflight_auth
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"

run_claude "$WEEKLY_TIMEOUT" --model "$CURATOR_MODEL" \
  -p "/curator-digest --weekly. Target week $WEEK (write json/$WEEK.json). Headless idempotent cron run: follow the weekly mode section of SKILL.md and print the report to stdout. CLIs are in $CURATOR_REPO/.venv/bin, data dir is $CURATOR_HOME." \
  --permission-mode acceptEdits --allowedTools "$TOOLS"
rc=$?
if [ "$rc" -ne 0 ]; then
  log "weekly failed: claude exit $rc (124 = timeout), retrying on the next tick"
  [ "$rc" -eq 3 ] && exit 3
  alert weekly_fail "curator weekly review has been failing for over 12 hours, see the log"
  exit 4
fi
if [ -f "$WJSON" ]; then
  rm -f "$ALERT_DIR/weekly_fail" "$ALERT_DIR/weekly_lock"
  log "weekly OK, $WEEK written"
  exit 0
fi
log "weekly finished but $WJSON does not exist, retrying on the next tick"
alert weekly_fail "curator weekly review has been failing for over 12 hours, see the log"
exit 5

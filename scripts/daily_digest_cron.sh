#!/bin/bash
# curator daily report: unattended, idempotent cron wrapper.
#
# launchd fires this every 2h (StartInterval) and at load (lid open / login).
# A fixed clock time does not work on a laptop: if it is asleep at that time
# the job is simply skipped. So we poll, and this script is the guard: if
# today's report already succeeded it exits at once, otherwise it runs the
# skill. Any 2h window in which the machine is awake produces today's report.
#
# Auth, timeout, lock, retries and alerts live in cron_lib.sh.
# Exit codes: 0 done or nothing to do; 2 lock problem; 3 owner must
# re-authorize; 4 claude failed or timed out; 5 watermark did not move;
# 6 transient failure. Every non-zero exit is retried on the next tick.
set -u
source "$(dirname "$0")/cron_lib.sh"

STAMP="$DIG/.last_daily_run"          # content = local date of the last successful run
LOCKDIR="$DIG/.daily.lock"
TODAY="$(date +%Y-%m-%d)"
DIGEST_TIMEOUT="${CURATOR_DAILY_TIMEOUT:-3600}"   # 60 min; the largest model is slow but writes better briefs

mkdir -p "$DIG"
[ "$(cat "$STAMP" 2>/dev/null)" = "$TODAY" ] && exit 0

acquire_lock "$LOCKDIR"
case $? in
  0) ;;
  1) exit 0 ;;
  *) alert daily_lock "curator daily lock has been held for too long, see the daily log" now; exit 2 ;;
esac
START_MARK="$(mktemp -t curator-daily)"
trap 'rm -rf "$LOCKDIR"; rm -f "$START_MARK" "${RUN_OUT:-}"' EXIT

cd "$CURATOR_REPO" || exit 1
log "daily wrapper started, producing the $TODAY report"

preflight_auth
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"

run_claude "$DIGEST_TIMEOUT" --model "$CURATOR_MODEL" \
  -p "/curator-digest daily mode. Headless idempotent cron run: follow SKILL.md end to end and print the final report to stdout. CLIs are in $CURATOR_REPO/.venv/bin, data dir is $CURATOR_HOME." \
  --permission-mode acceptEdits --allowedTools "$TOOLS"
rc=$?
if [ "$rc" -ne 0 ]; then
  log "daily failed: claude exit $rc (124 = timeout), retrying on the next tick"
  [ "$rc" -eq 3 ] && exit 3
  alert daily_fail "curator daily report has been failing for over 12 hours, see the log"
  exit 4
fi

# Success = the watermark file was written after this run started. The skill
# writes the watermark only when every step succeeded, so a clean exit code
# alone proves nothing.
if [ "$DIG/state.json" -nt "$START_MARK" ]; then
  echo "$TODAY" > "$STAMP"
  rm -f "$ALERT_DIR/daily_fail" "$ALERT_DIR/daily_lock"
  log "daily OK, marked $TODAY"
  exit 0
fi
log "daily finished but the watermark did not move, retrying on the next tick"
alert daily_fail "curator daily report has been failing for over 12 hours, see the log"
exit 5

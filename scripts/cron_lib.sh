#!/bin/bash
# Shared parts of the curator cron wrappers. Sourced by daily_digest_cron.sh
# and weekly_digest_cron.sh. macOS only (launchd, keychain, BSD stat).
#
# Goal: run unattended. The owner is notified only when something needs them
# in person (the long-lived token expired or was revoked).
#
# Auth: background runs do not rely on the CLI's own OAuth login, which expires
#   after about a month. They use a long-lived token from `claude setup-token`,
#   passed through CLAUDE_CODE_OAUTH_TOKEN. The token lives in the macOS
#   keychain, never in a plaintext file; scripts/setup_cron_auth.sh stores it.
#
# Self-healing:
#   - claude hangs                 -> hard timeout on the whole process group
#                                     (a run on 2026-08-20 hung for 69 days)
#   - lock holder died             -> lock is reclaimed (the lock records a PID)
#   - network / overload blips     -> health check retries with backoff; the real
#                                     run is retried once after a pause
#   - keychain briefly unreadable  -> skip this tick, try the next one; notify
#                                     only after 12h of it
#   - watermark did not move / output file missing -> non-zero exit, so the
#                                     next tick runs again
# Alert throttling: at most one notification per problem kind per 12 hours,
# cleared on success.

CURATOR_HOME="${CURATOR_HOME:-$HOME/.curator}"
CURATOR_REPO="${CURATOR_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")}"
CURATOR_MODEL="${CURATOR_MODEL:-opus}"
export CURATOR_HOME

DIG="$CURATOR_HOME/digests"
# Headless permission allowlist. Rules match on the command prefix, which is
# why the skill is told to call the CLIs by absolute path.
TOOLS="Bash($CURATOR_REPO/.venv/bin/curator-ingest:*),Bash($CURATOR_REPO/.venv/bin/curator-render:*),Bash(sqlite3:*),Bash(mkdir:*),Bash(cat:*),Read,Write,Edit"
KEYCHAIN_SERVICE="curator-claude-oauth-token"
ALERT_DIR="$DIG/.alerts"            # one file per problem kind: content = first seen, mtime = last notified
LOCK_MAX_AGE=$((4 * 3600))          # headroom above the hard timeout; a live holder older than this is abnormal
ALERT_EVERY=$((12 * 3600))

# When triggered by hand from inside a Claude Code session, the inherited
# CLAUDECODE variable makes `claude -p` refuse to run as a nested session.
unset CLAUDECODE

log() { echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') $* ==="; }

notify() {
  osascript -e "display notification \"$1\" with title \"curator\"" >/dev/null 2>&1 || true
}

# alert KIND MESSAGE [now] -> record a problem; notify at most once per 12h per kind.
# With "now" the first occurrence notifies (the owner must act); otherwise only
# once the problem has persisted for 12h.
alert() {
  local kind="$1" msg="$2" mode="${3:-}" f now first last
  mkdir -p "$ALERT_DIR"
  f="$ALERT_DIR/$kind"
  now=$(date +%s)
  if [ ! -f "$f" ]; then
    echo "$now" > "$f"
    touch -t 197001010000 "$f"        # not notified yet
  fi
  first=$(cat "$f")
  last=$(stat -f %m "$f")
  if [ "$mode" = "now" ] || [ $((now - first)) -ge "$ALERT_EVERY" ]; then
    if [ $((now - last)) -ge "$ALERT_EVERY" ]; then
      notify "$msg"
      touch "$f"
      log "notified: $msg"
    fi
  fi
}

clear_alerts() { rm -rf "$ALERT_DIR"; }

# run_with_timeout SECONDS COMMAND... -> 124 on timeout, else the command's exit code.
# macOS has no `timeout`. The command gets its own process group; on timeout the
# whole group gets TERM, then KILL 10 seconds later.
run_with_timeout() {
  local secs="$1"; shift
  set -m
  "$@" &
  local pid=$!
  set +m
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$secs" ]; then
      log "timeout after ${secs}s, killing process group $pid"
      kill -TERM -- "-$pid" 2>/dev/null
      sleep 10
      kill -KILL -- "-$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      return 124
    fi
    sleep 2
    waited=$((waited + 2))
  done
  wait "$pid"
}

# acquire_lock DIR -> 0 acquired; 1 another instance is running normally (exit
# quietly); 2 the holder is alive but older than LOCK_MAX_AGE.
acquire_lock() {
  local dir="$1" holder age
  if mkdir "$dir" 2>/dev/null; then
    echo $$ > "$dir/pid"
    return 0
  fi
  holder="$(cat "$dir/pid" 2>/dev/null)"
  if [ -z "$holder" ] || ! kill -0 "$holder" 2>/dev/null; then
    log "reclaiming stale lock $dir (holder ${holder:-unknown} is gone)"
    rm -rf "$dir"
    mkdir "$dir" 2>/dev/null || return 1
    echo $$ > "$dir/pid"
    return 0
  fi
  age=$(( $(date +%s) - $(stat -f %m "$dir") ))
  if [ "$age" -gt "$LOCK_MAX_AGE" ]; then
    log "lock $dir has been held by process $holder for ${age}s, over the limit"
    return 2
  fi
  return 1
}

# load_token -> 0 loaded; 1 not in the keychain (not configured); 2 keychain
# temporarily unreadable. Never prints the token. An already-set
# CLAUDE_CODE_OAUTH_TOKEN is used as is.
load_token() {
  [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && return 0
  local tok rc
  tok="$(security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$USER" -w 2>/dev/null)"
  rc=$?
  if [ "$rc" -eq 0 ] && [ -n "$tok" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$tok"
    return 0
  fi
  [ "$rc" -eq 44 ] && return 1      # errSecItemNotFound
  return 2
}

is_auth_error()      { grep -qiE "401|authentication_error|invalid.*(token|api key)|OAuth|not logged in|please run /login"; }
is_transient_error() { grep -qiE "overloaded|529|5[0-9][0-9] |rate.?limit|429|ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|network|socket hang up|fetch failed|timed out"; }

# reply_is_ok -> reads CLI output on stdin; 0 if any single line is "OK".
# Match per line (strip only in-line whitespace and periods, keep newlines):
# newer CLI versions print warning lines before the answer, so the output as a
# whole no longer equals "OK". On 2026-11-12 a whole-output comparison failed
# every health check and the daily report stopped.
reply_is_ok() { tr -d ' \t\r.' | grep -qxi "ok"; }

# preflight_auth -> 0 healthy; 3 the owner must re-authorize; 6 transient
# failure (retry on the next tick).
preflight_auth() {
  # Do not write this as case "$(load_token; echo $?)": command substitution
  # runs in a subshell and the exported token would be lost.
  local lt
  load_token; lt=$?
  case "$lt" in
    0) ;;
    1) log "no long-lived token in the keychain, falling back to the CLI's own login" ;;
    *) log "keychain temporarily unreadable, skipping this tick"
       alert keychain "curator has been unable to read the keychain for over 12 hours"
       return 6 ;;
  esac
  local out rc try delay
  for try in 1 2 3; do
    out="$(run_with_timeout 90 "$CLAUDE_BIN" -p "reply with the single word OK" --model haiku 2>&1)"
    rc=$?
    if [ "$rc" -eq 0 ] && printf '%s\n' "$out" | reply_is_ok; then
      rm -f "$ALERT_DIR/auth" "$ALERT_DIR/keychain" "$ALERT_DIR/transient"
      return 0
    fi
    echo "$out" | tail -2
    if echo "$out" | is_auth_error; then
      log "Claude authentication failed, the owner must re-authorize"
      alert auth "curator background auth expired. Run scripts/setup_cron_auth.sh in a terminal" now
      return 3
    fi
    delay=$((try * 60))
    log "health check failed (exit $rc, attempt $try), retrying in ${delay}s"
    [ "$try" -lt 3 ] && sleep "$delay"
  done
  alert transient "curator has been failing to reach Claude for over 12 hours"
  return 6
}

# run_claude TIMEOUT_SECONDS CLAUDE_ARGS... -> the real run. A transient error
# is retried once after 5 minutes. Output goes to the log and to $RUN_OUT (for
# the caller to inspect).
run_claude() {
  local secs="$1"; shift
  local attempt rc
  RUN_OUT="$(mktemp -t curator-run)"
  for attempt in 1 2; do
    run_with_timeout "$secs" "$CLAUDE_BIN" "$@" > "$RUN_OUT" 2>&1
    rc=$?
    cat "$RUN_OUT"
    [ "$rc" -eq 0 ] && return 0
    if [ "$attempt" -eq 1 ] && [ "$rc" -ne 124 ] && is_transient_error < "$RUN_OUT"; then
      log "claude hit a transient error (exit $rc), retrying once in 5 minutes"
      sleep 300
      continue
    fi
    if is_auth_error < "$RUN_OUT"; then
      alert auth "curator background auth expired. Run scripts/setup_cron_auth.sh in a terminal" now
      return 3
    fi
    return "$rc"
  done
  return "$rc"
}

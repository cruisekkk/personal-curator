#!/bin/bash
# Give the curator background jobs a long-lived token. One-off; the owner runs
# it in a terminal.
#
#   bash scripts/setup_cron_auth.sh            # full flow
#   bash scripts/setup_cron_auth.sh --store    # already have a token: store + verify only
#
# What it does:
#   1. Runs `claude setup-token`: authorize in the browser, the terminal prints
#      a long-lived token (needs a Claude subscription).
#   2. Reads the pasted token (terminals wrap it over several lines; the lines
#      are joined and all whitespace removed) and writes it to the macOS login
#      keychain through the stdin of `security -i`, so the token never appears
#      in a command line or in any file.
#   3. Makes one real Claude call with the stored token; some line of the reply
#      must be exactly OK.
#   4. Restarts the daily job so it runs once with the new setup.
# When the token later expires or is revoked the background job sends a
# notification; run this script again.
set -u
unset CLAUDECODE
CURATOR_HOME="${CURATOR_HOME:-$HOME/.curator}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")}"
SERVICE="curator-claude-oauth-token"
LAUNCHD_LABEL="${CURATOR_LAUNCHD_LABEL:-com.example.curator.daily-digest}"

if [ "${1:-}" != "--store" ]; then
  echo "Step 1: create a long-lived token. A browser page opens; when you finish, the terminal prints the token."
  echo
  "$CLAUDE_BIN" setup-token || { echo "setup-token failed, please retry."; exit 1; }
fi

echo
echo "Step 2: copy the whole token (select every wrapped line), paste it here, then press Enter once more (an empty line ends input). Input is hidden."
tok=""
while IFS= read -rs line; do
  [ -z "$line" ] && [ -n "$tok" ] && break
  tok+="$line"
done
tok="$(printf %s "$tok" | tr -d '[:space:]')"
echo
case "$tok" in
  sk-ant-oat01-*) ;;
  *) echo "The pasted text does not start with sk-ant-oat01-; nothing was stored."; exit 1 ;;
esac
if [ "${#tok}" -lt 90 ]; then
  echo "The token is only ${#tok} characters long, so it looks truncated (expect 100+); nothing was stored."; exit 1
fi
printf 'add-generic-password -U -s "%s" -a "%s" -l "curator Claude long-lived token" -w "%s"\n' \
  "$SERVICE" "$USER" "$tok" | security -i >/dev/null \
  || { echo "Writing to the keychain failed."; exit 1; }
unset tok
echo "Stored in the keychain."

echo
echo "Step 3: verifying the token..."
out="$(CLAUDE_CODE_OAUTH_TOKEN="$(security find-generic-password -s "$SERVICE" -a "$USER" -w)" \
  "$CLAUDE_BIN" -p "reply with the single word OK" --model haiku 2>&1)"
# Match "OK" per line, exactly like the preflight in cron_lib.sh. The CLI may
# print warning lines before its answer; stripping all whitespace from the
# whole output would glue them onto the "OK" and fail a perfectly good token.
if printf '%s\n' "$out" | tr -d ' \t\r.' | grep -qxi "ok"; then
  rm -rf "$CURATOR_HOME/digests/.alerts"
  echo "Verified."
else
  printf '%s\n' "$out" | tail -3
  echo "Verification failed: make sure you pasted the complete token, then run: bash $0 --store"
  exit 1
fi

echo
echo "Step 4: restarting the daily job (this also ends an old instance that is still hanging)."
if launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" 2>/dev/null; then
  echo "Done. Log: ~/Library/Logs/curator-daily-digest.log"
else
  echo "Token is ready, but the launchd job $LAUNCHD_LABEL is not loaded yet. See the README section on scheduled runs."
fi

#!/bin/bash
# Local-disk launchd entrypoint. One failed provider never prevents the other poll.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/bin:/bin:$PATH"
# Owner standard (2026-10-19): Python 3.14, backup 3.13; never system 3.9 or anaconda 3.12 (see py-standard.sh)
source "$HOME/.codex/bin/py-standard.sh" || exit 1
usage_poll_python="$STANDARD_PYTHON"
if [[ -n "${ORCH_USAGE_POLL_PYTHON:-}" ]]; then
  case "$("$ORCH_USAGE_POLL_PYTHON" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" in
    3.14|3.13) usage_poll_python="$ORCH_USAGE_POLL_PYTHON" ;;
    *) printf 'usage poll: ignoring unsupported ORCH_USAGE_POLL_PYTHON (need Python 3.14 or 3.13)\n' >&2 ;;
  esac
fi
if [[ ! -x "$usage_poll_python" ]]; then
  printf 'usage poll FAILED: selected Python unavailable\n' >&2
  exit 1
fi
# Mirrors pacing.STALE_AFTER_SECONDS; parity is enforced by a test.
STALE_AFTER_SECONDS=1800
usage_poll_failed=0
# Only the Claude subshell may receive headless credentials from the token file.
unset CLAUDE_CODE_OAUTH_TOKEN
for p in codex claude; do
  if [[ "$p" == claude && -f "$HOME/.codex/handoff/.claude-oauth-token" ]]; then
    # Headless auth: the long-lived setup-token the relay also uses (the interactive CLI login drops
    # intermittently and then /usage prints the API-billing view). Sourced in a subshell, never printed.
    out=$( source "$HOME/.codex/handoff/.claude-oauth-token" >/dev/null 2>&1; "$usage_poll_python" "$HOME/.codex/bin/${p}-usage-poll.py" 2>&1 ); rc=$?
  else
    out=$("$usage_poll_python" "$HOME/.codex/bin/${p}-usage-poll.py" 2>&1); rc=$?
  fi
  printf '%s %s rc=%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$p" "$rc" "$out"
  if [[ "$p" == codex ]]; then
    export USAGE_POLL_CODEX_RC="$rc" USAGE_POLL_CODEX_MESSAGE="$out"
  else
    export USAGE_POLL_CLAUDE_RC="$rc" USAGE_POLL_CLAUDE_MESSAGE="$out"
  fi
  if [[ $rc -ne 0 ]]; then usage_poll_failed=1; fi
done
"$usage_poll_python" "$HOME/.codex/bin/usage_alerts.py" "$STALE_AFTER_SECONDS"
exit "$usage_poll_failed"

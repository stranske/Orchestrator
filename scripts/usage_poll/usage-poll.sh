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
for p in codex claude; do
  if [[ "$p" == claude ]]; then
    # Dedicated login (2026-10-22): the poller owns its own CLI credential slot so the desktop app and other
    # sessions cannot rotate its refresh token (claude-code issues 54443, 88583). The setup-token is NOT used:
    # it lacks the user:profile scope that /usage needs (issues 24200, 22450, 28935).
    out=$( unset CLAUDE_CODE_OAUTH_TOKEN CLAUDECODE; export CLAUDE_CONFIG_DIR="${CLAUDE_POLL_CONFIG_DIR:-$HOME/.codex/claude-poll}"; "$usage_poll_python" "$HOME/.codex/bin/${p}-usage-poll.py" 2>&1 ); rc=$?
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

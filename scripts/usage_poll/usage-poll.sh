#!/bin/bash
# Local-disk launchd entrypoint. One failed provider never prevents the other poll.
export PATH="/opt/anaconda3/bin:/opt/homebrew/bin:$HOME/.local/bin:/usr/bin:/bin:$PATH"
usage_poll_python="${ORCH_USAGE_POLL_PYTHON:-/opt/anaconda3/bin/python3}"
if [[ ! -x "$usage_poll_python" ]]; then
  printf 'usage poll FAILED: selected Python unavailable\n' >&2
  exit 1
fi
# Mirrors pacing.STALE_AFTER_SECONDS; parity is enforced by a test.
STALE_AFTER_SECONDS=1800
usage_poll_failed=0
for p in codex claude; do
  out=$("$usage_poll_python" "$HOME/.codex/bin/${p}-usage-poll.py" 2>&1); rc=$?
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

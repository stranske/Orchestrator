#!/bin/bash
# Local-disk launchd entrypoint. One failed provider never prevents the other poll.
export PATH="/opt/anaconda3/bin:/opt/homebrew/bin:$HOME/.local/bin:/usr/bin:/bin:$PATH"
usage_poll_python="${ORCH_USAGE_POLL_PYTHON:-/opt/anaconda3/bin/python3}"
if [[ ! -x "$usage_poll_python" ]]; then
  printf 'usage poll FAILED: selected Python unavailable\n' >&2
  exit 1
fi
usage_poll_failed=0
for p in codex claude; do
  out=$("$usage_poll_python" "$HOME/.codex/bin/${p}-usage-poll.py" 2>&1); rc=$?
  printf '%s %s rc=%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$p" "$rc" "$out"
  if [[ $rc -ne 0 ]]; then usage_poll_failed=1; fi
done
exit "$usage_poll_failed"

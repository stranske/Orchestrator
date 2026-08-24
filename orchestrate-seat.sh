#!/usr/bin/env bash
# orchestrate-seat.sh — launch the ASSESSING orchestrator seat: a Claude (rotatable) session
# driven by ORCHESTRATOR.md, with the toolbox on PATH and the heartbeat set so the legacy cron
# lanes yield. The seat THINKS and delegates the coding to cheaper agents — it must not code itself.
#
# Usage:
#   orchestrate-seat.sh ["this-cycle instruction"]        # seat defaults to claude
#   orchestrate-seat.sh --agent codex ["instruction"]     # rotate the seat
#   ORCH_SEAT_DRYRUN=1 orchestrate-seat.sh                 # print the assembled prompt + exit (no seat)
set -euo pipefail
ORCH="${ORCH_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# This script reads a repo-root DOC, not a module, so it keeps the checkout root. The distinction
# matters since the modules moved under src/: `$ORCH/ORCHESTRATOR.md` would resolve to nothing.   # self-locating (code on Dropbox; runtime LOCAL)
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$HOME/.cursor/bin:$PATH"

SEAT="claude"
if [[ "${1:-}" == "--agent" ]]; then SEAT="${2:?--agent needs a value}"; shift 2; fi
CYCLE="${1:-Run one orchestration cycle: reap stale claims; assess capacity + backlog; redirect any in-flight work going wrong; then delegate the highest-value actionable work to the best-fit cheaper agents and monitor them. Conserve your own tokens — delegate the coding, do not write it yourself.}"

PROMPT="$(cat "$ORCH/ORCHESTRATOR.md")

---
## THIS CYCLE
$CYCLE"

if [[ "${ORCH_SEAT_DRYRUN:-0}" == "1" ]]; then
  printf '%s\n' "$PROMPT"; exit 0
fi

# Heartbeat: legacy cron lanes yield to the orchestrator while it is driving (stale in 15m).
printf '{"generated_at": %s, "pid": %s, "seat": "%s"}\n' "$(date +%s)" "$$" "$SEAT" \
  > "$HOME/.codex/handoff/orchestrator.json"

cd "$ORCH"
case "$SEAT" in
  claude)
    if [ -f "$HOME/.codex/handoff/.claude-oauth-token" ]; then
      set -a; . "$HOME/.codex/handoff/.claude-oauth-token"; set +a
    fi
    exec claude -p "$PROMPT" --dangerously-skip-permissions
    ;;
  codex)
    exec codex exec --skip-git-repo-check --sandbox workspace-write "$PROMPT"
    ;;
  *)
    echo "orchestrate-seat: unknown seat agent '$SEAT' (expected claude|codex)" >&2; exit 2 ;;
esac

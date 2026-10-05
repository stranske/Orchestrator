#!/usr/bin/env bash
# Regression for the tick's sweep recording default and operator kill switch.
# Run with: bash tests/check_redirect_sweep_defaults.sh
set -euo pipefail

test_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tick_script="${1:-$test_dir/../orchestrate.sh}"
test_tmp="$(mktemp -d)"
trap 'rm -rf "$test_tmp"' EXIT

# Execute only the sweep settings block: the full tick can dispatch agents and
# its prologue can load credentials. This fragment contains comments and exports.
awk '
  /^# Stage-2 evidence bridge/ { block = 1 }
  block { print }
  block && /^export ORCH_REDIRECT_SWEEP_BACKEND=/ { exit }
' "$tick_script" > "$test_tmp/sweep-settings.sh"

if [[ "$(grep -c '^export ORCH_REDIRECT_SWEEP_RECORD_CORPUS=' "$test_tmp/sweep-settings.sh")" != 1 ]]; then
  echo 'FAIL: expected one sweep recording export in the tick settings' >&2
  exit 1
fi
if ! grep -q '^# Consumer: redirect_apply.py' "$test_tmp/sweep-settings.sh"; then
  echo 'FAIL: the sweep settings must name redirect_apply.py as their consumer' >&2
  exit 1
fi
if ! grep -q 'redirect-sweep-live' "$test_tmp/sweep-settings.sh"; then
  echo 'FAIL: the consumer comment must name the stalled sweep candidate source' >&2
  exit 1
fi

for setting in unset empty 0 1; do
  actual="$(
    bash --noprofile --norc -c '
      set -euo pipefail
      case "$1" in
        unset) unset ORCH_REDIRECT_SWEEP_RECORD_CORPUS ;;
        empty) export ORCH_REDIRECT_SWEEP_RECORD_CORPUS="" ;;
        *) export ORCH_REDIRECT_SWEEP_RECORD_CORPUS="$1" ;;
      esac
      source "$2"
      # A child must inherit the value, as redirect_sweep does during a tick.
      bash --noprofile --norc -c '\''printf "%s" "$ORCH_REDIRECT_SWEEP_RECORD_CORPUS"'\''
    ' sweep-default-check "$setting" "$test_tmp/sweep-settings.sh"
  )"
  expected=1
  [[ "$setting" != 0 ]] || expected=0
  if [[ "$actual" != "$expected" ]]; then
    echo "FAIL: setting=$setting expected=$expected actual=$actual" >&2
    exit 1
  fi
  echo "PASS: setting=$setting child=$actual"
done

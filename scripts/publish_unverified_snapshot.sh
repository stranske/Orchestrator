#!/usr/bin/env bash
# Bypass the verdict, never the publication boundary. The legacy copier runs only
# against a private mirror and private HOME (including its separate registry).
set -euo pipefail
[[ "$#" == 2 ]] || { echo "usage: publish_unverified_snapshot.sh SOURCE MIRROR" >&2; exit 2; }
source_root="$1"
live_mirror="$2"
copy_script="${ORCH_SYNC_SCRIPT:-$HOME/.codex/bin/orch-sync-mirror.sh}"
python_bin="${PYTHON:-python3}"
runtime_registry="$HOME/.codex/orchestrator/repo_review_registry.json"
gh_config="${GH_CONFIG_DIR:-$HOME/.config/gh}"
stage_root="$(mktemp -d "${TMPDIR:-/tmp}/orch-unverified-sync.XXXXXX")"
trap 'rm -rf "$stage_root"' EXIT
# Resolve TMPDIR aliases before the copier makes snapshot-relative symlinks.
stage_root="$(cd "$stage_root" && pwd -P)"
mkdir -p "$stage_root/home/.codex/orchestrator"
if ! GH_CONFIG_DIR="$gh_config" GH_NO_UPDATE_NOTIFIER=1 \
    ORCH_PRIVATE_COPY_ROOT="$stage_root" HOME="$stage_root/home" ORCH_MIRROR="$stage_root/mirror" \
    bash "$copy_script" "$source_root"; then
  echo "NOT SYNCED: unverified scratch copy failed; live mirror untouched." >&2
  exit 2
fi
installer="$stage_root/mirror/scripts/install_verified_snapshot.py"
[[ -f "$installer" ]] || { echo "NOT SYNCED: scratch copy has no publisher." >&2; exit 2; }
# This digest binds the staged bytes, not a verification receipt. Never run verify.py
# or describe this route as verified. Structural validation and locks still apply.
digest="$("$python_bin" -I "$installer" "$stage_root/mirror" --digest)"
"$python_bin" -I "$installer" "$stage_root/mirror" "$live_mirror" \
  --expected-digest "$digest" --runtime-registry "$runtime_registry" --unverified

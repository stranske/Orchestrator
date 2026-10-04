#!/usr/bin/env bash
# Source immediately after the incumbent copier defines SRC and MIRROR, before
# mkdir/find/cp. Only the two staging callers may continue the in-place copier.
if [[ -n "${ORCH_PRIVATE_COPY_ROOT:-}" && -d "$ORCH_PRIVATE_COPY_ROOT" &&
      "$HOME" == "$ORCH_PRIVATE_COPY_ROOT/home" &&
      "$MIRROR" == "$ORCH_PRIVATE_COPY_ROOT/mirror" ]]; then
  return 0
fi
publisher="$SRC/scripts/publish_unverified_snapshot.sh"
if [[ ! -f "$publisher" ]]; then
  echo "NOT SYNCED: source has no guarded publisher; live mirror untouched." >&2
  exit 2
fi
# exec prevents returning to the incumbent deletion/copy after publication.
exec bash "$publisher" "$SRC" "$MIRROR"

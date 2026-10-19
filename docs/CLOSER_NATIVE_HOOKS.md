# Closer native handoff hooks (preview)

Bounded **source-owned** upgrade for per-lane round serialization in `handoff-prerun.sh` /
`handoff-postrun.sh`. This checkout ships only:

- `src/closer_native_lease.py` — `HANDOFF_EXACT_TOKEN_V1` lease acquire/release
- `scripts/render_closer_native_hooks.py` — fail-closed preview renderer
- `tests/test_closer_native_lease.py` — local protocol and subprocess tests

Live `~/.codex/bin/handoff-*.sh` are **read-only inputs** until an operator runs an explicit apply
(not performed by this module).

## Protocol

| Piece | Behavior |
|-------|----------|
| Parent lock | Short `fcntl` flock on `<canonical-lane-dir>.coord.lock` (same inode path for overrides / alias roots) |
| Lane dir | `$HANDOFF_ROOT/.lane-<lane>.lock` (or `HANDOFF_LANE_MUTEX_DIR`) |
| Acquire | Refuses **any** existing lane directory (no stale PID reap, no takeover) |
| Meta | Five fields: `owner_pid acquired_epoch lane agent token` |
| Token | `HANDOFF_EXACT_TOKEN_V1:<uuid>:<hex>:<inode>` |
| Release | Under the same parent lock; compares token exactly; `rmdir` only when matched — **no `rm -rf`** |
| Prerun env | `HANDOFF_REFUSE_EXISTING_LANE=1` passed into acquire (no age-based reap in preview) |
| Postrun env | `HANDOFF_EXPECTED_LANE_TOKEN` is the full meta line (single line + newline); bare token works for prerun halt cleanup only |
| CLI | `python3 closer_native_lease.py --handoff-root <root> <acquire\|release\|…>` — global `--handoff-root` must precede the subcommand |
| Invoke | `CLOSER_NATIVE_LEASE_INTERPRETER` + `CLOSER_NATIVE_LEASE_PY` (module path); do not execute the `.py` without an interpreter |

Generated shells record `HANDOFF_LANE_TOKEN` / `HANDOFF_EXPECTED_LANE_TOKEN` for halt and EXIT cleanup.

## Preview render

```bash
python3 scripts/render_closer_native_hooks.py \
  --prerun-source "$HOME/.codex/bin/handoff-prerun.sh" \
  --postrun-source "$HOME/.codex/bin/handoff-postrun.sh" \
  --output-dir /tmp/handoff-native-preview
```

Outputs:

- `handoff-prerun.sh` / `handoff-postrun.sh` (bash 3.2–compatible `#!/bin/bash`)
- `native-hooks-manifest.json` — source/output SHA-256, rollback hashes, integration gaps

Re-render is idempotent when sources are unchanged.

## Apply plan (not executed here)

1. Render preview to a distinct directory; review diff against canonical scripts.
2. Record manifest `outputs.*_sha256` and `pinned_native_hooks.lease_module_sha256` in Runtime
   **PinnedNativeHooks**.
3. Install rendered scripts only after separate runtime promotion; keep rollback SHAs from manifest
   `apply_plan.rollback`.
4. Set `CLOSER_NATIVE_LEASE_PY` to the pinned `closer_native_lease.py` path on the mirror.

## Integration gaps (honest)

- **Runtime** must pin script and lease-module SHAs; this repo does not edit dispatcher/installer.
- **Legacy actors** on installed scripts keep stale mutex reap and `rm -rf` EXIT trap until apply.
- **No live** native queue, global claims, or fleet tick validation in this unit.
- **`.verify-floor.json`** unchanged — +N tests require a separate floor owner / CI lag policy.
- **Capability ledger / admission** not registered here (integration owner).

## Rollback

Restore `handoff-prerun.sh` and `handoff-postrun.sh` bytes matching manifest
`apply_plan.rollback.restore_*_sha256`. No force-clear of lane directories: successors with valid
tokens remain protected.

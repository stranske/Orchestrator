# Closer agent CLI launchers (`src/closer_agent_launchers.py`)

Concrete per-agent bindings for **CLI execution** in the closer campaign context. This module
does not implement desktop application scheduling; `produce_launcher_receipt` still only observes
a pinned `closer_launcher.py` entrypoint process.

## Mechanisms

| Mechanism | Meaning |
|-----------|---------|
| `cli_offload` | `dispatcher.offload` with a registered immutable `execution_profiles` row and `adapters.build_command` argv |
| `desktop_native_handoff` | `PinnedNativeHooks` / `run_native_round` (requires `HANDOFF_EXACT_TOKEN_V1`; not qualified on installed helpers) |
| `desktop_app_scheduler` | Unsupported — no adapter claims app-level scheduling |

Approved pool: codex, claude, gemini, muse, cursor, vibe. **Aider excluded.** Vibe has CLI offload
profiles but is **not** in the native `handoff_agent` allowlist (`codex`, `claude_code`, `cursor`,
`gemini`, `muse`).

`desktop_native_supported` is true only when token-safe helpers are installed **and** qualified —
not from the actor allowlist alone. CLI presence (`readiness`) is separate from desktop qualification.

## Host launcher command

Production host manifests use `closer_distribution_package.host_binding_from_public`:

`{runtime.python} {launcher.path} run --manifest {host-manifest.json} --root {campaign_root}`

Legacy `closer_campaign.py tick` commands in a manifest are rejected for agent-cli dispatch.
`run_cli` requires `manifest_path` and `root`, loads the manifest via `closer_campaign.load_manifest`,
runs `policy_problem`, `qualify`, and `native_barrier` before any provider attempt, and delivers a
**bound request prompt** that cites only the verified launcher argv.

## API

- `inventory()` — agent → registered profile ids (no default profile recommendation)
- `readiness(agent, profile_id)` — explicit profile validity + bounded CLI probe (no auth)
- `verify_runtime_manifest` / `preflight_runtime` — pin, argv, policy, qualify, native barrier
- `plan_cli_run(...)` — argv preview + bound prompt (manifest path required)
- `run_cli(...)` — fails closed on prelaunch errors; preserves offload provenance fields

Shared entry: `closer_launcher.py agent-cli --manifest HOST_MANIFEST inventory` (or `readiness`,
`plan`, `run`). The shared launcher verifies its complete package before importing the binding.
An inner plan/run manifest must name the same file. `run_cli` rechecks the manifest and
policy after preparation, immediately before `dispatcher.offload`, and preserves the full
native result. Exit codes alone do not prove
a provider never started; uncertain submissions require reconciliation. Timeouts must be
explicit integers from 1 through 900 seconds (default 30).

A successful CLI transport result does not establish a verified Runtime result, queue acceptance,
or desktop qualification. Native callbacks in the Python API are trusted integration seams;
the CLI provides no override for the default qualification barriers.

Delegate prompts cite `plan.delegate_prompt_hint` only; that does not grant queue actions or promotion.

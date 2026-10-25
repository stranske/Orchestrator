# Non-Claude data-only admission boundary

Installed CLI inspection on 2026-10-25 found no complete, reviewed native
confinement contract for Cursor, Vibe, Antigravity or Muse. The adapter now
returns provider-specific fail-closed diagnostics through
`non_claude_data_only_boundary(agent, mode="assess")`. Its result always has
`status: unsupported`, `command_available: false`, unknown entitlement and
unassessed model suitability. The existing `data_only_problem` string/None API
uses that reason for non-Claude seats. It emits no candidate command and never
probes credentials, CLI processes, settings, capacity or provider APIs.

These are engineering blockers, not model capability failures. Installed-source
metadata, model names, environment flags and inert fixtures cannot enable a
transport. Claude's binary/hash/uncached-launch fence and all generic writing
provider branches remain unchanged. Profiles, routing, dispatcher, campaign and
role registry are outside this change.

## Installed evidence

The frozen `tests/fixtures/non_claude_data_only/installed-controls.json` records
versions, source references and hashes. It carries no qualification authority.

| Candidate | Locally observed controls | Missing boundary |
| --- | --- | --- |
| Cursor Composer 2.5 | Hidden allowed/excluded-tools and workspace/indexing exclusions | Empty allowlist is a remote header with unverified semantics; hook loading is independent; universal MCP/hook/no-fallback fencing not established. Wrapper hash omits bundled Node and transitive payload. |
| Vibe Medium 3.5 | Wildcard tool deny, empty MCP, hooks/connectors/context switches | Allows override denies; custom modules import before filtering; user/project agent/config discovery and Python/import/environment closure need isolation. Upstream model is mutable `mistral-vibe-cli-latest`. |
| Gemini Flash/Argon via agy | Print/plan/sandbox and slash-command controls | No complete all-tools/MCP/hooks/project-context/no-fallback launch fence was established. Binary model strings are not accepted-ID or entitlement evidence. |
| Muse Spark 1.2/1.3 and prior Wonder | Selective tool disables and empty preset | Universal tools/MCP/hooks/context/no-fallback semantics unestablished; wrapper may update and select another native payload. Spark strings are not entitlement evidence. |

Cursor inspection found independent enterprise/team/user/project/Claude hook
locations and session hooks; context exclusion alone does not stop them. Vibe
needs a cleared tool allowlist before wildcard denial, plus sterile home,
VIBE_HOME, cwd, import paths, config environment and agent/tool discovery paths.
Muse's native payload and auto-updating wrapper require separate identities.
No guessed flags or persistent MCP/security configuration changes were used.

## Offline validation

The inert executable fixtures only create a marker if invoked. Tests put them
on PATH, set tempting qualification environment values, and assert the normal
builder rejects before spawning, provisioning, ledger writes, or generic writing
fallback. Diagnostic mutations and wrong modes cannot change admission. The
existing offline data-only and Claude runtime-probe tests remain applicable.

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  tests/test_non_claude_data_only_boundaries.py \
  tests/test_offload_data_only.py tests/test_closer_role_runtime_probe.py
```

No model calls, credentials/settings changes, native tool attempts, new spending
or activation occurred. Static source inspection establishes missing guarantees;
it does not establish native confinement or provider suitability. A future
supported builder needs an independently reviewed immutable runtime closure,
sterile context, universal tool/MCP/hook fences, strict served identity/fallback
handling, and real shared-account accounting before activation can be considered.

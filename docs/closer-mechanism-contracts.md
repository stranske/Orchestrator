# Execution mechanism qualification

The existing Desktop qualification remains the default and requires independently observed
native Desktop events. Saved automation TOMLs, CLI transport completion, subprocess compatibility
receipts and manual notes do not qualify it. Native handoff remains held without its existing
supported native observer and exact-token helpers.

An explicit host-only `closer-mechanism/v1` binding can select `cli-offload/v1`. It names exactly
one agent/profile and a canonical private observation directory. The public package contains the
reader/supervisor code and its hash closure, never machine paths or policy readbacks.

`cli-configured-policy/v1` reads the canonical supervising controller and worker TOMLs afresh.
The required `worker_owner=codex` and `lane=closer` select the existing
`imi-merge-verify-closer` lane plus `reviewed-repo-automation-controller`. Both must be ACTIVE and
match the exact host-pinned bytes. Sync is excluded. The separate `agent` field names the actual
offload provider from the approved Codex, Claude, Gemini, Muse, Cursor or Vibe pool; it must match
the mechanism contract and its exact approved profile. Aider is excluded. The saved TOML model
names the outer supervising seat, not the delegated provider. No provider-specific worker TOML
or Desktop app state is inferred from this policy.

This policy states configured CLI pause intent. Changing policy bytes, provider or profile requires
a new host binding and that provider/profile's own actual supervised qualification receipt. A
Codex receipt cannot qualify Claude or any other provider. Other supervising owners remain held
until an exact canonical mapping is reviewed. Provider quota and capacity guards remain separate;
this binding never grants a quota override or promotes an unqualified member of the approved pool.

Supported low requests have distinct immutable profile IDs: `claude-sonnet-5-5-low` sends
`--model claude-sonnet-5-5 --effort low`, and `muse-wonder-1.0-low` sends the existing account model
with `--reasoning-effort low`. Both use their existing subscription pools. The old high profile
rows remain unchanged; corrected future calls explicitly send high, while historical v1 attempts
that omitted effort are not retroactively proven. Unspecified legacy effort adds no flag.

`cursor-gpt-5.6-luna-low` names a model advertised by the installed CLI. It is explicit-only;
normal inventories, routing and default offloads stay on Composer. A named profile can qualify
only its own CLI lifecycle; normal campaign tick selection does not automatically roll it out.
Vibe remains capable pending separate integration of its per-invocation configuration and proof;
these entries do not establish current provider availability or successful queue acceptance.

The pinned launcher's `qualify-cli` command starts two bounded submissions through the existing CLI
offloader, with no provider retries. It first persists intent, exact command/profile/nonce and the
original hard/action deadlines. `--budget-seconds` is required: five cleanup seconds are
reserved inside that overall budget before either provider starts; failure never extends it.
Early validation failure drains the known provider within this budget and signals only the
identity-verified no-effect child or private transport group. Signal denials and pending outcomes
remain explicit, block resubmission, and grant no qualification. The selected agent is asked only to invoke `qualification-child`, an explicit
no-effect production launcher mode. The supervisor independently observes the exact OS argv/start
identity and ancestry, durable handoff, controlled crash of only that identified child, terminal
observation, restarted child and durable result. The child cannot call queue, GitHub, native hooks or
claims. Provider exit 0 without these observations remains held. Unknown submissions retain intent
and prohibit another attempt; `cli-supervision-status` reads their evidence without clearing it or
creating a new budget.

Process ownership is established from parent/start metadata before any argument query. The bounded
trusted reader uses Linux `/proc/<pid>/cmdline` or macOS `KERN_PROCARGS2`, never rendered `ps`
command text. It emits only argument count and the SHA-256 of the exact NUL-delimited argument
bytes; multiline, Unicode and empty subsequent arguments retain their boundaries. The macOS
[SDK/kernel API](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_sysctl.c)
can return trailing environment bytes in its mutable buffer: the parser consumes only the `argc`
argument prefix, never inspects/copies/hashes those opaque extras, and zeros the buffer in `finally`.
Only independently proved owned lineage is queried. The reader has a fixed 1 MiB ceiling and an
original-deadline subprocess bound; a permission denial holds without another API or rendered-text
fallback. Neither raw provider arguments nor prompts are written by this observer.

The signed local observation binds mechanism, package/state, host policy, exact agent/profile and
phase artifacts. A fresh launcher process validates it from the fixed reader implementation; a
caller-selected observer, module or unsigned receipt grants no authority. The signature detects
ordinary receipt substitution/corruption. It is **not an OS privilege boundary against a malicious
same-user process**: the host, reviewed supervisor and providers' adherence to their scoped task are
trusted. The signing key is never included in the agent prompt. No private provider session storage
is read. Persisted receipts share the normal user-owned state trust model.

Qualification proves only this CLI lifecycle integration. It does not prove a guarded queue action,
source completion, Core ownership or Desktop qualification. Every actual tick and worker retains
fresh pauses, capacity/profile matching, exact package/head/provenance, lane and target ownership,
Core claims, reconciliation and uncertain-outcome barriers. A CLI-qualified campaign cannot turn a
native round with missing native hooks into an executable round. Actual queue action acceptance
requires its original real evidence and normal guarded action receipts.

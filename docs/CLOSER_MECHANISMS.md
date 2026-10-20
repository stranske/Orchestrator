# Scoped operational capacity and bounded native hooks

`closer_operational_capacity.py` is an opt-in `closer-operational-capacity/v1` producer.
It reads only the canonical Codex/Claude operational usage-poll JSONs and read-only shed
markers. It never calls the global capacity build, ccusage, private usage/session
readers, marker recovery, or marker deletion. Decision `generated_at` is evaluation
time; poll timestamps, reset times, source and input hashes remain original. A fresh
decision is not a fresh quota observation. Known exhaustion/ordinary-usage denial and
active/manual/unreadable holds win. Expired markers are left on disk. No reset inventory
is spent or altered.

A private config supplies the exact active `profile_ids`, original deadline (maximum 900
seconds), owner and physical Runtime-manifest hash. Default missing or stale usage is
unavailable. `bounded_trial_agents` may explicitly permit approved profiles when the
existing accounting model supports 429-authoritative trials (metered/flat, or ccusage
with no configured token ceiling). This derives from current policy, not permanent
provider categories. Native ordinary-usage permission remains required where that
provider supplies it; Codex missing permission cannot be waived. Windowed prepaid
accounting needs operational counters not supplied by this scoped producer and remains
unavailable unless a real hold already sheds it. This grants bounded permission with
**unknown remaining quota**, not quota proof. It does not establish auth, provider
availability, model availability, lifecycle qualification or action acceptance. Those
existing submission guards still run. Unresolved provider outcomes must remain excluded
from the trial profile scope. A low-only report cannot authorize higher profiles sharing
the same pool. Aider is excluded. No new profile or provider implementation is enabled
by a capacity decision; Vibe low wiring, for example, remains independently unsupported.
No fabricated usage estimates are added.

The CLI takes `--config`, distinct absolute `--output`, and mandatory `--expected-sha256
<oldhash|absent>`. Publication uses exclusive lock, owner and CAS, atomic replacement,
fsync and readback. It refuses the global `capacity.json`. Set explicit
`CLOSER_CAPACITY_REPORT` for the closer consumer, or private native round config
`capacity_report` to propagate it through A/B/C. Default consumers retain their existing
report path. Scoped reads enforce schema, producer hash, exact profile scope and
original deadline. Canonical poll/marker hashes and absences are reread at consumption;
a new hold or updated poll invalidates the old report. Refresh only by CAS under the
same original deadline. The campaign's existing `capacity_json` route input should point
to the same report. Do not copy old agent states and update their timestamp, and do not
relocate `HANDOFF_DIR`.

`render_closer_native_hooks.py --bounded-round` produces private hash-pinned copies
retaining normal preflight hygiene and C's stop validation/end-round. Only a **new
provider relay** is suppressed, at its launch boundary, when the explicit Runtime policy
`native_hooks.bounded_no_new_relay=true` is set. Normal scheduled continuation and
global helper files are unchanged. No mutex-only mode is appropriate for a real round.
In this mode A emits a fresh per-invocation nonce-bound verdict at its canonical check-
stop branch boundary before resume memory. Only its exact ordinary record admits B;
peer-review, missing, wrong or ambiguous records refuse even on exit0. Historical memory
strings have no admission authority. The cleared-review race reaches the ordinary
verdict. Unbounded legacy artifacts retain conservative marker compatibility; an
acquired token still receives one C attempt. Unknown stop outcomes remain unknown and
preserve native evidence.

Bounded provisioning additionally pins `postrun_dependencies` for the physical current
Runtime Python, resolved physical generation dispatcher behind the canonical installed
mirror anchor (same bytes as Runtime), and `native-assignment-snapshot.json` beside
`dispatch_state`. The snapshot must be a private immutable copy of the canonical
controller assignment; its hash must still equal canonical bytes at A admission.
`cfg.assignment` and actual B argv must name this same snapshot. C validates the
original admitted snapshot, not a later controller generation; a changed canonical
source is reported explicitly. The logical mirror anchor must still resolve to that same
physical generation. Rechecks happen before A/B/C. A post-admission dependency change
invokes cleanup-only C (exit65), retaining the original stop/effect uncertainty while
the exact-token EXIT cleanup attempts release. Changed helper or lease-module pins
refuse execution and retain ownership for reconciliation. These are host-local pins, not
public package paths or fictional native events. CLI lifecycle qualification still does
not establish Desktop qualification or successful queue delivery. Provisioning/execution
remains an owner operation after review, verified install, fresh native idle and
original deadline checks.

The bounded lease helper and B argv also use the physically pinned Python, not
a later resolution of a lexical symlink. The original Python anchor must still
resolve to that physical interpreter. Changed interpreter bytes or retargeting
fence execution; cleanup cannot invoke an untrusted binary merely to release a
lease. C returns65 and retains the lease and uncertainty for reconciliation.

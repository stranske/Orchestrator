# Issue 417: batch offering and current acceptance proof

The existing PromptAgent, `run_prompt_batch`, advisor match/decline events,
and dispatcher gate implement this scope. No new capability or event store is
introduced. The `role-prompt` bindings cover `research-program` and
`repo-audit:phase-4`; an explicit batch routes once while retaining a separate
role run for each body. Automatic dispatch remains off when `ORCH_ROLE_SHADOW`
is unset, and the delegate CLI reports that fact once per process.

Single-body, one-prompt, and no-batch decline reasons are non-demotable
`wrong_moment` on both writes and historical reads. The named regression checks
sixty events across spelling/kind combinations and preserves ledger bytes.
Phrases outside this bounded classifier remain governed by the existing policy;
this issue does not claim a general natural-language classifier.

## Current reproducible acceptance

On 2026-10-05, against merge `4f952e3b666b04dd100ed5d698650c3bf7ead87c`,
`/opt/anaconda3/bin/python3.12 scripts/verify_role_prompt_offering.py` completed
all three real pytest controls below. Each private module starts from the
checkout bytes, passes its baseline, fails exactly one named assertion after
mutation, and passes after byte-exact restoration. JUnit counts reject missing
or stale reports, import/usage errors, skips, and unrelated failures. No live
ledger, Brain, mirror, registry, or dispatch state is written.

| Control | Named test in `tests/test_role_prompt_offering.py` | Baseline | Broken | Restored |
| --- | --- | --- | --- | --- |
| Remove research-program consult site | `test_research_program_surface_is_declared_and_binds_role_prompt` | 1 pass, exit 0 | 1 failure, exit 1 | 1 pass, exit 0 |
| Make wrong_moment demotable | `test_wrong_moment_declines_never_demote` | 1 pass, exit 0 | 1 failure, exit 1 | 1 pass, exit 0 |
| Count each body against cycle cap | `test_a_batch_counts_once_against_the_cycle_cap` | 1 pass, exit 0 | 1 failure, exit 1 | 1 pass, exit 0 |

The script is the durable replay surface and prints full JSON receipts. Runner
scratch paths are temporary inputs, not acceptance dependencies. Run it with a
Python environment containing the pinned pytest prerequisites.

The focused offering/batch/receipt suites pass 29 cases plus six receipt
subtests. Whole-tree Black passes. Current-head CI remains authoritative for
checkout and mirror-shape verification; historical no-pytest/no-Black runner
limitations do not describe the present source or its readiness.

## Post-merge comparison disposition

Compare run `37308125678` evaluated the exact merge above and returned
CONCERNS/CONCERNS, preserved as NON_PASS. Its concrete diagram finding is valid:
the merged SVG contained conflict markers and XML parsing rejected line 3.
The follow-up removes the duplicated conflict block while retaining both batch
bindings and the value-chain description; XML parsing now passes.

This compact record supersedes historical interim notes that described missing
pytest/Black, uncommitted work, and ephemeral evidence paths. It supplies the
required replay commands and measured assertion/restoration outcomes directly.
The provider's truncated inspection is an evidence limitation, not a provider
PASS. Source issue 417 stays open until the bounded follow-up is gated, merged,
and its durable comparison is explicitly dispositioned. Live publication stays
manual and separate.

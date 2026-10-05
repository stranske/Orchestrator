# Issue 417: batch offering and per-decision cap proof

The existing `run_prompt_batch` API routes once and records each body independently.
This change extends that API and the existing advisor/decline event path; it creates
no new capability, event log, or dispatch path. The concept search, current capability
inventory, and improvement-log search confirmed existing PromptAgent and batching.

The advisor binds `role-prompt` at `research-program` (driver consult) and
`repo-audit:phase-4` (issue filing). Single-body / one-prompt / no-batch reasons
are classified as non-demotable `wrong_moment` on new writes and on historical reads;
the detector regression checks the original ledger bytes remain unchanged.
The automatic dispatch gate remains off when `ORCH_ROLE_SHADOW` is unset;
the delegate CLI prints that fact once. Explicit batch authoring does not flip it.

## Deliberate-break and restoration

Each control used a temporary copy of one module ahead of `src` on pytest's
configured import path. Only the private copy was mutated and restored.
Every control returned exit 1 with one named failure, then exit 0 with one named
pass after restoration; the checkout source remained byte-identical throughout.

| Mutation | Named test in `tests/test_role_prompt_offering.py` | Broken | Restored |
| --- | --- | --- | --- |
| surface | `test_research_program_surface_is_declared_and_binds_role_prompt` | 1 failed, exit 1 | 1 passed, exit 0 |
| demotion | `test_wrong_moment_declines_never_demote` | 1 failed, exit 1 | 1 passed, exit 0 |
| per-body-cap | `test_a_batch_counts_once_against_the_cycle_cap` | 1 failed, exit 1 | 1 passed, exit 0 |

The cap control increments the prompt-role counter for each body. A three-body
batch then reports more than one invocation at the next capped decision, which
the regression rejects. A normal batch reports one invocation and keeps three
distinct role runs; a second batch is withheld without publishing bodies.

Commands: `python3 -m pytest tests/test_role_prompt_offering.py -q`,
`python3 src/verify.py`, and the named test selections with
`-o "pythonpath=<private-module-dir> <checkout>/src <checkout>/tests"` for each control.
Complete RED/GREEN logs and JSON receipts are retained in the automation's
`evidence/20261005T0202Z` directory. CI's full verifier remains authoritative for
the pushed PR head. No live mirror deployment is part of this PR.

Initial checkout verdict: `python3 src/verify.py` passed 1,747/1,747 collected tests,
99/99 selftests, all five gates, and all 115 mypy modules with no exemptions.
The six focused cases also passed; the measured floor is 1,747, with no skip or
exemption ceiling increase.

After concurrent PR #452 merged, this branch was rebased onto main `1f06bc3`.
Both floor histories were preserved; measured collection is 1,757. The rebased
full verifier passed 1,757 tests, 99 selftests, all five gates, and all 115 mypy
modules. The six new cases account for this branch’s collection delta; all ceilings
remain unchanged.

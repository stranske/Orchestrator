# Issue #556: delivery eligibility correction

`no_merge` now requires positive no-delivery provenance from the recorded run. The offload
writer records `mode=offload` and returns work without a claim or PR, including research work.
A recorded PR or merge takes precedence over that mode. Local/remote runs retain their delivery
semantics; other modes without delivery evidence are explicitly `unknown`. A run id prefix,
missing PR, or NULL/False merge state alone is insufficient.

Both effectiveness and value-chain readers load run mode and PR identity and use the same
eligibility helper and outcome classifier. Association, experiment-member, learning-exclusion,
abandoned-unmerged, merged-pending, distinct-subject sampling, and verifier-failure rules remain
covered by the affected suites.

## Verified work

- [x] Establish positive evidence before classifying a run as `no_merge`.
- [x] Preserve the separate verdict rate, distinct-subject minimum sample, and verifier veto.
- [x] Cover ordinary PR-bearing FAIL_BROKEN, PASS, and NON_PASS with NULL/False merge and pending
  durability through production writers.
- [x] Keep `value_chain_monitor` consistent with the corrected classifier.
- [ ] Complete hosted verification and controller-owned review/merge/actual comparison.

## RED and restored GREEN

Temporarily restored only `src/capability_effectiveness.py` from merged #555,
`49cdbfee4dba260a5e9149bdd979b7ebca4fd06b`, and ran:

```sh
python3 -m pytest tests/test_capability_effectiveness_predicates.py \
  -k pending_pr_outcome -m 'not slow' -q --tb=short
```

All six cases failed as intended. FAIL_BROKEN and NON_PASS returned
`(terminal_subjects, durable_subjects, durable_rate) = (5, 5, 1.0)` instead of `(6, 5, 0.833)`;
PASS was incorrectly classified `no_merge` instead of `open`. Restored the corrected source
in a `finally` block, then verified the affected suites:

```sh
python3 -m pytest tests/test_capability_effectiveness_predicates.py \
  tests/test_capability_effectiveness_outcomes.py \
  tests/test_capability_effectiveness_experiment_members.py \
  tests/test_value_chain_monitor.py tests/test_value_chain_monitor_outcomes.py \
  tests/test_lifecycle_learning_excluded.py -m 'not slow' -q
python3 src/capability_effectiveness.py --selftest
python3 src/value_chain_monitor.py --selftest
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
```

Results: **149 passed**, both selftests passed, **393 Python files unchanged** by the Black check.
Black matched the repository pin, **26.10.0**. Missing pytest/Black tooling was recovered from
pinned official GitHub sources into `/tmp`; the Black launcher ran its normal formatter serially
because process-pool execution stalled in this sandbox. No repository dependencies were changed.

The tests use temporary Brain databases; the effectiveness selftest uses in-memory SQLite and
a temporary ledger. The monitor selftest exercises fixtures directly. No live Brain was mutated.

Hosted exact-head checks, review/merge, and post-merge actual comparison are **not attested** by
this local result. The final source commit must pass those lifecycle steps before the last task
and the final acceptance criterion are checked off.

This workspace mounts `.git` read-only: normal staging/commit failed creating `index.lock`.
The source commit is therefore recorded in `/tmp/orchestrator-556-git`, a bare clone using this
checkout as its work tree. The verified edits remain in this checkout for the automation runner
to publish. The mounted branch/ref was not changed.

## Follow-up lifecycle verification (2026-10-12)

Six new production-writer cases follow a recorded PR from NULL/False merge state through
merged/pending and durable, then a later verifier NON_PASS. Both readers preserve the five
existing durable subjects throughout: the sixth joins the durable population only after the
sweep's judgment, and NON_PASS reduces its durable rate to 0.833. PR identity takes precedence
over unknown, local, and offload mode. The sweep selector is checked at every stage.

The same six affected suites now pass **155 tests** with `-m 'not slow'`; both module selftests
pass. Collection with that marker selects **3,097 tests**, six above the prior hosted collection
of 3,091. Black **26.10.0** formatted the changed test and the required repository-wide check
passed with **393 files unchanged**, using its normal formatter serially in the sandbox.
All local checks use temporary state; no live Brain was mutated.

Hosted CI run [38266959305](https://github.com/stranske/Orchestrator/actions/runs/38266959305)
passed checkout and flat-mirror verification on prior head
`f43c260a545b8fd9d42c96b46da300b95e46420b`; its checkout log reports 3,065 pytest passes,
26 allowed skips, and 97 selftest passes with seven allowed skips. Gate rerun
[38268012441](https://github.com/stranske/Orchestrator/actions/runs/38268012441) also passed.
These receipts do not cover the follow-up commit. Fresh exact-head hosted verification,
controller-owned review/guarded merge, and post-merge actual comparison remain outstanding.

The required active `src/merge_guard.py` invocation with the follow-up commit as
`--expected-head` blocked at **review-thread GraphQL query failed**; `merge_executed` was false.
This runner cannot connect to `api.github.com` through `gh`. Connector attempts to add the
`needs-human` label and post the blocker comment were also rejected: both require approval,
while this run's approval policy is `never`. Neither mutation occurred. The verified changes
remain in this checkout and the writable `/tmp` clone for the automation runner to publish;
the lifecycle checkbox remains unchecked.

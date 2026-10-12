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

## Verifier-writer RED/restored GREEN receipt (2026-10-12)

The six-subject regression and the six monitor regressions now call
`local_verify.record_verdict` directly. This is the production writer identified in #556:
it supplies no merge update. For NULL, it inserts the outcome; for False, the test first
records `merged=False` and verifies that the verifier preserves it. Every case verifies
`durability=pending` in the stored outcome. The test count is unchanged.

After reviewing commits `f43c260a545b8fd9d42c96b46da300b95e46420b` and
`b38bd8765a57e3bba0bc88dcc1ccc8f89b07b105`, the first four primary task checkboxes
are verified complete. Updating the PR body was attempted before proceeding, but the
connector returned `MCP tool call requires approval, but approval policy is never`.
The remote checklist was not changed. The combined hosted/review/merge/comparison task
remains incomplete.

Source bindings for this receipt:

| Binding | Git blob or SHA-256 |
| --- | --- |
| RED commit (merged #555) | `49cdbfee4dba260a5e9149bdd979b7ebca4fd06b` |
| RED `src/capability_effectiveness.py` SHA-256 | `2601f7ff5b71cc3748e344870bd0aaac791cee5f6166eab780a09c9d4e32a5b2` |
| Restored GREEN source Git blob | `770d315257e2402906c4ddbee2bc52047f9dd034` |
| GREEN source SHA-256 before and after restoration | `3414f0ecbe839a5074acf248dfa00bd6d674c271c998b73d525d5ca36262d207` |
| Predicate test Git blob | `cabc2ce5924c63dc6ce6bd28b16ac98d3c179355` |
| Predicate test SHA-256, identical for RED and GREEN | `4678b082aae266528c7e4beb55cb8054b6a349702d9fb9da0ccfe44b54ee72d9` |
| Monitor test Git blob | `fbfac45e124c23d8c864adc8067edc673c357cac` |

Only the classifier source was replaced with the bytes returned by:

```sh
git show 49cdbfee4dba260a5e9149bdd979b7ebca4fd06b:src/capability_effectiveness.py
```

For each source state, the same command ran with pytest 8.3.5 on Python 3.14.8:

```sh
python3 -m pytest tests/test_capability_effectiveness_predicates.py \
  -k pending_pr_outcome -m "not slow" -q --tb=short
```

RED returned exit 1: **6 failed, 41 deselected**. Four failing-verdict cases reported
`(5, 5, 1.0)` instead of `(6, 5, 0.833)`; the two PASS cases reported `no_merge`
instead of `open`. There were no collection errors. A `finally` block restored the
saved corrected source bytes, and a direct byte comparison returned true before
GREEN ran. GREEN returned exit 0: **6 passed, 41 deselected**. These commands used
temporary Brain databases and fixture state; no live Brain was mutated.

- [x] Run the six intended failures on merged #555 and six passes after byte-identical
  restoration, capturing commands, results, and source bindings without live Brain mutation.
- [ ] Complete hosted verification of the verifier-writer changes and the normal
  review/guarded merge/actual-comparison lifecycle.

The six affected suites passed **155 tests** after the writer changes; both module
selftests passed. Black 26.10.0 left both changed Python files unchanged, and the required
repository-wide `black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`
passed with **393 files unchanged**. Tooling was
recovered into `/tmp` from pinned official sources, with installed pip-vendored dependencies;
Black schedules its normal formatter serially because this sandbox denies the socket
required by Python 3.14's process pool. No repository dependencies were changed.

Hosted CI [38269807578](https://github.com/stranske/Orchestrator/actions/runs/38269807578)
and Gate [38269808924](https://github.com/stranske/Orchestrator/actions/runs/38269808924)
passed on prior head `b38bd8765a57e3bba0bc88dcc1ccc8f89b07b105`. The checkout verify
job reported **3,071 passed, 26 allowed skips, 3,097 collected** and **97 of 104
selftests ran, seven allowed skips**. These receipts cover that prior head only.
The merge guard's dry-run with that expected head returned `blocked=true`,
`reason=review-thread GraphQL query failed`, and `merge_executed=false`. No merge
was attempted outside the guard, and no post-merge actual comparison is attested.

Attempts to add `needs-human` and post the blocker comment were also rejected with
`MCP tool call requires approval, but approval policy is never`; neither mutation occurred.
The mounted `.git` is read-only, so this round's test and evidence commit is recorded in
the writable bare clone `/tmp/orchestrator-556-git`, using this checkout as its work tree.
The automation runner must publish the checkout changes and reconcile the PR body;
the remote PR was observed open and ready for review, with no review threads.

## Late delivery evidence and comparison disposition (PR #562, 2026-10-12)

The correction is already in merged PR #557 (`a533419adc65873d209e31120211e9612b10eae4`).
This round adds seven production-writer regressions rather than changing that classifier again.
Six follow an unchanged verifier outcome through recorded run provenance changing from unknown,
to offload without delivery, to offload with a recorded PR. PASS, FAIL_BROKEN and NON_PASS each
cover NULL/False merge state. Both readers must re-read the run evidence on each report: only
the proven offload stage enters the verdict population. The seventh checks that offload mode
without any outcome is still open, with no scored verdict.

The six affected suites ran with the command above and `-m "not slow"`: **162 passed**
(the original 155 plus these seven). Both module selftests passed. Collection with
`python3 -m pytest --collect-only -m "not slow" -q` reported **3,104 tests collected**.
`.verify-floor.json` now records that measured collection, with all skip/mypy ceilings preserved;
the `passed` field is the repository's collection expectation, not an assertion that all 3,104
tests were executed locally. Full hosted execution is still required for this follow-up.

The required RED/restored GREEN command was rerun against identical test bytes:

```sh
python3 -m pytest tests/test_capability_effectiveness_predicates.py \
  -k pending_pr_outcome -m "not slow" -q --tb=short
```

RED used `git show 49cdbfee4dba260a5e9149bdd979b7ebca4fd06b:src/capability_effectiveness.py`
and returned exit 1: **6 failed, 48 deselected**. The four FAIL_BROKEN/NON_PASS cases again
reported `(5, 5, 1.0)` instead of `(6, 5, 0.833)`; the two PASS cases again reported `no_merge`
instead of `open`. A `finally` block restored the saved source bytes and a direct byte
comparison returned true. GREEN returned exit 0: **6 passed, 48 deselected**.

| Binding | Git blob or SHA-256 |
| --- | --- |
| RED source SHA-256 | `2601f7ff5b71cc3748e344870bd0aaac791cee5f6166eab780a09c9d4e32a5b2` |
| GREEN source blob (unchanged from #557) | `770d315257e2402906c4ddbee2bc52047f9dd034` |
| GREEN source SHA-256, before and after restoration | `3414f0ecbe839a5074acf248dfa00bd6d674c271c998b73d525d5ca36262d207` |
| Predicate test blob, identical during RED and GREEN | `fa49b65fe5f3d45bfd509708f2641611e8c38a70` |
| Predicate test SHA-256 | `889349f4b89d4c6f8f640c4eb97bdc1383b29eda0e9d2b2ca4971abae055208c` |

Tests used temporary Brain databases, and both selftests used their offline fixtures.
No live Brain was mutated. Pytest 8.3.5 and Black 26.10.0 were recovered into `/tmp` from
pinned official sources; repository dependencies were not changed.

Black formatted the changed test and the required
`black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`
passed: **393 files would be left unchanged**. Its default asynchronous scheduling stalled
in this sandbox and was interrupted; a `/tmp` launcher then scheduled Black's unchanged
`reformat_one` formatter serially. Formatting and checking semantics were preserved.

The actual [provider comparison report for #557](https://github.com/stranske/Orchestrator/pull/557#issuecomment-6130287116)
evaluated merge `a533419adc65873d209e31120211e9612b10eae4` and returned **NON_PASS**:
OpenAI PASS, Anthropic CONCERNS. It must not be described as a provider PASS merely because
the comparison workflow completed. Its CI concern can now be attributed precisely:
[CI run 38291043273](https://github.com/stranske/Orchestrator/actions/runs/38291043273),
checkout [job 114950936404](https://github.com/stranske/Orchestrator/actions/runs/38291043273/job/114950936404),
reported **3,071 passed, 0 failed, 26 skipped, 3,097 collected**, but failed because the recorded
collection floor was **3,064**, 33 behind. The flat-mirror and coverage jobs succeeded, and the
main-only floor-reconciliation job succeeded. Main's subsequently published floor is 3,097.
This follow-up's measured floor of 3,104 incorporates the seven new tests.

The report's evidence concerns are addressed locally by the fresh RED/GREEN receipt above,
its unchanged classifier binding, and this explicit comparison disposition. Its concern about
unrecognized modes is intentional: without positive provenance, an unknown legacy run cannot
be claimed to have no delivery path. `dispatcher.offload` records `mode="offload"` for its runs.
These findings do not change the published NON_PASS or attest to fresh hosted checks on this
follow-up. The combined hosted verification/review/guarded merge/actual comparison task remains
unchecked until the controller completes those steps on the final published head.

Publication was attempted from `/tmp/orchestrator-556-round-git`, a writable bare clone
using this checkout as its work tree because the mounted `.git` is read-only. The commit
contains only the predicate tests, measured collection floor, and this evidence document.
Push to `codex/issue-556` failed with `Could not resolve host: github.com`. The required
merge guard was run in dry-run mode against PR #562 with the source commit as its expected
head; it returned `blocked=true`, `reason=review-thread GraphQL query failed`, and
`merge_executed=false`. No guard was bypassed.

Attempts to reconcile the verified PR checkboxes, add `needs-human`, and post the blocker
comment were rejected with `MCP tool call requires approval, but approval policy is never`.
None of those remote mutations occurred. PR #562 was observed open and ready for review.
The automation runner must publish the checkout changes and complete the remaining lifecycle
on the published head; the local checks above cannot substitute for that hosted receipt.

## Closer integration, 2026-10-12

This optional follow-up preserves seven unique late-evidence regression nodes after source #556 was independently closed. It introduces no new production behavior and does not reopen the original source. Rebased onto main 00d9f964a4f4d55955c329e4d4293b741b8c5035. Six affected suites pass 162 tests, both module selftests pass, and exact integrated collection is 3104. Current main's skip/mypy ceilings are preserved. Fresh hosted complete verification, full topology, review and post-push floor remain required. Historical provider NON_PASS stays unchanged.

## Verdict population preservation, keepalive follow-up (2026-10-12)

The positive delivery check is already implemented by merged #557. This round strengthens two
existing production-writer tests, without changing production behavior or adding collection nodes:

- [x] Preserve the separate verdict population for proven no-delivery runs. Five durable PR
  subjects and one ordinary verifier FAIL_BROKEN retain their durable rate of **0.833** beside
  the offloads. Both effectiveness and value-chain readers retain these delivery counts.
- [x] Enforce the distinct-subject minimum through the production writers. Seven passing
  offload attempts across four subjects refuse a verdict rate. A fifth subject with two failed
  attempts enables a verdict rate of **0.8**, with nine attempts and five subjects; the delivery
  rate remains **0.833**.
- [x] Preserve the verifier veto. Every member of `feedback.VERIFIER_FAILURES`, written in
  lowercase under an adjudicated PASS, remains a failed verdict. The verdict rate is withheld
  below five distinct subjects and measures **0.0** thereafter.
- [ ] Publish this round's commit and obtain final-head hosted complete verification, review,
  guarded merge, and actual postmerge comparison. The original provider NON_PASS is unchanged.

The six-suite command in this document ran with `-m "not slow"`: **162 passed**. Both module
selftests passed, and `python3 -m pytest --collect-only -m "not slow" -q` collected **3104** tests.
The floor and all skip/mypy ceilings remain unchanged. Black **26.10.0** formatted the changed
test and passed the required check:

```sh
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
```

Result: **393 files would be left unchanged**. Pytest **8.3.5**, Black, and their missing
dependencies were recovered from pinned official GitHub sources into `/tmp`, with no repository
dependency changes. Commands used system Python **3.12** (`PATH=/usr/bin:/bin`) and
`PYTHONPATH=/tmp/issue-556-tools`. The Black launcher scheduled its unchanged `reformat_one`
formatter serially; neither discovery nor formatting/checking semantics were narrowed.

The exact RED/restored GREEN command was rerun:

```sh
python3 -m pytest tests/test_capability_effectiveness_predicates.py \
  -k pending_pr_outcome -m "not slow" -q --tb=short
```

RED used `git show 49cdbfee4dba260a5e9149bdd979b7ebca4fd06b:src/capability_effectiveness.py`:
exit **1**, **6 failed, 48 deselected**. Four FAIL_BROKEN/NON_PASS cases read `(5, 5, 1.0)`
instead of `(6, 5, 0.833)`; two PASS cases read `no_merge` instead of `open`.
The `finally` block restored byte-identical source and confirmed unchanged test bytes.
GREEN: exit **0**, **6 passed, 48 deselected**. All databases were temporary; no live Brain
was mutated.

| Binding | Git blob or SHA-256 |
| --- | --- |
| RED source SHA-256 | `2601f7ff5b71cc3748e344870bd0aaac791cee5f6166eab780a09c9d4e32a5b2` |
| Restored GREEN source blob | `770d315257e2402906c4ddbee2bc52047f9dd034` |
| GREEN source SHA-256, before and after | `3414f0ecbe839a5074acf248dfa00bd6d674c271c998b73d525d5ca36262d207` |
| Test blob, identical during RED and GREEN | `98ffdb7dc5c3772a3d8e4cf6196e6af8bb70332c` |
| Test SHA-256 | `31f585bef7347eaa421cc8b41150174c0429887d5b734a260d8b7930b6841fa4` |

Hosted [CI run 38298528111](https://github.com/stranske/Orchestrator/actions/runs/38298528111)
is successful for published head `c99a33c324a2c6ebee574baf1d98dad0bf43b59e` and synthetic merge
`06502ec96dc279b0cd7682db8cb83e28cdee311c`. Its checkout
[verification job 114975447020](https://github.com/stranske/Orchestrator/actions/runs/38298528111/job/114975447020)
reports **3078 passed, 0 failed, 26 allowed skips, 3104 collected**, **97/104 selftests run**,
**3/5 gates green**, and **122 mypy modules checked with zero exemptions**. Flat-mirror and
combined-coverage jobs also concluded success. These receipts cover the prior published bytes
only; they do not establish final-head verification for this round or provider PASS.

Normal staging failed with `index.lock: Read-only file system`. The writable bare clone
`/tmp/issue-556-verdict-git` uses this checkout as its work tree to record the focused test and
evidence commit. PR #562 was observed **open, ready for review**. Attempts to add `needs-human`
and post a blocker comment were rejected with `MCP tool call requires approval, but approval
policy is never`; neither mutation occurred. The remote lifecycle remains unchecked.

Publication from the writable clone failed with `Could not resolve host: github.com`. The
repository merge guard's dry-run returned `blocked=true`, `reason=review-thread GraphQL query
failed`, and `merge_executed=false`. No merge guard was bypassed. The controller must publish
these checkout changes, reconcile the verified PR checkboxes, and complete hosted verification
and the review/merge/comparison lifecycle on that published head. Final readiness inspection
confirmed PR #562 remains open with `isDraft=false` at its prior head `c99a33c`.

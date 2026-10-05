# PR #446: role-owned exclusion provenance

Reviewed repository head: `76fa70e5d1fe2d159b82a5c76e009283d9cee39b`.
The findings and validation below concern the local repair on that head, not a
published commit or current-head CI result.

## Finding and repair

The sole active review thread, [CodeRabbit's provenance finding](https://github.com/stranske/Orchestrator/pull/446#discussion_r4179499530),
remains valid in the reviewed head. Recording a role-owned `transient_infra`
without new notes retains earlier `automatically influenced` notes. Later
propagation mistakes the role's class for inheritance and clears it. An excluded
acting outcome can also overwrite an independently owned role exclusion.

The local repair adds `outcomes.failure_class_origin` through an idempotent
migration. Direct outcome writes and `mark_transient_infra` set `own`; propagated
classes set `inherited`. Role aggregation preserves independently owned
exclusions, regardless of notes or another acting run's exclusion. Only a class
known to be inherited can be cleared by an attributable acting outcome.

Migration conservatively marks existing non-null classes as owned. Legacy notes
cannot establish inheritance, because direct writes could retain those notes.
Consequently, ambiguous historical exclusions require explicit correction rather
than automatic promotion to learning evidence.

## Focused proof

```sh
pytest tests/test_outcomes_closing_pr_verdict.py \
  tests/test_durability_sweep_exact_merge.py \
  tests/test_durability_feedback_recovery.py -q -m 'not slow'
```

Result: **58 passed**. Four new regressions cover repeated propagation after a
direct role outcome without notes, an intervening inherited exclusion, the infra
marker claiming an inherited class, and repeated migration of a legacy store.
The behavior tests check both routing learners, including their observation
counts. Existing tests still prove that attributable failures replace genuinely
inherited exclusions in both arrival orders.

The same four new tests were run with `feedback.py` from the reviewed head in a
temporary import directory, leaving the repaired production file untouched:
**4 failed, 6 deselected**. The failures reproduce the cleared own class, the
overwritten own class, the unclaimed inherited infra class, and the absent
provenance column. Restoring the repaired import path produces the green result
above.

Black 26.5.1 formatted the changed Python files. Its sequential formatter checked
the repository with the same configuration before the mandatory command passed:

```sh
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
```

Result: **305 files would be left unchanged**. `git diff --check` passes.
`durability_sweep.py --selftest` passes; `feedback.py --selftest` passes with its
existing machine-local capability prerequisites reported absent. Tools were
loaded from their official GitHub sources into `/tmp`, because package-index DNS
was unavailable. No tooling dependencies were added to the repository.

The broader `pytest tests/ -q -m 'not slow'` run, with `ORCH_LOCAL_RUNTIME` and
`ORCH_STATE_DIR` pointing into `/tmp`, finished with **1,575 passed, 24 skipped,
3 failed**. The failures were environment prerequisites: the offload heartbeat
test tried to write `/home/runner/.codex/handoff`, and two coverage-report tests
could not import `coverage`. With `HANDOFF_DIR` and `ORCH_OFFLOAD_DIR` also pointed
into `/tmp` and official coverage.py 7.11.0 sources available, the three failing
tests passed on a targeted rerun (**3 passed**). The full suite was not rerun
after those environment corrections; every selected test has passing proof
across the broad run and targeted rerun.

## Publication and reviewer disposition

Local `git add` fails because `.git` is read-only. GitHub connector attempts to
create a source blob, reply to the originating review thread, and add
`needs-human` all return: `MCP tool call requires approval, but approval policy is never`.
No commit, review reply, or label was published by this run.

The originating reviewer has not disposed of the finding. The review thread and
the remaining task checkbox stay open. Publication of the source/test repair,
current-head CI, reviewer disposition, and the guarded closer remain outstanding.
The PR was verified open and ready for review (`draft=false`).

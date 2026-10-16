# Orchestrator #599 consumer binding validation

The existing consumer resolver is reused. All reviewed declarations with a local consumer name defined `file.py:symbol` rails; the external docs-drift repair flow is explicitly missing. The `consumer_missing` diagnostic reaches reconciliation but does not satisfy the unchanged consumer gate, promote lifecycle state, or write action evidence.

Named checks:
- `tests/test_capability_admission.py::test_declared_consumers_resolve_in_the_tree`
- `tests/test_capability_admission.py::test_missing_consumer_declaration_survives_reconciliation_without_acceptance`

Original deliberate break: replace the route-weights-export binding with prose. The named test exits 1 with an assertion identifying that capability; byte-identical restoration exits 0. A separate invocation of the existing local deliberate-break verifier overlays the test file onto base a071ff06 and confirms RED on base/GREEN on the implementation. Existing unchanged tests in that file are not new discriminating evidence.

Private-ledger observation:
```
CONSUMERS (10th): 47 of 48 declare a consumer this tree can find — acting (capability-level) 1, in grace 0, not invoked 0, exercise-bound 17, unmeasured 0
never acted 29 (blocking 0, pre-cutoff debt 30); prose 1, missing 0, undeclared 0 — DRAINABLE 1 (a declaration edit each)
```

The one prose row is docs-drift-fix-agent, marked consumer_missing. Its external implementation has no local acting handover; crediting invocation ingestion would invent output consumption. The literal prose-zero target therefore remains qualified by the issue's explicit instruction to retain missing consumers truthfully. No changes to req_consumer_acts, its grace or enforcement date were made. These counters describe a private snapshot; deployment, production acting counts and original dated native acceptance are not established by symbol resolution.

## Source binding

- `src/capabilities.py`: SHA256 `1abf6fc36369ab13c7a913b0ea1780b08f3dae448ef328628dc2923346a16a71`
- `tests/test_capability_admission.py`: SHA256 `0079fc08d79a4ab0f0e3b97844734311f4b09b2831a89c106ae396d8ad1b786b`

The source collection is 3,668 cases (two new regression cases); every existing skip/selftest/gate/mypy ceiling is unchanged. Complete local verification and hosted results are recorded separately in the PR validation section. The source issue remains subject to its original deployed acceptance.

## Captured original fault control

RED (binding replaced by prose; exit 1):

```text

tests/test_capability_admission.py:833: AssertionError
=========================== short test summary info ============================
FAILED tests/test_capability_admission.py::test_declared_consumers_resolve_in_the_tree
1 failed in 1.64s
```

Restored GREEN (exit 0):

```text
.                                                                        [100%]
1 passed in 0.93s
```

Full initial private-state run executed 3,668 pytest cases: 3,665 passed and three failed; all 104 selftests, five gates and the mypy ratchet passed. Follow-through moved the new tests before the standalone gate entry point and distinguished administrative declaration reconciliation from weekly measurement credit. Exact affected-file checks and a complete final verify run follow in the PR validation record; the initial failures remain part of the evidence.

## 2026-10-16 declaration follow-up

Four bindings now name the code that consumes the output more precisely:

| Capability | Consumer | Evidence in the tree |
| --- | --- | --- |
| strategy-experiments | `exp_abcd.py:prepare_arms` | `prepare_strategy_experiment` passes its planned arms to this launcher, rather than `prepare`. |
| capability-activation-audit | `verify.py:run_gates` | The activation-audit subprocess exit status becomes a gate verdict used by verification. |
| feature-scan | `feature_scan.py:apply_scan` | Reads the scan's `unlogged` proposals and persists them through `features.record_use` when `--apply` is requested. |
| frontend-verifier | `runtime_ac.py:_completed_result; runtime_ac.py:evaluate_results` | Reads the frontend process's JSON result and carries failed assertions into the runtime gate. |

The named resolution test checks each table independently, including declarations
that an overlapping row in the other table could otherwise hide. Two additional
regression cases check that masking failure and demonstrate a scan proposal being
written into a temporary feature registry. The expected collection floor increases
by two; no skip ceiling changes.

Validation on this runner:

- The named test, both new cases, and the existing missing-consumer reconciliation
  case passed when called directly with Python.
- Deliberately replacing the strategy binding with a sentence failed the named
  test with `KNOWN_GATES:strategy-experiments` (exit 1). Restoring byte-identical
  source passed (exit 0).
- `feature_scan.py`, `runtime_ac.py`, and `strategy_experiment.py` selftests passed.
- `git diff --check` passed.

`docs-drift-fix-agent` remains the only declaration marked `consumer_missing: true`;
its external repair output has no local acting handover. This remains a finding,
not an exemption from the acting requirement.

The live ledger is absent on this runner. Running the CLI with
`ORCH_LOCAL_RUNTIME=/tmp/orchestrator-consumer-runtime` reports an empty catalogue:

```text
CONSUMERS (10th): 0 of 0 declare a consumer this tree can find — acting (capability-level) 0, in grace 0, not invoked 0, exercise-bound 0, unmeasured 0
never acted 0 (blocking 0, pre-cutoff debt 0); prose 0, missing 0, undeclared 0 — DRAINABLE 0 (a declaration edit each)
```

This is not evidence of the live-ledger acceptance target. The owner must rerun
after syncing. The existing report still classifies the explicitly missing
docs-drift consumer as prose, so its production prose-zero criterion also remains
unresolved.

Pytest and Black are unavailable, and installing them from PyPI failed because
DNS is blocked. Consequently neither the pytest run nor the mandatory Black gate
could complete, and these follow-up changes must remain uncommitted until that
gate passes. GitHub API access also failed, so no PR body or readiness state could
be updated or verified; the missing-consumer finding and CLI output above are the
pending PR-body addition.


## Current-head closer review repair

Fresh guarded Sol review at51ac7dbf found that the original source599 offload row was omitted from both declaration tables, so the original table-only test could pass while its live prose remained. A read-only live-ledger audit confirmed that row still exists. The added overlay names existing role consumers and the existing backend-run binding; it grants no lifecycle status or measured credit. The new regression starts from the original prose row and requires successful, idempotent reconciliation with unchanged status, event history and outcome links. It assertion-fails before the overlay and passes after it. All original post-sync/October27 acceptance remains open.

## 2026-10-16 keepalive admission-debt regression

Extended the existing missing-consumer reconciliation test to run full admission
on the reconciled `docs-drift-fix-agent` row on both sides of the tenth-part
cutoff. Its diagnostic must remain pre-cutoff debt, block post-cutoff admission,
remain in DRAINABLE, and receive no acting or lifecycle credit. Reusing the
existing test leaves the collection count unchanged. Temporarily replacing the
consumer requirement with an accepting predicate makes these new assertions fail;
restoring the predicate passes.

The named declaration test was also run against a real source edit: replacing
the route-weights-export binding with prose exits 1 and identifies
`KNOWN_GATES:route-weights-export`; restoring byte-identical source exits 0.
The missing-consumer inventory remains exactly `docs-drift-fix-agent`.

The live ledger is unavailable. Running
`ORCH_LOCAL_RUNTIME=/tmp/orchestrator-consumer-633-runtime python3 src/capability_admission.py`
exits 0 and prints the same empty-catalogue CONSUMERS lines shown above. Those
zeros do not establish live acceptance. Running the admission selftest against
that empty runtime fails its `ledger must load` prerequisite assertion.

Pytest and both required Black commands cannot run because their executables are
absent. Installation from PyPI fails on DNS resolution. The extended Python test
therefore remains uncommitted under the mandatory Black gate; direct function
execution and the deliberate-break checks are not substitutes for that gate.
Eight focused checks passed by direct function execution, including this extended
test, the named resolution test, reconciliation and consumer cutoff/acting cases,
and feature-scan output consumption. These results are not a pytest suite result.

The proposed PR body is saved at `/tmp/pr-633-keepalive-body.md`, including the
missing-consumer finding, the empty-run output, and verified task checkboxes;
live post-sync acceptance stays open. Publishing the body, adding a blocker
comment, and adding `needs-human` were each rejected by the connector because
approval is required while this run's approval policy is `never`. The hosted PR
body and task checkboxes remain unchanged.

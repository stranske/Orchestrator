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

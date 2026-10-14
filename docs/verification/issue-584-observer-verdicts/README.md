# Issue 584 verification

The existing selection exemption now also governs usefulness verdicts. Explicit code declarations cover propensity and redirect without widening observer matcher kinds. No new capability, store, runtime flag or scheduler was added.

- `python3 -m pytest tests/test_capabilities.py tests/test_tick_evidence_projection.py tests/test_value_chain_monitor.py -q`: 63 passed.
- `python3 src/capabilities.py --selftest` and `python3 src/capability_propensity.py --selftest`: pass in isolated selftest state.
- Black, Ruff, focused mypy: pass. Declaration validation against a private copy of the populated ledger: 49 rows valid; production history was untouched.
- Exact collection: 3310 = base 3306 + four executed regression nodes. Floor retains every skip/selftest/gate/mypy ceiling. This is a union of executed test nodes, not a full-suite PASS claim.
- `proof.json` names each exact mutation and command. Removing the refusal yields DID NOT RAISE. Restoring tick recording yields verdict_failed. Both commands exit 1, then 0 after byte-identical source restoration.

Historical verdict events remain append-only; `observer_noise` carries their old counts, excluded from raw and weighted delivery rates. Report deltas, cadence and observing liveness remain. Consumer actions reuse the value-chain monitor's accepted-edge classification and window; unread evidence is unmeasured.

Hosted CI, fresh current-head review and the seven-minute floor remain required. Source #584 stays open for the owner's mirror sync and the 2026-10-20 observation of usage rows and no new observer_output_change events. No deployment or production acceptance is claimed.

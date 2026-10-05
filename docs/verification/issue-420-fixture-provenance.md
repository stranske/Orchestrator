# Fixture provenance acceptance proof

Issue #420 separates sandboxed contract passes from production usefulness. The existing event
ledger and ranking/reporting paths are extended; no capability or parallel store is introduced.

## Reproduce

```sh
PYTHONPATH=src python3 -m pytest tests/test_fixture_provenance.py -q
python3 src/capability_propensity.py --selftest
python3 src/capabilities.py --selftest
python3 src/rail_exercise.py --selftest
python3 src/verify.py
```

The initial full local verifier passed 1,857 tests, 99 module selftests, all five capability gates,
and the mypy ratchet across 116 modules; only the measured collection floor lagged by five tests.
The floor update preserves every skip and exemption ceiling.

The five regression tests verify zero ranking influence, separate production/fixture usage counters,
append-only and idempotent migration, detector isolation, and the cadence recorder's provenance.
The migration CLI accepts `--ledger /absolute/path/to/capabilities.json`; its summary names both
changed and unchanged outcomes. Historical hashed advice references are recognized from their
explicit `contract .../exercises[/2]/...json:` evidence, alongside explicit rail-exercise identities.
A generic production evidence mention of a fixture is preserved as production evidence.

## Deliberate break and restoration

Each control runs against a temporary copy of the module, imported before pytest. The checkout
source stays unchanged.

| Control | Named regression | Broken exit | Restored exit |
|---|---|---:|---:|
| Give fixture_observed the machine_observed weight | test_fixture_verdicts_never_rank | 1 | 0 |
| Add fixture passes to the production-useful count | test_usage_report_splits_fixture_from_production | 1 | 0 |

Original outcome events retain their recorded provenance and metadata. The migration appends one
idempotent correction per capability/experiment; readers apply that correction without counting it
as a new outcome. Fixture failure counts are also retained separately. Fixture verdicts cannot be
used as late production corroboration. Production verdicts remain eligible for ranking and detection.

This source change does not enable the recording flag or publish an execution mirror. CI validates
the pushed PR head in checkout and flat mirror shapes; later guarded publication is a separate step.

Final verification after the measured floor update: `python3 src/verify.py` exited 0, with 1,857/1,857 tests, 99/99 selftests, all five gates green, and 116 modules checked by mypy with zero exemptions. The original floor history and every skip/exemption ceiling are preserved. The final log is in the opener receipt `evidence/20261005T0501Z/verify420-green.log`.

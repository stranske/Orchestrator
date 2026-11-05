# CSV canary contract verification (reproducible)

- **Issue**: [stranske/Orchestrator#988](https://github.com/stranske/Orchestrator/issues/988)
- **Partial revision**: commit `05a7ce20246df00b09daf091d16e5f25c13e96a7` (PR #984)
- **Corrected revision**: the head of [PR #989](https://github.com/stranske/Orchestrator/pull/989)

Native Verify outcomes for this work are the PR’s **Verify** CI check and the post-merge verifier. This file does not substitute for either.

## Pytest must not collect the fixture tree

```bash
python3 -I -m pytest --collect-only tests/fixtures/closer_real_csv_canary -q; echo $?
```

```
no tests collected in 0.03s
5
```

(`5` is pytest’s “no tests collected” exit code.)

## Partial revision: contract script fails

```bash
mkdir -p /tmp/partial && git show 05a7ce20246df00b09daf091d16e5f25c13e96a7:tests/fixtures/closer_real_csv_canary/v1/csv_export.py > /tmp/partial/csv_export.py && python3 -I tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py /tmp/partial/csv_export.py; echo $?
```

```
PASS json-round-trip
PASS unsupported-format
FAIL csv-shape-and-round-trip: NotImplementedError: Declared incomplete CSV canary fixture
1 failed
1
```

## Corrected revision (checkout PR #989): contract script passes

```bash
python3 -I tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py; echo $?
```

```
PASS json-round-trip
PASS unsupported-format
PASS csv-shape-and-round-trip
PASS tests/fixtures/closer_real_csv_canary/v1/csv_export.py
0
```
